"""ShortTermView Phase 6.4 P2 单测 —— 完成态 + 闲聊摘要注入。"""
from __future__ import annotations

import sys
from datetime import datetime, UTC
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from smartbutler.emotion.memory.short_term_view import (
    ShortTermView,
    TaskWindow,
    build_short_term_prompt_block,
)
from smartbutler.emotion.memory.task_index import TaskIndex, TaskRecord


class _StubSaver:
    """最小化 AsyncSqliteSaver stub —— 只暴露 ShortTermView 用到的接口。"""

    def __init__(self, threads: dict[str, list[dict[str, Any]]] | None = None) -> None:
        self._threads = threads or {}

    async def alist(self, *, config: Any = None, limit: int = 100) -> Any:  # noqa: ARG002
        for tid in self._threads:
            yield _CP(tid, ts="2026-10-10T22:00:00+00:00")

    async def aget(self, config: dict[str, Any]) -> dict[str, Any] | None:
        tid = config.get("configurable", {}).get("thread_id", "")
        msgs = self._threads.get(tid)
        if msgs is None:
            return None
        return {"channel_values": {"messages": msgs}}


class _CP:
    """Checkpoint tuple stub。"""
    def __init__(self, thread_id: str, ts: str) -> None:
        self.config = {"configurable": {"thread_id": thread_id}}
        self.checkpoint = {"id": f"cp-{thread_id}", "ts": ts}


def _now(offset_min: int = 0) -> str:
    from datetime import timedelta
    return (datetime.now(UTC) + timedelta(minutes=offset_min)).strftime(
        "%Y-%m-%dT%H:%M:%S+00:00"
    )


def _task(task_id: str, status: str, **kw: Any) -> TaskRecord:
    defaults: dict[str, Any] = dict(
        user_id="u1", category="other", title="t", start_ts=_now(0),
        end_ts=None, summary="",
    )
    defaults.update(kw)
    return TaskRecord(task_id=task_id, status=status, **defaults)


@pytest.mark.asyncio
async def test_window_includes_completed_and_chitchat_summaries(tmp_path: Path):
    """build_window 返回的 window 含 completed_task_summaries + chitchat_task_summaries。"""
    idx = TaskIndex(tmp_path / "tasks.db")
    # 完成态 2 个
    idx.upsert_task(_task("c1", "completed", title="订机票", summary="已订好 10/11",
                          end_ts=_now(60), source_thread="s-A"))
    idx.upsert_task(_task("c2", "abandoned", title="打电话", summary="未接通",
                          end_ts=_now(120), source_thread="s-B"))
    # 闲聊 2 个
    idx.upsert_task(_task("k1", "chitchat_done", title="周末闲聊",
                          end_ts=_now(180), source_thread="s-C"))
    idx.upsert_task(_task("k2", "chitchat_done", title="情绪问候",
                          end_ts=_now(240), source_thread="s-D"))
    # 当前 ongoing
    idx.upsert_task(_task("now", "ongoing", title="订外卖",
                          source_thread="s-current"))

    saver = _StubSaver()
    view = ShortTermView(
        saver=saver, task_index=idx,
        n_completed_tasks=3, n_chitchat_tasks=30, n_prior_tasks=3,
    )
    window = await view.build_window(
        current_session_id="s-current", current_user_input="订个外卖",
        user_id="u1",
    )

    assert len(window.completed_task_summaries) == 2
    assert {s.thread_id for s in window.completed_task_summaries} == {"s-A", "s-B"}
    assert len(window.chitchat_task_summaries) == 2
    assert {s.thread_id for s in window.chitchat_task_summaries} == {"s-C", "s-D"}


@pytest.mark.asyncio
async def test_window_respects_n_completed_limit(tmp_path: Path):
    """n_completed_tasks=1 只取 1 条完成态。"""
    idx = TaskIndex(tmp_path / "tasks.db")
    for i in range(5):
        idx.upsert_task(_task(f"c{i}", "completed", title=f"task{i}",
                              end_ts=_now(60 + i * 10), source_thread=f"s-{i}"))
    saver = _StubSaver()
    view = ShortTermView(
        saver=saver, task_index=idx,
        n_completed_tasks=1, n_chitchat_tasks=30, n_prior_tasks=3,
    )
    window = await view.build_window(
        current_session_id="any", current_user_input="x", user_id="u1",
    )
    assert len(window.completed_task_summaries) == 1


@pytest.mark.asyncio
async def test_window_respects_n_chitchat_limit(tmp_path: Path):
    """n_chitchat_tasks=2 只取 2 条闲聊。"""
    idx = TaskIndex(tmp_path / "tasks.db")
    for i in range(5):
        idx.upsert_task(_task(f"k{i}", "chitchat_done", title=f"chit{i}",
                              end_ts=_now(60 + i * 10), source_thread=f"s-{i}"))
    saver = _StubSaver()
    view = ShortTermView(
        saver=saver, task_index=idx,
        n_completed_tasks=3, n_chitchat_tasks=2, n_prior_tasks=3,
    )
    window = await view.build_window(
        current_session_id="any", current_user_input="x", user_id="u1",
    )
    assert len(window.chitchat_task_summaries) == 2


@pytest.mark.asyncio
async def test_window_call_time_overrides_constructor(tmp_path: Path):
    """call-time n_completed_tasks 覆盖构造参数。"""
    idx = TaskIndex(tmp_path / "tasks.db")
    for i in range(5):
        idx.upsert_task(_task(f"c{i}", "completed", title=f"task{i}",
                              end_ts=_now(60 + i), source_thread=f"s-{i}"))
    saver = _StubSaver()
    view = ShortTermView(
        saver=saver, task_index=idx,
        n_completed_tasks=3, n_chitchat_tasks=30, n_prior_tasks=3,
    )
    # call-time 覆盖:取 2 条
    window = await view.build_window(
        current_session_id="any", current_user_input="x", user_id="u1",
        n_completed_tasks=2,
    )
    assert len(window.completed_task_summaries) == 2


def test_prompt_block_contains_completed_section():
    """prompt block 含"已完成 task 摘要"段落。"""
    window = TaskWindow(
        completed_task_summaries=[_summary_stub(thread="s-A", user="订机票", asst="已订好")],
    )
    block = build_short_term_prompt_block(window)
    assert "已完成 task 摘要" in block
    assert "订机票" in block
    assert "已订好" in block


def test_prompt_block_contains_chitchat_section():
    """prompt block 含"闲聊记录"段落。"""
    window = TaskWindow(
        chitchat_task_summaries=[_summary_stub(thread="s-B", user="周末闲聊", asst="周末闲聊")],
    )
    block = build_short_term_prompt_block(window)
    assert "闲聊记录" in block
    assert "周末闲聊" in block


def test_prompt_block_empty_when_no_content():
    """window 全空 → 返空串(不污染 prompt)。"""
    window = TaskWindow()
    assert build_short_term_prompt_block(window) == ""


def test_prompt_block_all_four_sections():
    """当前 task + paused + completed + chitchat 四段全拼。"""
    from smartbutler.emotion.memory.short_term_view import ThreadSummary
    window = TaskWindow(
        current_task_messages=[{"role": "user", "content": "hi", "timestamp": _now(0), "user_id": "u1"}],
        prior_task_summaries=[ThreadSummary(thread_id="p1", user_id="u1",
                                            last_user_message="u", last_assistant_message="a",
                                            last_timestamp=_now(0), total_turns=1)],
        completed_task_summaries=[ThreadSummary(thread_id="c1", user_id="u1",
                                                last_user_message="u2", last_assistant_message="a2",
                                                last_timestamp=_now(0), total_turns=1)],
        chitchat_task_summaries=[ThreadSummary(thread_id="k1", user_id="u1",
                                                last_user_message="k", last_assistant_message="k",
                                                last_timestamp=_now(0), total_turns=1)],
    )
    block = build_short_term_prompt_block(window)
    assert "短期记忆" in block
    assert "跨 task 锚点" in block
    assert "已完成 task 摘要" in block
    assert "闲聊记录" in block


def _summary_stub(*, thread: str, user: str, asst: str) -> Any:
    from smartbutler.emotion.memory.short_term_view import ThreadSummary
    return ThreadSummary(
        thread_id=thread, user_id="u1",
        last_user_message=user, last_assistant_message=asst,
        last_timestamp=_now(0), total_turns=1,
    )
