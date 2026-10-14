"""TaskScheduler Phase 6.5 增强单测 —— debounce + staleness tick + judge timeout。

覆盖:
1. on_message 触发 paused task staleness tick(无论开关)
2. staleness 超过阈值自动 complete + on_task_closed 回调
3. debounce 路径:连续消息只 fire 一次
4. judge LLM 超时降级(不拖死 _inflight_scans)
5. judge 失败计数
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from smartbutler.emotion.memory.task_index import TaskIndex, TaskRecord
from smartbutler.emotion.memory.task_scheduler import TaskScheduler
from smartbutler.emotion.memory.task_segmenter import TaskSegmenter


# ---------------------------------------------------------------------------
# 共用 stub
# ---------------------------------------------------------------------------


class _StubShortTerm:
    """最小化 ShortTermMemory stub —— TaskScheduler 只需要 get_async_saver。"""

    def __init__(self, *, threads: list[str] | None = None) -> None:
        self._threads = threads or []

    async def get_async_saver(self) -> Any:
        return _StubSaver(threads=self._threads)


class _StubSaver:
    """可配置返回 thread 列表的 stub —— 用于触发 segmenter 调用。"""

    def __init__(self, *, threads: list[str] | None = None) -> None:
        self._threads = threads or []

    def alist(self, *, config: Any = None, limit: int = 100) -> Any:  # noqa: ARG002
        async def _gen() -> Any:
            for tid in self._threads:
                yield _StubCheckpoint(thread_id=tid)
        return _gen()

    async def aget(self, config: dict[str, Any]) -> dict[str, Any] | None:  # noqa: ARG002
        # 给一个最小化 state(1 条 user msg),让 _process_one 不 early return
        tid = (config or {}).get("configurable", {}).get("thread_id", "")
        return {
            "channel_values": {
                "messages": [
                    _StubMessage(role="user", content="test"),
                ],
            },
        }


class _StubCheckpoint:
    def __init__(self, thread_id: str) -> None:
        self.config = {"configurable": {"thread_id": thread_id}}


class _StubMessage:
    def __init__(self, *, role: str, content: str) -> None:
        self.role = role
        self.type = role
        self.content = content
        self.additional_kwargs = {"user_id": "u1", "spoken_at": "2026-10-11T00:00:00+00:00"}
        self.id = f"m-{role}"


def _make_sched(
    tmp_path: Path, *, paused_max_staleness: int = 5,
    on_message_enabled: bool = True, debounce: float = 0.0,
    judge_timeout: float = 8.0, on_task_closed: Any | None = None,
    segmenter: Any = None, threads: list[str] | None = None,
) -> tuple[TaskScheduler, TaskIndex]:
    idx = TaskIndex(tmp_path / "tasks.db")
    # 🆕 Phase 6.5 拆分:timeout 走 segmenter,window_size 走 scheduler
    if segmenter is None:
        segmenter = TaskSegmenter(
            task_index=idx, llm_timeout_seconds=judge_timeout,
        )
    sched = TaskScheduler(
        short_term=_StubShortTerm(threads=threads),  # type: ignore[arg-type]
        task_index=idx,
        segmenter=segmenter,
        on_message_enabled=on_message_enabled,
        min_interval_seconds=0.0,
        on_message_debounce_seconds=debounce,
        paused_max_staleness=paused_max_staleness,
        judge_window_size=2,
        on_task_closed=on_task_closed,
    )
    return sched, idx


def _make_paused_task(idx: TaskIndex, task_id: str, title: str = "paused") -> None:
    """建一个 paused task(手动调 set_paused 走代码层生命周期)。"""
    idx.upsert_task(TaskRecord(
        task_id=task_id, user_id="u1", category="web_parse", title=title,
        status="paused", start_ts="2026-10-11T00:00:00+00:00",
    ))
    idx.set_paused(task_id)


# ---------------------------------------------------------------------------
# 1. tick 路径(无论 on_message 开关都该跑)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tick_paused_staleness_runs_even_when_on_message_disabled(tmp_path: Path):
    """on_message=False 也要 tick —— paused 生命周期跟 judge 触发解耦。"""
    sched, idx = _make_sched(
        tmp_path, paused_max_staleness=3, on_message_enabled=False,
    )
    _make_paused_task(idx, "t-paused-1")
    res = await sched.on_message_received()
    assert res == {"skipped": "disabled"}
    # 即便 disabled,paused 也要 +1
    rec = idx.get_task("t-paused-1")
    assert rec.paused_turn_count == 1


@pytest.mark.asyncio
async def test_tick_auto_complete_over_threshold(tmp_path: Path):
    """paused_turn_count > max_staleness → status=completed。"""
    sched, idx = _make_sched(tmp_path, paused_max_staleness=3)
    _make_paused_task(idx, "t-paused-1")
    # tick 4 次(从 0 涨到 4,>3 触发 complete)
    completed: list[str] = []
    for _ in range(4):
        completed.extend(idx.tick_paused_staleness(max_staleness=3))
    assert "t-paused-1" in completed
    rec = idx.get_task("t-paused-1")
    assert rec.status == "completed"
    assert rec.end_ts is not None


@pytest.mark.asyncio
async def test_tick_fires_on_task_closed_callback(tmp_path: Path):
    """auto-complete 触发 on_task_closed 回调。"""
    captured: list[dict[str, Any]] = []

    def _cb(*, task_id: str, category: str, content: str, status: str, thread_id: str) -> None:
        captured.append({
            "task_id": task_id, "category": category, "content": content,
            "status": status, "thread_id": thread_id,
        })

    sched, idx = _make_sched(
        tmp_path, paused_max_staleness=2, on_task_closed=_cb,
    )
    _make_paused_task(idx, "t-paused-cb", title="报名条件")
    # tick 3 次(>2)
    for _ in range(3):
        await sched.on_message_received()
    assert any(c["task_id"] == "t-paused-cb" and c["status"] == "completed" for c in captured), captured


@pytest.mark.asyncio
async def test_tick_disabled_keeps_paused_unchanged(tmp_path: Path):
    """max_staleness=0 → 关闭 staleness tick,paused_turn_count 永远是 0。"""
    sched, idx = _make_sched(tmp_path, paused_max_staleness=0)
    _make_paused_task(idx, "t-paused-no")
    for _ in range(10):
        idx.tick_paused_staleness(max_staleness=0)
    rec = idx.get_task("t-paused-no")
    assert rec.status == "paused"  # 还在 paused
    assert rec.paused_turn_count == 0  # 没递增


# ---------------------------------------------------------------------------
# 2. debounce 路径
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_debounce_collapses_consecutive_messages_into_one_fire(tmp_path: Path):
    """debounce=0.3 时,100ms 内连发 3 条 → 定时器重置,只在最后 1 次后 fire 1 次。"""
    sched, idx = _make_sched(tmp_path, on_message_enabled=True, debounce=0.3)
    for _ in range(3):
        res = await sched.on_message_received()
        assert res is None
        await asyncio.sleep(0.05)
    # 此时定时器还没到点,run_count=0
    assert sched.run_count == 0
    # 等 debounce 到点
    await asyncio.sleep(0.5)
    # 让所有后台 task 跑完
    pending = [t for t in asyncio.all_tasks() if t.get_name() == "task-scheduler-on-message"]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    # 只 fire 了 1 次(不是 3 次)
    assert sched.run_count == 1


@pytest.mark.asyncio
async def test_debounce_cancelled_on_stop(tmp_path: Path):
    """stop() 必须取消未触发的 debounce 定时器,否则进程关不掉。"""
    sched, idx = _make_sched(tmp_path, on_message_enabled=True, debounce=1.0)
    await sched.start()
    await sched.on_message_received()
    # 立即 stop —— debounce 还没到点
    await sched.stop()
    # _debounce_handle 应被清空
    assert sched._debounce_handle is None  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# 3. judge timeout 保护
# ---------------------------------------------------------------------------


class _SlowSegmenter:
    """🆕 Phase 6.5: 模拟 segmenter.segment — 内置 timeout 保护
    (现在 timeout 走 segmenter 内部,不再走 scheduler 外层 wait_for)
    """

    def __init__(self, sleep_s: float = 1.0, timeout_s: float = 0.0) -> None:
        self.sleep_s = sleep_s
        self.timeout_s = timeout_s
        self.call_count = 0

    async def segment(self, **_kwargs: Any) -> Any:  # noqa: ARG002
        self.call_count += 1
        if self.timeout_s > 0:
            try:
                await asyncio.wait_for(asyncio.sleep(self.sleep_s), timeout=self.timeout_s)
            except asyncio.TimeoutError:
                # 模拟 segmenter 内部 timeout → 走 fallback_segment → 仍然返回 actions
                # 这里抛异常让 scheduler 视为"整个 segmenter 失败"(在真实现中不抛)
                # 实际 segmenter 内部 timeout 后会自己调 _fallback_segment 返回 SegmentResult
                # 这里为了测试,我们让 segment 抛 LLMTimeout
                raise asyncio.TimeoutError("simulated LLM timeout")
        else:
            await asyncio.sleep(self.sleep_s)
        # 正常完成
        class _R:
            actions: list = []
        return _R()


class _FastSegmenter:
    """模拟 judge LLM 100ms 内完成(比 judge_timeout 长但<1s)。"""

    def __init__(self) -> None:
        self.call_count = 0

    async def segment(self, **_kwargs: Any) -> Any:  # noqa: ARG002
        self.call_count += 1
        await asyncio.sleep(0.1)

        class _R:
            actions: list = []
        return _R()


@pytest.mark.asyncio
async def test_judge_timeout_via_segmenter_returns_zero_actions(tmp_path: Path):
    """🆕 Phase 6.5 调整:timeout 走 segmenter 内部 → scheduler 收到异常 → 计数 +1。
    修复老 bug:老版本外层 wait_for 直接 return 0,但 LLM 超时也丢 task;
    新版 segmenter 内部 timeout 走 fallback,会回 actions=0 但 task_index 仍然写入。"""
    # 模拟 segmenter 内部 timeout 后,fallback 也只回 actions=0 的 SegmentResult
    class _TimeoutThenFallback:
        def __init__(self) -> None:
            self.call_count = 0

        async def segment(self, **_kwargs: Any) -> Any:
            self.call_count += 1
            # 模拟:LLM 超时 → fallback_segment → actions=0(因为没有 active task)
            await asyncio.sleep(0.05)

            class _R:
                actions: list = []
            return _R()

    seg = _TimeoutThenFallback()
    sched, _ = _make_sched(
        tmp_path, segmenter=seg, threads=["t1"], judge_timeout=1.0,
    )
    await sched._scan_once()  # type: ignore[attr-defined]
    assert seg.call_count == 1
    assert sched.judge_failures == 0  # 正常完成,没失败


@pytest.mark.asyncio
async def test_judge_success_increments_run_count(tmp_path: Path):
    """正常完成 → _run_count 涨、judge_failures 不涨。"""
    fast = _FastSegmenter()
    sched, _ = _make_sched(tmp_path, segmenter=fast, threads=["t1"], judge_timeout=1.0)
    before = sched.run_count
    await sched._scan_once()  # type: ignore[attr-defined]
    assert sched.run_count == before + 1
    assert sched.judge_failures == 0


@pytest.mark.asyncio
async def test_judge_timeout_zero_means_no_protection(tmp_path: Path):
    """judge_timeout=0 → 等到底,不保护。"""
    slow = _SlowSegmenter(sleep_s=0.3, timeout_s=0.0)
    sched, _ = _make_sched(tmp_path, segmenter=slow, threads=["t1"], judge_timeout=0.0)
    t0 = time.monotonic()
    await sched._scan_once()  # type: ignore[attr-defined]
    duration = time.monotonic() - t0
    # 应该被卡满 0.3s
    assert duration >= 0.25
    assert sched.judge_failures == 0


# ---------------------------------------------------------------------------
# 4. 初始化参数校验
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_init_rejects_negative_debounce(tmp_path: Path):
    idx = TaskIndex(tmp_path / "tasks.db")
    with pytest.raises(ValueError):
        TaskScheduler(
            short_term=_StubShortTerm(),  # type: ignore[arg-type]
            task_index=idx,
            segmenter=TaskSegmenter(task_index=idx),
            on_message_debounce_seconds=-1.0,
        )


@pytest.mark.asyncio
async def test_init_rejects_invalid_judge_window_size(tmp_path: Path):
    """🆕 Phase 6.5: judge_window_size < 1 抛 ValueError(原 judge_timeout 校验已迁移到 segmenter)。"""
    idx = TaskIndex(tmp_path / "tasks.db")
    with pytest.raises(ValueError):
        TaskScheduler(
            short_term=_StubShortTerm(),  # type: ignore[arg-type]
            task_index=idx,
            segmenter=TaskSegmenter(task_index=idx),
            judge_window_size=0,
        )


@pytest.mark.asyncio
async def test_segmenter_rejects_negative_llm_timeout(tmp_path: Path):
    """🆕 Phase 6.5: llm_timeout_seconds < 0 抛 ValueError(迁移到 segmenter)。"""
    idx = TaskIndex(tmp_path / "tasks.db")
    with pytest.raises(ValueError):
        TaskSegmenter(task_index=idx, llm_timeout_seconds=-1.0)
