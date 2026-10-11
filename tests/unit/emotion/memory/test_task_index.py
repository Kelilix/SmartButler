"""TaskIndex 单元测试(Phase 6.4 P1)。"""
from __future__ import annotations

import sys
from datetime import datetime, UTC
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from smartbutler.emotion.memory.task_index import (
    TaskIndex,
    TaskMessageRecord,
    TaskRecord,
)


@pytest.mark.asyncio
async def test_upsert_and_get_task(tmp_path: Path):
    """upsert + get round-trip。"""
    idx = TaskIndex(tmp_path / "tasks.db")
    rec = TaskRecord(
        task_id="t-001", user_id="u1", category="booking",
        title="订机票", status="ongoing",
        start_ts="2026-10-10T22:00:00+00:00",
    )
    idx.upsert_task(rec)
    fetched = idx.get_task("t-001")
    assert fetched is not None
    assert fetched.title == "订机票"
    assert fetched.user_id == "u1"
    assert fetched.status == "ongoing"


@pytest.mark.asyncio
async def test_participants_serialization(tmp_path: Path):
    """participants 列表正确序列化。"""
    idx = TaskIndex(tmp_path / "tasks.db")
    idx.upsert_task(TaskRecord(
        task_id="t-002", user_id="u1", category="booking",
        title="家庭", status="ongoing",
        start_ts="2026-10-10T22:00:00+00:00",
        participants=["男主人", "女主人"],
    ))
    fetched = idx.get_task("t-002")
    assert fetched.participants == ["男主人", "女主人"]


@pytest.mark.asyncio
async def test_list_active_excludes_completed(tmp_path: Path):
    """active 列表只返 ongoing/paused,completed 排除。"""
    idx = TaskIndex(tmp_path / "tasks.db")
    idx.upsert_task(TaskRecord(task_id="t1", user_id="u1", category="a", title="ongoing", status="ongoing", start_ts="2026-10-10T22:00:00+00:00"))
    idx.upsert_task(TaskRecord(task_id="t2", user_id="u1", category="a", title="paused", status="paused", start_ts="2026-10-10T22:01:00+00:00"))
    idx.upsert_task(TaskRecord(task_id="t3", user_id="u1", category="a", title="completed", status="completed", start_ts="2026-10-10T22:02:00+00:00", end_ts="2026-10-10T22:03:00+00:00"))
    idx.upsert_task(TaskRecord(task_id="t4", user_id="u1", category="a", title="abandoned", status="abandoned", start_ts="2026-10-10T22:04:00+00:00"))
    active = idx.list_active_tasks(user_id="u1")
    assert len(active) == 2
    assert {t.task_id for t in active} == {"t1", "t2"}


@pytest.mark.asyncio
async def test_message_append_no_dup(tmp_path: Path):
    """(task_id, message_id) 主键去重。"""
    idx = TaskIndex(tmp_path / "tasks.db")
    idx.upsert_task(TaskRecord(task_id="t1", user_id="u1", category="a", title="t", status="ongoing", start_ts="2026-10-10T22:00:00+00:00"))
    msg = TaskMessageRecord(
        task_id="t1", message_id="m-001", thread_id="s1",
        role="user", user_id="u1", spoken_at="2026-10-10T22:00:00+00:00",
        content="你好",
    )
    idx.append_message(msg)
    idx.append_message(msg)  # 重复插入
    idx.append_message(msg)  # 重复插入
    assert idx.count_messages("t1") == 1


@pytest.mark.asyncio
async def test_iter_task_messages_ordered(tmp_path: Path):
    """iter_task_messages 按 spoken_at 升序。"""
    idx = TaskIndex(tmp_path / "tasks.db")
    idx.upsert_task(TaskRecord(task_id="t1", user_id="u1", category="a", title="t", status="ongoing", start_ts="2026-10-10T22:00:00+00:00"))
    # 故意乱序插入
    for ts, content in [
        ("2026-10-10T22:02:00+00:00", "b"),
        ("2026-10-10T22:00:00+00:00", "a"),
        ("2026-10-10T22:01:00+00:00", "b-mid"),
    ]:
        idx.append_message(TaskMessageRecord(
            task_id="t1", message_id=f"m-{ts}", thread_id="s1",
            role="user", user_id="u1", spoken_at=ts, content=content,
        ))
    msgs = idx.iter_task_messages("t1")
    assert [m.content for m in msgs] == ["a", "b-mid", "b"]


@pytest.mark.asyncio
async def test_stats(tmp_path: Path):
    """stats 返正确数字。"""
    idx = TaskIndex(tmp_path / "tasks.db")
    idx.upsert_task(TaskRecord(task_id="t1", user_id="u1", category="a", title="t", status="ongoing", start_ts="2026-10-10T22:00:00+00:00"))
    idx.append_message(TaskMessageRecord(task_id="t1", message_id="m1", thread_id="s1", role="user", user_id="u1", spoken_at="2026-10-10T22:00:00+00:00", content="x"))
    s = idx.stats()
    assert s["tasks_total"] == 1
    assert s["messages_total"] == 1
    assert s["active_tasks"] == 1
    assert "tasks.db" in s["db_path"]
