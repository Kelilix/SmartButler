"""🔒 TaskScheduler scan 互斥锁回归测试(2026-10-11)。

修的 bug:
    ``_scan_once`` 是 fire-and-forget,两次触发重叠时会同时读到**同一个未推进的
    游标** ``_processed_ids``,把同一批消息喂给两次 judge → 每次都判出
    ``start_new`` → 建出两个 task,且同一 ``message_id`` 落进两个 task
    (各自抽出一条内容重复的 fact)。实测本该 2 个 task 建出 4 个。

覆盖:
1. 并发触发只跑一个 scan,另一个被跳过(互斥)
2. 被跳过的触发置 pending,当前 scan 结束后**自动补跑**
3. 补跑能判到"原本被跳过的那条消息"(不丢尾消息)
4. 回归:并发触发不再产生重复 task / 重复 fact
5. 锁懒初始化(非 loop 线程 __init__ 也不炸)
6. stop() 能drain 到补跑的 scan
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from smartbutler.emotion.memory.task_index import TaskIndex
from smartbutler.emotion.memory.task_scheduler import TaskScheduler
from smartbutler.emotion.memory.task_segmenter import SegmentResult

# ---------------------------------------------------------------------------
# 1. 互斥:并发触发只有一个真跑
# ---------------------------------------------------------------------------


class _GateSegmenter:
    """segmenter,用 ``gate`` 事件控制 judge 何时返回。"""

    def __init__(self, gate: asyncio.Event | None = None, *, result: SegmentResult | None = None):
        self.gate = gate
        self.result = result or SegmentResult(actions=[], summary="")
        self.concurrent_peak = 0
        self._active = 0
        self.calls: list[list[dict[str, Any]]] = []

    async def segment(self, *, new_messages: list[dict[str, Any]], **kw: Any) -> SegmentResult:  # noqa: ARG002
        self.calls.append(list(new_messages))
        self._active += 1
        self.concurrent_peak = max(self.concurrent_peak, self._active)
        try:
            if self.gate is not None:
                await self.gate.wait()
            else:
                await asyncio.sleep(0.05)
            return self.result
        finally:
            self._active -= 1


def _make_sched(
    tmp_path: Path, *, segmenter: Any, threads: list[str] | None = None,
) -> tuple[TaskScheduler, TaskIndex]:
    idx = TaskIndex(tmp_path / "tasks.db")
    sched = TaskScheduler(
        short_term=_StubShortTerm(threads=threads),  # type: ignore[arg-type]
        task_index=idx,
        segmenter=segmenter,
        on_message_enabled=True,
        min_interval_seconds=0.0,
        judge_window_size=10,
    )
    return sched, idx


class _StubShortTerm:
    def __init__(self, *, threads: list[str] | None = None) -> None:
        self._threads = threads or []

    async def get_async_saver(self) -> Any:
        return _StubSaver(threads=self._threads)


class _StubCheckpoint:
    def __init__(self, thread_id: str) -> None:
        self.config = {"configurable": {"thread_id": thread_id}}


class _StubMessage:
    def __init__(self, *, role: str, content: str, mid: str) -> None:
        self.role = role
        self.type = role
        self.content = content
        self.additional_kwargs = {"user_id": "u1", "spoken_at": "2026-10-11T00:00:00+00:00"}
        self.id = mid


class _StubSaver:
    """按 thread 返回可控消息列表的 stub。"""

    def __init__(self, *, threads: list[str] | None = None,
                 messages: dict[str, list[_StubMessage]] | None = None) -> None:
        self._threads = threads or []
        self._messages = messages or {}

    def alist(self, *, config: Any = None, limit: int = 100) -> Any:  # noqa: ARG002
        async def _gen() -> Any:
            for tid in self._threads:
                yield _StubCheckpoint(thread_id=tid)
        return _gen()

    async def aget(self, config: dict[str, Any]) -> dict[str, Any] | None:  # noqa: ARG002
        tid = (config or {}).get("configurable", {}).get("thread_id", "")
        msgs = self._messages.get(tid) or [_StubMessage(role="user", content="test", mid=f"{tid}:0")]
        return {"channel_values": {"messages": msgs}}


@pytest.mark.asyncio
async def test_concurrent_triggers_run_only_one_scan(tmp_path: Path):
    """并发触发:第二个 scan 必须被跳过,judge 绝不并发执行。"""
    gate = asyncio.Event()
    seg = _GateSegmenter(gate)
    sched, _ = _make_sched(tmp_path, segmenter=seg, threads=["t1"])

    first = asyncio.create_task(sched.scan_now())
    await asyncio.sleep(0.02)  # 让第一个 scan 拿到锁并进入 judge

    second = await sched.scan_now()
    gate.set()
    await first

    assert second == {"skipped": "scan_in_flight"}
    assert seg.concurrent_peak == 1, "judge 出现并发执行 —— 锁没生效"
    assert sched.skipped_by_lock == 1


# ---------------------------------------------------------------------------
# 2+3. pending 补跑:被跳过的消息不能丢
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_skipped_trigger_is_rescanned_after_current_finishes(tmp_path: Path):
    """被跳过的触发 → 当前 scan 结束后自动补跑(run_count 2 而非 1)。

    注意:补跑那次通常判定为 ``no_new_messages`` 并**不**再调 segmenter ——
    这才是正确行为。补跑的语义是"再扫一遍,确认没有遗漏",不是"必须再判一次"。
    真正要验证的是补跑**被触发了**,由 run_count 体现。
    """
    gate = asyncio.Event()
    seg = _GateSegmenter(gate)
    sched, _ = _make_sched(tmp_path, segmenter=seg, threads=["t1"])

    first = asyncio.create_task(sched.scan_now())
    await asyncio.sleep(0.02)
    assert await sched.scan_now() == {"skipped": "scan_in_flight"}

    gate.set()
    await first

    # 补跑 task 由 _scan_once 的 finally fire,等它跑完
    for _ in range(50):
        pending = [t for t in asyncio.all_tasks() if t.get_name() == "task-scheduler-on-message"]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
            break
        await asyncio.sleep(0.02)

    assert sched.run_count == 2, f"补跑没发生,run_count={sched.run_count}(应为 2)"
    assert sched.skipped_by_lock == 1


# ---------------------------------------------------------------------------
# 4. 回归:并发不再产生重复 task
# ---------------------------------------------------------------------------


class _FakeLLM:
    """假 LLM —— 每次都让 judge 开一个新 task。

    必须用**真** TaskSegmenter:建 task 的动作发生在 ``_apply_actions``
    (segmenter 内部),假 segmenter 只返回 action 不会写库,测不到重复 task。
    """

    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, *, messages: Any, temperature: float = 0.0) -> Any:  # noqa: ARG002
        self.calls += 1
        import json as _json

        from smartbutler.capabilities.llm.types import LLMResponse

        payload = {
            "actions": [
                {
                    "type": "start_new",
                    "category": "other",
                    "title": f"task-{self.calls}",
                    "reason": "always new",
                }
            ],
            "summary": "s",
        }
        return LLMResponse(content=_json.dumps(payload, ensure_ascii=False))


def _real_sched(tmp_path: Path, *, threads: list[str] | None = None, saver: Any = None
                ) -> tuple[TaskScheduler, TaskIndex, _FakeLLM]:
    """构造走真 TaskSegmenter + 假 LLM 的 scheduler。"""
    from smartbutler.emotion.memory.task_segmenter import TaskSegmenter

    idx = TaskIndex(tmp_path / "tasks.db")
    llm = _FakeLLM()
    seg = TaskSegmenter(task_index=idx, llm=llm, llm_timeout_seconds=5.0)
    sched = TaskScheduler(
        short_term=_StubShortTermWithSaver(
            saver or _StubSaver(threads=threads)
        ),  # type: ignore[arg-type]
        task_index=idx,
        segmenter=seg,
        on_message_enabled=True,
        min_interval_seconds=0.0,
        judge_window_size=10,
    )
    return sched, idx, llm


@pytest.mark.asyncio
async def test_concurrent_scans_do_not_create_duplicate_tasks(tmp_path: Path):
    """🔒 核心回归:同一批消息并发 judge 不得建出 2 个 task。

    老行为:两个 scan 都读到空游标 → 都判 start_new → 建出 2 个 task。
    加锁后:只有一个 scan 真跑,只建出 1 个 task。
    """
    sched, idx, llm = _real_sched(tmp_path, threads=["t1"])

    await asyncio.gather(sched.scan_now(), sched.scan_now(), sched.scan_now())

    tasks = idx.list_active_tasks()
    assert len(tasks) == 1, f"并发建出了 {len(tasks)} 个 task(应为 1)"
    assert llm.calls == 1


@pytest.mark.asyncio
async def test_second_scan_after_lock_release_creates_no_duplicate(tmp_path: Path):
    """锁释放后再扫一次:没有新消息 → 不该再建 task。"""
    sched, idx, llm = _real_sched(tmp_path, threads=["t1"])

    await sched.scan_now()
    await sched.scan_now()

    tasks = idx.list_active_tasks()
    assert len(tasks) == 1, f"重复扫导致 {len(tasks)} 个 task"
    assert llm.calls == 1, "无新消息不该再调 LLM"


@pytest.mark.asyncio
async def test_new_message_after_concurrency_still_gets_its_own_task(tmp_path: Path):
    """锁不能把新消息也一起吞掉 —— 补跑后新消息仍要建 task。"""
    saver = _StubSaver(
        threads=["t1"],
        messages={"t1": [_StubMessage(role="user", content="第一条", mid="m1")]},
    )
    sched, idx, _ = _real_sched(tmp_path, saver=saver)

    await asyncio.gather(sched.scan_now(), sched.scan_now())
    assert len(idx.list_active_tasks()) == 1

    # 追加第二条新消息。
    # ⚠️ 上面的并发 gather 必然置 pending(第二次被锁跳过),由 finally 补跑一次。
    # 那个补跑 task 是 fire-and-forget,必须等它跑完再断言 —— 否则断言的是
    # 补跑前的中间状态。锁本身行为正确:补跑日志显示 total=2 fresh=1。
    saver._messages["t1"].append(_StubMessage(role="user", content="第二条", mid="m2"))
    await sched.scan_now()
    for _ in range(50):
        pending = [t for t in asyncio.all_tasks()
                   if t.get_name() == "task-scheduler-on-message"]
        if not pending:
            break
        await asyncio.gather(*pending, return_exceptions=True)
        await asyncio.sleep(0.02)
    await asyncio.sleep(0.05)

    tasks = idx.list_active_tasks()
    assert len(tasks) == 2, (
        f"新消息应建第 2 个 task,实际 {len(tasks)}: "
        f"active={[(t.task_id, t.status, t.title) for t in tasks]}"
    )


class _StubShortTermWithSaver:
    def __init__(self, saver: Any) -> None:
        self._saver = saver

    async def get_async_saver(self) -> Any:
        return self._saver


# ---------------------------------------------------------------------------
# 5. 锁懒初始化
# ---------------------------------------------------------------------------


def test_lock_is_lazy_and_not_created_in_init(tmp_path: Path):
    """__init__ 不建锁 —— 可能在非 loop 线程构造 scheduler。"""
    seg = _GateSegmenter()
    sched, _ = _make_sched(tmp_path, segmenter=seg)
    assert sched._scan_lock is None  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_lock_created_on_first_scan(tmp_path: Path):
    seg = _GateSegmenter()
    sched, _ = _make_sched(tmp_path, segmenter=seg, threads=["t1"])
    await sched.scan_now()
    assert sched._scan_lock is not None  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# 6. stop() drain 到补跑的 scan
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stop_drains_pending_rescan(tmp_path: Path):
    """stop() 必须等到补跑结束 —— 否则会话退出时尾消息永远没进记忆。

    ⚠️ 必须通过 ``_fire_scan_now`` 起第一个 scan(而不是手动 create_task):
    ``stop()`` 的 drain 循环只等 ``_inflight_scans`` 里的 task,
    手动 create_task 不在其中,drain 会立刻空转返回 —— 那是测试构造问题,
    生产路径上所有 scan 都由 ``_fire_scan_now`` 创建,必然在集合里。
    """
    gate = asyncio.Event()
    seg = _GateSegmenter(gate)
    sched, _ = _make_sched(tmp_path, segmenter=seg, threads=["t1"])

    sched._fire_scan_now()  # noqa: SLF001 —— 进 _inflight_scans,stop 能等到
    await asyncio.sleep(0.02)
    assert await sched.scan_now() == {"skipped": "scan_in_flight"}
    gate.set()

    await sched.stop()

    # stop 之后不该还有 pending 悬着,也不该有 scan 留在飞行中
    assert sched._scan_pending is False  # type: ignore[attr-defined]
    assert not sched._inflight_scans, "stop 返回后仍有 scan 在跑"
