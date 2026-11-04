"""🆕 2026-11-02: 归属贯穿不变式测试。

核心不变式(**代码承担事实,LLM 承担表达**):

    归属(谁说的)必须由代码以结构化 tag 落库,
    并在**每一层升格时由代码重新写一遍**。
    LLM 只负责正文措辞,归属永远不由 LLM 生成。

测试覆盖:
1. **fact 层** —— 按说话人拆 fact,共享 task tag,归属 = 说话人
2. **转述不误判** —— "我妈今天也去上海" 归属说话人,不是话语里的人
3. **升格分组** —— 按主体(user)分桶,task 作段落锚点
4. **共享 task 裁决** —— 多人参与同一 task → 归主说话人,不割裂
5. **跨层归属不丢** —— fact → 日报 → 周报,user/task tag 都在
6. **产出量正确** —— 3 个 task 2 个人 → 2 条日报(不是 3 条,也不是 1 条)
7. **召回渲染** —— 按 task 分组 + 显示名
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from smartbutler.emotion.memory.auto_extract import AutoExtractHook
from smartbutler.emotion.memory.consolidation import Consolidator
from smartbutler.emotion.memory.hooks import MemoryHooks
from smartbutler.emotion.memory.levels import (
    parse_participants_tag,
    parse_task_tags,
    parse_user_tag,
    participants_tag,
    task_tag,
    user_tag,
)
from smartbutler.emotion.memory.task_index import TaskIndex, TaskMessageRecord

# ------------------- Helpers -------------------


def _make_facade() -> MagicMock:
    """造一个记录 remember 调用的 facade mock。"""
    f = MagicMock()
    f.remember = AsyncMock(return_value="key")
    f.hooks = MemoryHooks()
    return f


def _add_msg(
    task_index: TaskIndex,
    *,
    task_id: str,
    message_id: str,
    user_id: str,
    content: str,
    role: str = "user",
) -> None:
    task_index.append_message(
        TaskMessageRecord(
            task_id=task_id,
            message_id=message_id,
            thread_id="thread-1",
            role=role,
            user_id=user_id,
            spoken_at="2026-01-01T00:00:00+00:00",
            content=content,
        )
    )


async def _drain() -> None:
    """等 fire-and-forget 任务跑完。"""
    for _ in range(20):
        await asyncio.sleep(0)
        pending = [
            t for t in asyncio.all_tasks() if t.get_name().startswith("auto-extract")
        ]
        if not pending:
            return
        await asyncio.gather(*pending, return_exceptions=True)


# ===========================================================================
# 1. fact 层:按说话人拆,共享 task tag
# ===========================================================================


async def test_fact_split_by_speaker_shared_task_tag(tmp_path: Path) -> None:
    """多人 task → 多条 fact,共享 task tag,各自 user tag。"""
    task_index = TaskIndex(tmp_path / "tasks.db")
    facade = _make_facade()
    hook = AutoExtractHook(
        facade=facade,
        short_term=MagicMock(),
        task_index=task_index,
        min_importance=0.0,
    )

    _add_msg(task_index, task_id="t1", message_id="m1", user_id="u_dad",
             content="我明天要去上海")
    _add_msg(task_index, task_id="t1", message_id="m2", user_id="u_mom",
             content="我也去")
    _add_msg(task_index, task_id="t1", message_id="m3", user_id="u_dad",
             content="我妈也去上海")

    await hook._extract_and_store(
        task_id="t1", category="booking", title="订机票",
        status="completed", thread_id="thread-1",
    )

    assert facade.remember.await_count == 2, "应产出 2 条 fact(每个说话人一条)"

    calls = facade.remember.await_args_list
    tags_by_user: dict[str, list[str]] = {}
    for c in calls:
        tags = c.kwargs["tags"]
        uid = parse_user_tag(tags)
        assert uid is not None, "每条 fact 都必须有 user tag"
        tags_by_user[uid] = tags

    assert set(tags_by_user) == {"u_dad", "u_mom"}

    # 两条 fact 共享同一个 task tag —— task 是工作单元锚点
    for tags in tags_by_user.values():
        assert task_tag("t1") in tags
        assert "task:t1" in tags

    # 参与者被记录
    p = parse_participants_tag(tags_by_user["u_dad"])
    assert set(p) == {"u_dad", "u_mom"}


async def test_attribution_is_speaker_not_mentioned_person(tmp_path: Path) -> None:
    """🆕 核心:转述的置信度问题。

    "我妈今天也去上海" 是**高天禹说的**,事实归属高天禹,
    不是他妈妈 —— 否则就把"谁说的"和"说的是谁的事"混为一谈。
    """
    task_index = TaskIndex(tmp_path / "tasks.db")
    facade = _make_facade()
    hook = AutoExtractHook(
        facade=facade, short_term=MagicMock(), task_index=task_index,
        min_importance=0.0,
    )

    _add_msg(task_index, task_id="t1", message_id="m1", user_id="u_dad",
             content="我妈今天也去上海")

    await hook._extract_and_store(
        task_id="t1", category="chitchat", title="闲聊",
        status="chitchat_done", thread_id="thread-1",
    )

    assert facade.remember.await_count == 1
    tags = facade.remember.await_args.kwargs["tags"]
    # 归属 = 说话人
    assert parse_user_tag(tags) == "u_dad"
    # 不得出现一个"妈妈"这种从话语里解析出来的归属
    assert "u_mom" not in tags
    # 原话保留在正文里(LLM 读正文时自己判断指代)
    content = facade.remember.await_args.args[0]
    assert "我妈今天也去上海" in content


async def test_assistant_message_attaches_to_previous_speaker(tmp_path: Path) -> None:
    """assistant 回复归给上一位 user(管家在回应谁)。"""
    task_index = TaskIndex(tmp_path / "tasks.db")
    facade = _make_facade()
    hook = AutoExtractHook(
        facade=facade, short_term=MagicMock(), task_index=task_index,
        min_importance=0.0,
    )

    _add_msg(task_index, task_id="t1", message_id="m1", user_id="u_dad",
             content="订张票")
    _add_msg(task_index, task_id="t1", message_id="m2", user_id="",
             content="好的,已订好", role="assistant")

    await hook._extract_and_store(
        task_id="t1", category="booking", title="订票",
        status="completed", thread_id="thread-1",
    )

    assert facade.remember.await_count == 1
    tags = facade.remember.await_args.kwargs["tags"]
    assert parse_user_tag(tags) == "u_dad"
    content = facade.remember.await_args.args[0]
    assert "[assistant] 好的,已订好" in content


# ===========================================================================
# 2. 升格分组
# ===========================================================================


def _make_consolidator() -> Consolidator:
    """造一个 Consolidator(embedder 走 stub,不依赖外部模型)。"""
    store = MagicMock()
    store.store_memory = MagicMock(return_value=None)

    class _Emb:
        async def embed(self, text: str) -> list[float]:
            return [0.0] * 1024

    return Consolidator(store=store, embedder=_Emb())  # type: ignore[arg-type]


def _fact(key: str, content: str, tags: list[str], importance: float = 0.5) -> dict[str, Any]:
    return {
        "key": key,
        "content": content,
        "importance": importance,
        "created_at": "2026-11-01T10:00:00+00:00",
        "tags": tags,
    }


def test_group_by_subject_buckets_by_user_not_task() -> None:
    """3 个 task、2 个人 → 2 组(按人分桶,不是按 task)。"""
    c = _make_consolidator()
    facts = [
        _fact("f1", "A 内容", [task_tag("t1"), user_tag("u_dad")]),
        _fact("f2", "B 内容", [task_tag("t2"), user_tag("u_dad")]),
        _fact("f3", "C 内容", [task_tag("t3"), user_tag("u_mom")]),
    ]
    groups = c._group_by_subject(facts)

    assert len(groups) == 2, "应产出 2 组(每人一条日报)"
    users = {g.primary_user for g in groups}
    assert users == {"u_dad", "u_mom"}

    dad = next(g for g in groups if g.primary_user == "u_dad")
    # 爸爸的组里含 2 个 task(日报内部的段落锚点)
    assert set(dad.task_ids) == {"t1", "t2"}
    # 段落数 = task 数
    assert len(dad.segments) == 2


def test_group_shared_task_goes_to_primary_speaker() -> None:
    """同一 task 多人参与 → 归主说话人(消息多的),不割裂成两条。"""
    c = _make_consolidator()
    facts = [
        _fact("f1", "A", [task_tag("t1"), user_tag("u_dad")]),
        _fact("f2", "B", [task_tag("t1"), user_tag("u_dad")]),
        _fact("f3", "C", [task_tag("t1"), user_tag("u_mom")]),
    ]
    groups = c._group_by_subject(facts)

    assert len(groups) == 1, "共享 task 不应被切成两条日报"
    assert groups[0].primary_user == "u_dad", "主说话人 = 消息数多的"
    # 参与者仍然记录完整
    assert set(groups[0].participants) == {"u_dad", "u_mom"}


def test_group_unknown_user_falls_into_unknown_bucket() -> None:
    """无归属 fact(老数据)归 unknown 组,不丢弃也不污染他人。"""
    c = _make_consolidator()
    facts = [
        _fact("f1", "无 tag", []),
        _fact("f2", "有归属", [user_tag("u_dad")]),
    ]
    groups = c._group_by_subject(facts)

    assert len(groups) == 2
    users = {g.primary_user for g in groups}
    assert "unknown" in users
    assert "u_dad" in users


def test_group_segments_follow_task_order() -> None:
    """段落按 task 首次出现顺序(日报里的项目顺序稳定)。"""
    c = _make_consolidator()
    facts = [
        _fact("f1", "A", [task_tag("t1"), user_tag("u")]),
        _fact("f2", "B", [task_tag("t2"), user_tag("u")]),
        _fact("f3", "C", [task_tag("t1"), user_tag("u")]),
    ]
    groups = c._group_by_subject(facts)
    seg_task_ids = [tid for tid, _ in groups[0].segments]

    assert seg_task_ids == ["t1", "t2"]
    # t1 段里有 2 条 fact
    assert len(groups[0].segments[0][1]) == 2


# ===========================================================================
# 3. 跨层归属不丢
# ===========================================================================


async def test_write_summary_writes_attribution_tags() -> None:
    """升格写库时,**由代码**写入 user/task/participants tag。"""
    from smartbutler.emotion.memory.levels import MemoryLevel

    c = _make_consolidator()
    groups = c._group_by_subject([
        _fact("f1", "A", [task_tag("t1"), user_tag("u_dad")]),
    ])

    key = await c._write_summary(
        summary="摘要内容",
        target_level=MemoryLevel.DAILY,
        source_keys=["f1"],
        importance=0.6,
        period="昨天",
        source_count=1,
        group=groups[0],
    )

    assert key is not None
    # 断言 store_memory 收到的 tags
    call = c._store.store_memory.call_args
    tags = call.kwargs["tags"]
    assert "user:u_dad" in tags
    assert "task:t1" in tags
    # key 里带归属,人工排查一眼看出是谁的
    assert "u_dad" in key


async def test_attribution_survives_three_levels() -> None:
    """fact → 日报 → 周报:归属逐层不丢。"""
    c = _make_consolidator()
    from smartbutler.emotion.memory.levels import MemoryLevel

    # L1: fact
    facts = [_fact("f1", "A", [task_tag("t1"), user_tag("u_dad")])]
    groups = c._group_by_subject(facts)

    # L2: 日报
    daily_key = await c._write_summary(
        summary="日", target_level=MemoryLevel.DAILY, source_keys=["f1"],
        importance=0.6, period="昨天", source_count=1, group=groups[0],
    )
    assert daily_key is not None
    daily_tags = c._store.store_memory.call_args.kwargs["tags"]
    assert "user:u_dad" in daily_tags
    assert "task:t1" in daily_tags

    # L3: 周报 —— 输入是日报的 tags
    weekly_facts = [_fact(daily_key, "日报内容", daily_tags)]
    weekly_groups = c._group_by_subject(weekly_facts)
    weekly_key = await c._write_summary(
        summary="周", target_level=MemoryLevel.WEEKLY, source_keys=[daily_key],
        importance=0.6, period="本周", source_count=1, group=weekly_groups[0],
    )

    assert weekly_key is not None
    weekly_tags = c._store.store_memory.call_args.kwargs["tags"]
    # 归属没丢
    assert "user:u_dad" in weekly_tags
    assert "task:t1" in weekly_tags


# ===========================================================================
# 4. tag helper 契约
# ===========================================================================


def test_tag_helpers_roundtrip() -> None:
    """user/participants/task tag 的生成与解析自洽。"""
    assert user_tag("u1") == "user:u1"
    assert user_tag(None) == "user:unknown"
    assert user_tag("") == "user:unknown"

    assert task_tag("t1") == "task:t1"
    assert task_tag("") == ""

    p = participants_tag(["b", "a", "b"])
    assert p == "participants:a,b", "去重 + 排序,保证值稳定"
    assert parse_participants_tag([p]) == ["a", "b"]
    assert participants_tag([]) == ""

    tags = [user_tag("u1"), task_tag("t1"), task_tag("t2")]
    assert parse_user_tag(tags) == "u1"
    assert parse_task_tags(tags) == ["t1", "t2"]
    # 没有 user tag → None(不是 "unknown"),调用方才能区分两种情况
    assert parse_user_tag([task_tag("t1")]) is None
