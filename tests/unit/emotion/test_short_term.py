"""短期记忆(ShortTermMemory)单元测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from smartbutler.emotion.memory import ShortTermMemory


def test_is_persistent_when_sqlite_saver_available() -> None:
    """如果 langgraph-checkpoint-sqlite 装上,is_persistent=True。"""
    from smartbutler.emotion.memory.short_term import _SQLITE_SAVER_AVAILABLE

    if _SQLITE_SAVER_AVAILABLE:
        stm = ShortTermMemory(Path("data/_test_stm.db"))
        assert stm.is_persistent is True
    else:
        pytest.skip("langgraph-checkpoint-sqlite 未装")


def test_path_parent_created(tmp_path: Path) -> None:
    """ShortTermMemory 构造时应创建父目录。"""
    target = tmp_path / "nested" / "stm.db"
    ShortTermMemory(target)
    assert target.parent.exists()


@pytest.mark.asyncio
async def test_get_async_saver_returns_usable_saver(tmp_path: Path) -> None:
    """get_async_saver 直接返回可用的 AsyncSqliteSaver 实例(Phase 6.2 P0)。

    不再要求调用方 ``async with`` 包装 —— Long-lived held pattern。
    """
    from smartbutler.emotion.memory.short_term import _SQLITE_SAVER_AVAILABLE

    if not _SQLITE_SAVER_AVAILABLE:
        pytest.skip("langgraph-checkpoint-sqlite 未装")
    stm = ShortTermMemory(tmp_path / "stm.db")
    try:
        saver = await stm.get_async_saver()
        # 关键:有 aput / aget_tuple 方法(异步 LangGraph checkpointer 必备)
        assert hasattr(saver, "aput")
        assert callable(saver.aput)
        assert hasattr(saver, "aget_tuple")
    finally:
        await stm.aclose()
