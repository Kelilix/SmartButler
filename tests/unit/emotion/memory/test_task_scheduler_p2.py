"""TaskScheduler Phase 6.4 P2 单测 —— on_message_received 触发 + 频率限制。"""
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

from smartbutler.emotion.memory.task_index import TaskIndex
from smartbutler.emotion.memory.task_scheduler import TaskScheduler
from smartbutler.emotion.memory.task_segmenter import TaskSegmenter


class _StubShortTerm:
    """最小化 ShortTermMemory stub —— TaskScheduler 只需要 get_async_saver。"""

    async def get_async_saver(self) -> Any:
        return _StubSaver()


class _StubSaver:
    """空 saver —— 没有 thread 也能跑。"""

    def alist(self, *, config: Any = None, limit: int = 100) -> Any:  # noqa: ARG002
        async def _gen() -> Any:
            if False:
                yield None
        return _gen()

    async def aget(self, config: dict[str, Any]) -> None:  # noqa: ARG002
        return None


@pytest.mark.asyncio
async def test_on_message_disabled_returns_skipped(tmp_path: Path):
    """开关关闭 → 返 {"skipped": "disabled"},_last_trigger_ts 不更新。"""
    idx = TaskIndex(tmp_path / "tasks.db")
    sched = TaskScheduler(
        short_term=_StubShortTerm(),  # type: ignore[arg-type]
        task_index=idx,
        segmenter=TaskSegmenter(task_index=idx),
        on_message_enabled=False,
    )
    res = await sched.on_message_received()
    assert res == {"skipped": "disabled"}
    assert sched.last_trigger_ts == 0.0


@pytest.mark.asyncio
async def test_on_message_throttled_within_min_interval(tmp_path: Path):
    """在 min_interval 窗口内连续触发 → 第二个被 throttle。"""
    idx = TaskIndex(tmp_path / "tasks.db")
    sched = TaskScheduler(
        short_term=_StubShortTerm(),  # type: ignore[arg-type]
        task_index=idx,
        segmenter=TaskSegmenter(task_index=idx),
        on_message_enabled=True,
        min_interval_seconds=10.0,
    )
    res1 = await sched.on_message_received()
    # res1 是 None(已 fire-and-forget)
    assert res1 is None
    # 立即再触发一次
    res2 = await sched.on_message_received()
    assert res2 == {"skipped": "throttle"}
    assert sched.skipped_by_throttle == 1


@pytest.mark.asyncio
async def test_on_message_increments_run_count_via_fire_and_forget(tmp_path: Path):
    """on_message 触发后,_run_count 应该在后台 task 完成后 +1。"""
    idx = TaskIndex(tmp_path / "tasks.db")
    sched = TaskScheduler(
        short_term=_StubShortTerm(),  # type: ignore[arg-type]
        task_index=idx,
        segmenter=TaskSegmenter(task_index=idx),
        on_message_enabled=True,
        min_interval_seconds=0.0,  # 不限频
    )
    before = sched.run_count
    await sched.on_message_received()
    # 等后台 task 跑完
    await asyncio.sleep(0.1)
    # 让所有 pending task 跑完
    pending = [t for t in asyncio.all_tasks() if t.get_name() == "task-scheduler-on-message"]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    assert sched.run_count == before + 1


@pytest.mark.asyncio
async def test_on_message_min_interval_zero_always_triggers(tmp_path: Path):
    """min_interval=0 → 不限频,每次都触发。"""
    idx = TaskIndex(tmp_path / "tasks.db")
    sched = TaskScheduler(
        short_term=_StubShortTerm(),  # type: ignore[arg-type]
        task_index=idx,
        segmenter=TaskSegmenter(task_index=idx),
        on_message_enabled=True,
        min_interval_seconds=0.0,
    )
    await sched.on_message_received()
    await sched.on_message_received()
    await sched.on_message_received()
    # 等所有后台 task 跑完
    await asyncio.sleep(0.1)
    pending = [t for t in asyncio.all_tasks() if t.get_name() == "task-scheduler-on-message"]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    assert sched.skipped_by_throttle == 0


@pytest.mark.asyncio
async def test_min_interval_must_be_non_negative(tmp_path: Path):
    """min_interval_seconds < 0 抛 ValueError。"""
    idx = TaskIndex(tmp_path / "tasks.db")
    with pytest.raises(ValueError):
        TaskScheduler(
            short_term=_StubShortTerm(),  # type: ignore[arg-type]
            task_index=idx,
            segmenter=TaskSegmenter(task_index=idx),
            min_interval_seconds=-1.0,
        )


@pytest.mark.asyncio
async def test_interval_seconds_must_be_at_least_1(tmp_path: Path):
    """interval_seconds < 1 抛 ValueError。"""
    idx = TaskIndex(tmp_path / "tasks.db")
    with pytest.raises(ValueError):
        TaskScheduler(
            short_term=_StubShortTerm(),  # type: ignore[arg-type]
            task_index=idx,
            segmenter=TaskSegmenter(task_index=idx),
            interval_seconds=0.5,
        )


@pytest.mark.asyncio
async def test_last_trigger_ts_updates_on_real_trigger(tmp_path: Path):
    """真实触发后 last_trigger_ts > 0。"""
    idx = TaskIndex(tmp_path / "tasks.db")
    sched = TaskScheduler(
        short_term=_StubShortTerm(),  # type: ignore[arg-type]
        task_index=idx,
        segmenter=TaskSegmenter(task_index=idx),
        on_message_enabled=True,
        min_interval_seconds=0.0,
    )
    assert sched.last_trigger_ts == 0.0
    t_before = time.monotonic()
    await sched.on_message_received()
    # 等后台 task 跑完确保时间推进
    await asyncio.sleep(0.05)
    assert sched.last_trigger_ts >= t_before
