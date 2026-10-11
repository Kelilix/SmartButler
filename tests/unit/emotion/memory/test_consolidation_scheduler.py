"""🆕 Phase 6.3+: ConsolidationScheduler 单测。

只验 start/stop 幂等 + 异步触发。无真实 cron 等待(测试用 patch)。
"""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from smartbutler.emotion.memory.consolidation_scheduler import ConsolidationScheduler


@pytest.fixture
def consolidator() -> MagicMock:
    c = MagicMock()
    rep = MagicMock()
    rep.candidates_scanned = 5
    rep.finished_at = "2026-10-11T03:00:00+08:00"

    async def _consolidate(*args: Any, **kwargs: Any) -> Any:
        return rep

    c.consolidate = AsyncMock(side_effect=_consolidate)
    return c


async def test_start_idempotent(consolidator: MagicMock) -> None:
    """多次 start 不抛。"""
    s = ConsolidationScheduler(consolidator=consolidator)
    s.start()
    s.start()  # 第二次不抛
    assert s.is_running


async def test_run_daily_calls_consolidate(consolidator: MagicMock) -> None:
    """start 后直接调内部 _run_daily 验证链路。"""
    s = ConsolidationScheduler(consolidator=consolidator)
    s.start()
    await s._run_daily()
    consolidator.consolidate.assert_awaited_once()
    args, kwargs = consolidator.consolidate.call_args
    assert kwargs.get("level") == "daily"
    assert s.run_count["daily"] == 1


async def test_run_weekly_monthly(consolidator: MagicMock) -> None:
    """weekly/monthly 各自 level 正确。"""
    s = ConsolidationScheduler(consolidator=consolidator)
    s.start()
    await s._run_weekly()
    await s._run_monthly()
    assert consolidator.consolidate.await_count == 2
    levels = [c.kwargs.get("level") for c in consolidator.consolidate.call_args_list]
    assert "weekly" in levels
    assert "monthly" in levels


async def test_consolidate_failure_does_not_crash(consolidator: MagicMock) -> None:
    """consolidate 抛异常 → 计数不增,但 scheduler 自身不挂。"""

    async def _boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("qdrant down")

    consolidator.consolidate = AsyncMock(side_effect=_boom)
    s = ConsolidationScheduler(consolidator=consolidator)
    s.start()
    await s._run_daily()
    # 异常被吞
    assert s.run_count["daily"] == 0
    assert s.is_running  # 仍在跑


async def test_stop_idempotent(consolidator: MagicMock) -> None:
    """重复 stop 不抛。"""
    s = ConsolidationScheduler(consolidator=consolidator)
    s.start()
    await s.stop()
    await s.stop()  # 第二次不抛
    assert not s.is_running
