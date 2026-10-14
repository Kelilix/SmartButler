"""TaskScheduler 游标持久化回归测试(Phase 6.5, 修复 2026-10-12)。

Bug:dev_chat 重启后 _processed_ids 为空,
导致 saver.alist() 返回的所有 thread 被重新 judge,旧消息重复进 task。

Fix:游标持久化到 task_index.db,start() 时 load,scan 结束后 save。
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from smartbutler.emotion.memory.task_index import TaskIndex


class TestTaskIndexCursor:
    """TaskIndex 游标读写基本正确性。"""

    def test_save_and_load_roundtrip(self, tmp_path: Path) -> None:
        """save → load 往返数据不变。"""
        idx = TaskIndex(tmp_path / "cursor.db")

        original = {
            "thread-1": {"m1", "m2", "m3"},
            "thread-2": {"m10", "m20"},
        }
        idx.save_processed_cursor(original)
        loaded = idx.load_processed_cursor()

        assert loaded == original

    def test_load_empty_on_fresh_db(self, tmp_path: Path) -> None:
        """新 db(无游标记录)加载返回空 dict。"""
        idx = TaskIndex(tmp_path / "fresh.db")
        assert idx.load_processed_cursor() == {}

    def test_load_ignores_malformed_json(self, tmp_path: Path) -> None:
        """损坏的 JSON 条目被跳过,不影响其他 thread。"""
        idx = TaskIndex(tmp_path / "corrupt.db")

        # 手动写一条损坏的
        import sqlite3
        conn = sqlite3.connect(str(tmp_path / "corrupt.db"))
        conn.execute(
            "INSERT OR IGNORE INTO _processed_cursor VALUES (?, ?)",
            ("bad-thread", "not json at all {{{"),
        )
        conn.execute(
            "INSERT OR IGNORE INTO _processed_cursor VALUES (?, ?)",
            ("good-thread", '["m1","m2"]'),
        )
        conn.commit()
        conn.close()

        loaded = idx.load_processed_cursor()
        assert "bad-thread" not in loaded
        assert loaded.get("good-thread") == {"m1", "m2"}

    def test_save_overwrites_previous(self, tmp_path: Path) -> None:
        """连续 save 是原子覆盖,不是 append。"""
        idx = TaskIndex(tmp_path / "overwrite.db")

        idx.save_processed_cursor({"t1": {"a", "b"}})
        idx.save_processed_cursor({"t2": {"c"}})  # t1 应该消失

        loaded = idx.load_processed_cursor()
        assert "t1" not in loaded
        assert loaded.get("t2") == {"c"}


class TestSchedulerCursorPersistence:
    """TaskScheduler 生命周期内游标持久化正确性。"""

    @pytest.mark.asyncio
    async def test_start_loads_cursor(self, tmp_path: Path) -> None:
        """start() 从 task_index 加载游标到 _processed_ids。"""
        from smartbutler.emotion.memory.task_scheduler import TaskScheduler

        idx = TaskIndex(tmp_path / "idx.db")
        # 预存游标
        idx.save_processed_cursor({
            "thread-A": {"msg-1", "msg-2"},
            "thread-B": {"msg-10"},
        })

        sched = TaskScheduler(
            short_term=MagicMock(),
            task_index=idx,
            segmenter=MagicMock(),
        )

        # mock start() 里不需要真正起 loop task
        with patch.object(sched, "_loop", AsyncMock(return_value=None)):
            await sched.start()

        assert sched._processed_ids == {
            "thread-A": {"msg-1", "msg-2"},
            "thread-B": {"msg-10"},
        }
        await sched.stop()

    @pytest.mark.asyncio
    async def test_scan_saves_cursor(self, tmp_path: Path) -> None:
        """scan 结束后游标写回 task_index。"""
        from smartbutler.emotion.memory.task_scheduler import TaskScheduler

        idx = TaskIndex(tmp_path / "idx2.db")
        sched = TaskScheduler(
            short_term=MagicMock(),
            task_index=idx,
            segmenter=MagicMock(),
        )
        sched._processed_ids = {"t-X": {"m-A", "m-B"}}

        # mock scan 流程(不调 LLM)
        saver = AsyncMock()
        saver.alist = MagicMock(return_value=iter([]))  # 空 thread 列表
        sched._short_term.get_async_saver = AsyncMock(return_value=saver)

        await sched._scan_inner()

        loaded = idx.load_processed_cursor()
        assert loaded.get("t-X") == {"m-A", "m-B"}

    @pytest.mark.asyncio
    async def test_stop_saves_cursor(self, tmp_path: Path) -> None:
        """stop() 把当前游标写回磁盘。"""
        from smartbutler.emotion.memory.task_scheduler import TaskScheduler

        idx = TaskIndex(tmp_path / "idx3.db")
        sched = TaskScheduler(
            short_term=MagicMock(),
            task_index=idx,
            segmenter=MagicMock(),
        )

        # 先 save 游标(模拟前一次运行留下),再 start 加载它
        idx.save_processed_cursor({"t-stop": {"msg-final"}})
        with patch.object(sched, "_loop", AsyncMock(return_value=None)):
            await sched.start()

        # stop 应该把加载的值写回(值没变,所以 loaded == {"t-stop": {"msg-final"}})
        await sched.stop()
        loaded = idx.load_processed_cursor()
        assert loaded.get("t-stop") == {"msg-final"}

    @pytest.mark.asyncio
    async def test_cursor_accumulates_across_scans(self, tmp_path: Path) -> None:
        """多次 scan 期间游标累加,stop 时全部写回。"""
        from smartbutler.emotion.memory.task_scheduler import TaskScheduler

        idx = TaskIndex(tmp_path / "idx4.db")
        sched = TaskScheduler(
            short_term=MagicMock(),
            task_index=idx,
            segmenter=MagicMock(),
        )

        saver = AsyncMock()
        saver.alist = MagicMock(return_value=iter([]))
        sched._short_term.get_async_saver = AsyncMock(return_value=saver)

        # 模拟两次 scan,每次推进一个 thread 的游标
        sched._processed_ids = {"t1": {"msg-1"}}
        await sched._scan_inner()

        sched._processed_ids = {"t1": {"msg-1", "msg-2"}, "t2": {"msg-a"}}
        await sched._scan_inner()

        await sched.stop()
        loaded = idx.load_processed_cursor()
        assert loaded.get("t1") == {"msg-1", "msg-2"}
        assert loaded.get("t2") == {"msg-a"}
