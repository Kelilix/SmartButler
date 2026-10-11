"""TaskIndex 增强单测(Phase 6.4 P2)—— list_completed_tasks / list_chitchat_tasks。"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, UTC
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from smartbutler.emotion.memory.task_index import TaskIndex, TaskRecord


def _now(offset_min: int = 0) -> str:
    return (datetime.now(UTC) + timedelta(minutes=offset_min)).strftime(
        "%Y-%m-%dT%H:%M:%S+00:00"
    )


def _task(
    *, task_id: str, user_id: str, status: str, category: str = "other",
    title: str = "t", start_offset: int = 0, end_offset: int | None = None,
    summary: str = "",
) -> TaskRecord:
    return TaskRecord(
        task_id=task_id, user_id=user_id, category=category, title=title,
        status=status, start_ts=_now(start_offset),
        end_ts=_now(end_offset) if end_offset is not None else None,
        summary=summary,
    )


@pytest.mark.asyncio
async def test_list_completed_tasks_excludes_ongoing_paused_chitchat(tmp_path: Path):
    """list_completed_tasks 只返 completed + (可选)abandoned,排除 ongoing/paused/chitchat。"""
    idx = TaskIndex(tmp_path / "tasks.db")
    idx.upsert_task(_task(task_id="t1", user_id="u1", status="ongoing", start_offset=0))
    idx.upsert_task(_task(task_id="t2", user_id="u1", status="paused", start_offset=1))
    idx.upsert_task(_task(task_id="t3", user_id="u1", status="completed", start_offset=2, end_offset=3))
    idx.upsert_task(_task(task_id="t4", user_id="u1", status="abandoned", start_offset=4, end_offset=5))
    idx.upsert_task(_task(task_id="t5", user_id="u1", status="chitchat_done", start_offset=6, end_offset=7))

    res = idx.list_completed_tasks(limit=10, user_id="u1", include_abandoned=True)
    ids = {t.task_id for t in res}
    assert ids == {"t3", "t4"}, f"应只含 completed + abandoned,实际={ids}"


@pytest.mark.asyncio
async def test_list_completed_tasks_excludes_abandoned_when_disabled(tmp_path: Path):
    """include_abandoned=False 时,abandoned 也不进。"""
    idx = TaskIndex(tmp_path / "tasks.db")
    idx.upsert_task(_task(task_id="t3", user_id="u1", status="completed", start_offset=2, end_offset=3))
    idx.upsert_task(_task(task_id="t4", user_id="u1", status="abandoned", start_offset=4, end_offset=5))
    res = idx.list_completed_tasks(limit=10, user_id="u1", include_abandoned=False)
    ids = {t.task_id for t in res}
    assert ids == {"t3"}, f"应只含 completed,实际={ids}"


@pytest.mark.asyncio
async def test_list_completed_tasks_sort_by_end_ts_desc(tmp_path: Path):
    """按 end_ts 倒序(刚完成的优先)。"""
    idx = TaskIndex(tmp_path / "tasks.db")
    idx.upsert_task(_task(task_id="old", user_id="u1", status="completed",
                          start_offset=0, end_offset=10, title="old"))
    idx.upsert_task(_task(task_id="new", user_id="u1", status="completed",
                          start_offset=100, end_offset=200, title="new"))
    res = idx.list_completed_tasks(limit=10, user_id="u1")
    assert [t.task_id for t in res] == ["new", "old"]


@pytest.mark.asyncio
async def test_list_completed_tasks_limit(tmp_path: Path):
    """limit 限制返回条数。"""
    idx = TaskIndex(tmp_path / "tasks.db")
    for i in range(5):
        idx.upsert_task(_task(task_id=f"t{i}", user_id="u1", status="completed",
                              start_offset=i, end_offset=i + 1, title=f"task{i}"))
    res = idx.list_completed_tasks(limit=2, user_id="u1")
    assert len(res) == 2
    assert {t.task_id for t in res} == {"t3", "t4"}


@pytest.mark.asyncio
async def test_list_completed_tasks_user_filter(tmp_path: Path):
    """user_id 过滤生效。"""
    idx = TaskIndex(tmp_path / "tasks.db")
    idx.upsert_task(_task(task_id="t1", user_id="u1", status="completed", end_offset=1))
    idx.upsert_task(_task(task_id="t2", user_id="u2", status="completed", end_offset=1))
    res_u1 = idx.list_completed_tasks(limit=10, user_id="u1")
    res_u2 = idx.list_completed_tasks(limit=10, user_id="u2")
    assert {t.task_id for t in res_u1} == {"t1"}
    assert {t.task_id for t in res_u2} == {"t2"}


@pytest.mark.asyncio
async def test_list_chitchat_tasks_only(tmp_path: Path):
    """list_chitchat_tasks 只返 chitchat_done。"""
    idx = TaskIndex(tmp_path / "tasks.db")
    idx.upsert_task(_task(task_id="t1", user_id="u1", status="ongoing"))
    idx.upsert_task(_task(task_id="t2", user_id="u1", status="completed", end_offset=1))
    idx.upsert_task(_task(task_id="t3", user_id="u1", status="chitchat_done", end_offset=2, title="周末闲聊"))
    idx.upsert_task(_task(task_id="t4", user_id="u1", status="chitchat_done", end_offset=3, title="情绪问候"))
    res = idx.list_chitchat_tasks(limit=10, user_id="u1")
    ids = {t.task_id for t in res}
    assert ids == {"t3", "t4"}


@pytest.mark.asyncio
async def test_list_chitchat_tasks_sort_by_end_ts_desc(tmp_path: Path):
    """按 end_ts 倒序(最近的闲聊优先)。"""
    idx = TaskIndex(tmp_path / "tasks.db")
    idx.upsert_task(_task(task_id="old", user_id="u1", status="chitchat_done",
                          start_offset=0, end_offset=10, title="old chitchat"))
    idx.upsert_task(_task(task_id="new", user_id="u1", status="chitchat_done",
                          start_offset=100, end_offset=200, title="new chitchat"))
    res = idx.list_chitchat_tasks(limit=10, user_id="u1")
    assert [t.task_id for t in res] == ["new", "old"]


@pytest.mark.asyncio
async def test_list_chitchat_tasks_limit_30_default(tmp_path: Path):
    """默认 limit 30(模拟生产:闲聊很多,只取最近 30)。"""
    idx = TaskIndex(tmp_path / "tasks.db")
    for i in range(50):
        idx.upsert_task(_task(task_id=f"t{i:03d}", user_id="u1", status="chitchat_done",
                              start_offset=i, end_offset=i + 1, title=f"chit{i}"))
    res = idx.list_chitchat_tasks(limit=30, user_id="u1")
    assert len(res) == 30
