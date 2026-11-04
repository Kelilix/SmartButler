"""🆕 2026-11-02: 升格 prompt 与跨天 task 归一化测试。

覆盖两个容易做错的地方:

1. **升格 prompt 按项目分段**
   - LLM 拿到的输入已经是**代码分好段**的
   - prompt 明确要求"按【项目】分段组织"
   - 关键:prompt 里必须写明"照原文保留指称,不要替 LLM 判断那是谁的事"
     —— 这是"归属由代码承担"的推论:LLM 不许重新判断归属

2. **跨天 task 归一化**
   - 一个 task 跨午夜时,所有 fact 落在"开始那天"
   - 窗口过滤用 ``task_started_at`` 而不是 fact 自己的 created_at
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

from smartbutler.emotion.memory.consolidation import Consolidator
from smartbutler.emotion.memory.levels import (
    task_started_at_tag,
    task_tag,
    user_tag,
)
from smartbutler.emotion.memory.summarizer import LLMSummarizer


class _FakeLLM:
    """记录 prompt 的假 LLM。"""

    def __init__(self, reply: str = "## 摘要\n- 内容") -> None:
        self.reply = reply
        self.prompts: list[str] = []

    async def chat(self, messages: Any, **kwargs: Any) -> Any:
        # 最后一个 Message 是 user prompt
        self.prompts.append(getattr(messages[-1], "content", "") or "")
        resp = MagicMock()
        resp.content = self.reply
        return resp


class _Emb:
    async def embed(self, text: str) -> list[float]:
        return [0.0] * 1024


def _make_store() -> MagicMock:
    store = MagicMock()
    store.store_memory = MagicMock(return_value=None)
    return store


# ---------------------------------------------------------------------------
# 1. 升格 prompt 按项目分段
# ---------------------------------------------------------------------------


async def test_group_prompt_is_segmented_by_task() -> None:
    """prompt 输入按【项目】分段,不是扁平列表。"""
    llm = _FakeLLM()
    s = LLMSummarizer(llm)
    c = Consolidator(store=_make_store(), embedder=_Emb())  # type: ignore[arg-type]

    facts = [
        {
            "key": "f1",
            "content": "[user=u_dad] 我明天要去上海",
            "importance": 0.7,
            "created_at": "2026-11-01T10:00:00+00:00",
            "tags": [user_tag("u_dad"), task_tag("t1")],
        },
        {
            "key": "f2",
            "content": "[user=u_dad] 周末看房",
            "importance": 0.7,
            "created_at": "2026-11-01T11:00:00+00:00",
            "tags": [user_tag("u_dad"), task_tag("t2")],
        },
    ]
    groups = c._group_by_subject(facts)
    assert len(groups) == 1
    assert len(groups[0].segments) == 2, "两个 task → 两段"

    out = await s.summarize_group(groups[0], level="daily", period="昨天")

    assert out == "## 摘要\n- 内容"
    prompt = llm.prompts[0]

    # 分段标题
    assert "【项目1】" in prompt
    assert "【项目2】" in prompt
    # 两段内容都在
    assert "我明天要去上海" in prompt
    assert "周末看房" in prompt
    # 要求按项目分段组织
    assert "按【项目】分段组织" in prompt


async def test_group_prompt_forbids_llm_from_reassigning_attribution() -> None:
    """prompt 明确禁止 LLM 重新判断归属。

    这是"代码承担事实,LLM 承担表达"在 prompt 层的落地:
    LLM 读到"我妈也去上海"时,**不许**把它当成妈妈的事去写,
    只许照原文保留这个指称。
    """
    llm = _FakeLLM()
    s = LLMSummarizer(llm)
    c = Consolidator(store=_make_store(), embedder=_Emb())  # type: ignore[arg-type]

    facts = [
        {
            "key": "f1",
            "content": "[user=u_dad] 我妈今天也去上海",
            "importance": 0.7,
            "created_at": "2026-11-01T10:00:00+00:00",
            "tags": [user_tag("u_dad"), task_tag("t1")],
        },
    ]
    groups = c._group_by_subject(facts)
    await s.summarize_group(groups[0], level="daily", period="昨天")

    prompt = llm.prompts[0]
    assert "不要" in prompt
    assert "指称" in prompt or "判断" in prompt
    # 禁止编造
    assert "严禁推断" in prompt
    # 要求只用真实存在的信息
    assert "只写记忆里真实存在的信息" in prompt


async def test_group_prompt_says_no_third_person() -> None:
    """prompt 要求用直接陈述,不要"用户说"这类转述。"""
    llm = _FakeLLM()
    s = LLMSummarizer(llm)
    c = Consolidator(store=_make_store(), embedder=_Emb())  # type: ignore[arg-type]

    facts = [
        {
            "key": "f1", "content": "内容", "importance": 0.7,
            "created_at": "2026-11-01T10:00:00+00:00",
            "tags": [user_tag("u")],
        },
    ]
    groups = c._group_by_subject(facts)
    await s.summarize_group(groups[0], level="daily", period="昨天")

    prompt = llm.prompts[0]
    assert "不要写" in prompt
    assert "用户" in prompt


async def test_summarize_group_without_llm_returns_none() -> None:
    """未注入 LLM → 返回 None(调用方降级到启发式拼接)。"""
    s = LLMSummarizer(None)
    group = MagicMock()
    group.segments = []
    group.facts = [{"content": "x"}]
    assert await s.summarize_group(group, level="daily", period="昨天") is None


async def test_summarize_group_llm_failure_returns_none() -> None:
    """LLM 抛异常 → 返回 None,不炸。"""

    class _BrokenLLM:
        async def chat(self, *args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("llm down")

    s = LLMSummarizer(_BrokenLLM())
    group = MagicMock()
    group.segments = [("t1", [{"content": "x"}])]
    group.facts = [{"content": "x"}]
    assert await s.summarize_group(group, level="daily", period="昨天") is None


# ---------------------------------------------------------------------------
# 2. 跨天 task 归一化
# ---------------------------------------------------------------------------


def test_scan_uses_task_started_at_for_window() -> None:
    """窗口过滤用**双条件**:fact 自身时间 **或** task 起始时间。

    场景:一个 task 跨了午夜(10/31 开始,11/01 继续)。
    升"昨天(11/01)"的日报时,11/01 的后续内容**必须**在 ——
    否则当天真实发生的事被漏掉。
    """
    store = MagicMock()
    store.list_keys_by_level = MagicMock(return_value=[
        {
            "key": "f1",
            "content": "跨天 task 的后续",
            "importance": 0.8,
            "created_at": "2026-11-01T02:00:00+00:00",
            "level": "",
            "tags": [
                user_tag("u_dad"),
                task_tag("t1"),
                task_started_at_tag("2026-10-31T20:00:00+00:00"),
            ],
        },
    ])
    c = Consolidator(store=store, embedder=_Emb())  # type: ignore[arg-type]

    # 窗口 = 11/01 全天
    since = datetime(2026, 11, 1, 0, 0, tzinfo=UTC)
    now = datetime(2026, 11, 2, 0, 0, tzinfo=UTC)
    out = c._scan(level=None, since=since, now=now, limit=100)

    assert len(out) == 1, "跨天 task 当天的 fact 必须进当天日报"
    assert out[0]["tags"], "tags 必须带出来(分组要用)"


def test_scan_includes_task_started_in_window() -> None:
    """task 在窗口内开始 → 它的 fact 进当天日报(哪怕 fact 写入时间在之后)。"""
    store = MagicMock()
    store.list_keys_by_level = MagicMock(return_value=[
        {
            "key": "f1",
            "content": "新开启的 task",
            "importance": 0.8,
            "created_at": "2026-11-01T23:59:00+00:00",
            "level": "",
            "tags": [
                user_tag("u_dad"),
                task_tag("t1"),
                task_started_at_tag("2026-11-01T09:00:00+00:00"),
            ],
        },
    ])
    c = Consolidator(store=store, embedder=_Emb())  # type: ignore[arg-type]

    since = datetime(2026, 11, 1, 0, 0, tzinfo=UTC)
    now = datetime(2026, 11, 2, 0, 0, tzinfo=UTC)
    out = c._scan(level=None, since=since, now=now, limit=100)

    assert len(out) == 1


def test_scan_excludes_task_started_before_window_and_fact_outside() -> None:
    """task 开始于窗口之前 + fact 写入也在窗口外 → 排除。"""
    store = MagicMock()
    store.list_keys_by_level = MagicMock(return_value=[
        {
            "key": "f1",
            "content": "早就结束的 task",
            "importance": 0.8,
            "created_at": "2026-10-20T10:00:00+00:00",
            "level": "",
            "tags": [
                user_tag("u_dad"),
                task_tag("t1"),
                task_started_at_tag("2026-10-19T09:00:00+00:00"),
            ],
        },
    ])
    c = Consolidator(store=store, embedder=_Emb())  # type: ignore[arg-type]

    since = datetime(2026, 11, 1, 0, 0, tzinfo=UTC)
    now = datetime(2026, 11, 2, 0, 0, tzinfo=UTC)
    assert c._scan(level=None, since=since, now=now, limit=100) == []


def test_scan_without_task_started_at_uses_created_at() -> None:
    """老数据没有 task_started_at → 退回用 created_at(行为不变)。"""
    store = MagicMock()
    store.list_keys_by_level = MagicMock(return_value=[
        {
            "key": "f1",
            "content": "普通 fact",
            "importance": 0.8,
            "created_at": "2026-11-01T10:00:00+00:00",
            "level": "",
            "tags": [user_tag("u_dad")],
        },
    ])
    c = Consolidator(store=store, embedder=_Emb())  # type: ignore[arg-type]

    since = datetime(2026, 11, 1, 0, 0, tzinfo=UTC)
    now = datetime(2026, 11, 2, 0, 0, tzinfo=UTC)
    out = c._scan(level=None, since=since, now=now, limit=100)

    assert len(out) == 1


def test_scan_excludes_out_of_window() -> None:
    """窗口外的 fact 被排除。"""
    store = MagicMock()
    store.list_keys_by_level = MagicMock(return_value=[
        {
            "key": "f1",
            "content": "太早的",
            "importance": 0.8,
            "created_at": "2026-10-20T10:00:00+00:00",
            "level": "",
            "tags": [user_tag("u_dad")],
        },
    ])
    c = Consolidator(store=store, embedder=_Emb())  # type: ignore[arg-type]

    since = datetime(2026, 11, 1, 0, 0, tzinfo=UTC)
    now = datetime(2026, 11, 2, 0, 0, tzinfo=UTC)
    assert c._scan(level=None, since=since, now=now, limit=100) == []
