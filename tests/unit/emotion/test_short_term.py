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
async def test_get_async_saver_works_inside_async_with(tmp_path: Path) -> None:
    """get_async_saver 返回的 wrapper 必须在 async with 内才能用 aput。"""
    from smartbutler.emotion.memory.short_term import _SQLITE_SAVER_AVAILABLE

    if not _SQLITE_SAVER_AVAILABLE:
        pytest.skip("langgraph-checkpoint-sqlite 未装")
    stm = ShortTermMemory(tmp_path / "stm.db")
    saver = await stm.get_async_saver()
    async with saver as s:
        # 关键:有 aput 方法(异步 LangGraph checkpointer 必备)
        assert hasattr(s, "aput")
        assert callable(s.aput)
