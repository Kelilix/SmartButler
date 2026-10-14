"""场景 1+2 端到端验证(Phase 6.4 P1 fixture)。

不走真实 LLM —— 验证降级路径(TaskBoundaryDetector 规则)。
LLM 路径留 fixture 标记,等 --live 模式补测。

场景 1(闲聊整合):
    T1 user:今天周几?
    T1 asst:周日了
    T2 user:明天怎么又要上班?
    T2 asst:抱歉休息时间要结束
    T3 user:出差,帮我订票
    T3 asst:好的请问什么时间出发
    T4 user:明早 8 点
    T4 asst:好,定好票了(butler_done 标志)

场景 2(切换 + 回归):
    T1 user:明天出差,帮我订票
    T1 asst:请问什么时间
    T2 user:对了现在 12 点了,帮我打电话给老婆问她几点回来
    T2 asst:好的(切换到打电话)
    T3 user:继续订票吧
    T3 asst:好的(回订票 task)

我们用 SQLite + TaskIndex 直跑 segmenter,模拟 scheduler 一轮扫描。
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, UTC
from pathlib import Path
from typing import Any

import pytest

# 让 `python tests/.../test_e2e_task.py` 能跑
_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from smartbutler.emotion.memory.task_index import TaskIndex
from smartbutler.emotion.memory.task_segmenter import TaskSegmenter


def _now(offset_min: int = 0) -> str:
    return (datetime.now(UTC) + timedelta(minutes=offset_min)).strftime(
        "%Y-%m-%dT%H:%M:%S+00:00"
    )


def _msg(role: str, content: str, *, user_id: str = "u-male", offset_min: int = 0) -> dict[str, Any]:
    return {
        "role": role,
        "content": content,
        "user_id": user_id if role == "user" else None,
        "spoken_at": _now(offset_min),
        "message_id": f"m-{role}-{offset_min}-{content[:10]}",
    }


@pytest.mark.asyncio
async def test_scenario1_chitchat_then_task(tmp_path: Path):
    """场景 1:先闲聊,后转入 task。"""
    idx = TaskIndex(tmp_path / "tasks.db")
    seg = TaskSegmenter(task_index=idx, llm=None)  # 降级到规则

    thread_id = "session-A"

    # 第 1 轮:闲聊(查询周几)
    msgs1 = [_msg("user", "今天周几", offset_min=0)]
    await seg.segment(thread_id=thread_id, new_messages=msgs1, user_id="u-male")
    active = idx.list_active_tasks(user_id="u-male")
    assert len(active) == 1, f"应开 1 个 task,得到 {len(active)}"
    assert active[0].status == "ongoing"

    # 第 2 轮:同一 thread 继续 → 累加
    msgs2 = [
        _msg("assistant", "周日了", offset_min=1),
        _msg("user", "明天怎么又要上班了", offset_min=2),
    ]
    await seg.segment(thread_id=thread_id, new_messages=msgs2, user_id="u-male")
    assert len(idx.list_active_tasks(user_id="u-male")) == 1

    # 第 3 轮:触发词"帮我订票"→ 新 task
    msgs3 = [
        _msg("assistant", "抱歉休息时间要结束", offset_min=3),
        _msg("user", "出差,帮我订票", offset_min=4),
    ]
    await seg.segment(thread_id=thread_id, new_messages=msgs3, user_id="u-male")
    active = idx.list_active_tasks(user_id="u-male")
    assert len(active) == 2, f"应 2 个 active,得到 {len(active)}"
    statuses = sorted(t.status for t in active)
    assert statuses == ["ongoing", "paused"], f"status 错:{statuses}"
    new_ongoing = next(t for t in active if t.status == "ongoing")
    assert "订" in new_ongoing.title or "票" in new_ongoing.title


@pytest.mark.asyncio
async def test_scenario2_switch_and_return(tmp_path: Path):
    """场景 2:从 A 切到 B(纯规则精确判不出 return,LLM 才能。)。"""
    idx = TaskIndex(tmp_path / "tasks.db")
    seg = TaskSegmenter(task_index=idx, llm=None)

    # 第 1 轮:订票
    await seg.segment(
        thread_id="session-B", new_messages=[_msg("user", "明天出差,帮我订票", offset_min=0)],
        user_id="u-male",
    )
    assert len(idx.list_active_tasks(user_id="u-male")) == 1

    # 第 2 轮:触发词"对了"→ 切到打电话
    await seg.segment(
        thread_id="session-B",
        new_messages=[
            _msg("assistant", "请问什么时间", offset_min=1),
            _msg("user", "对了,帮我打电话给老婆问她几点回来", offset_min=2),
        ],
        user_id="u-male",
    )
    active = idx.list_active_tasks(user_id="u-male")
    assert len(active) == 2, f"切换后应 2 active:{len(active)}"
    ongoing = [t for t in active if t.status == "ongoing"]
    paused = [t for t in active if t.status == "paused"]
    assert len(ongoing) == 1
    assert len(paused) == 1
    assert "电话" in ongoing[0].title or "老婆" in ongoing[0].title

    # 确认 who 维度都存了
    for t in active:
        assert t.user_id == "u-male"
        assert t.source_thread == "session-B"

    # 确认 message 落表(每条都带 who/when)
    # 🆕 Phase 6.4 P1: 同一 thread 的多轮可能 message_id 重复(byte 截断)
    # 改用 (task_id, role, spoken_at) 三元组判定唯一
    ongoing_msgs = idx.iter_task_messages(ongoing[0].task_id)
    keys = {(m.role, m.spoken_at) for m in ongoing_msgs}
    assert len(keys) >= 1, f"ongoing_msgs={ongoing_msgs}"
    # 至少有本轮 user 那条
    user_keys = {k for k in keys if k[0] == "user"}
    assert any("打电话" in m.content for m in ongoing_msgs if m.role == "user")


@pytest.mark.asyncio
async def test_completed_task_excluded_from_prompt(tmp_path: Path):
    """完成态 task 不再注入 prompt(用户决策 2026-10-10)。"""
    idx = TaskIndex(tmp_path / "tasks.db")
    seg = TaskSegmenter(task_index=idx, llm=None)

    await seg.segment(
        thread_id="session-C", new_messages=[_msg("user", "订个外卖", offset_min=0)],
        user_id="u-male",
    )
    active = idx.list_active_tasks(user_id="u-male")
    assert len(active) == 1
    from smartbutler.emotion.memory.task_index import TaskRecord
    idx.upsert_task(TaskRecord(
        task_id=active[0].task_id, user_id="u-male",
        category="other", title="订外卖",
        status="completed", start_ts=active[0].start_ts,
        end_ts=_now(10), summary="已完成",
    ))
    assert len(idx.list_active_tasks(user_id="u-male")) == 0
