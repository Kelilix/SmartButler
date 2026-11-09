"""🆕 2026-11-04: 时间语义抽取测试。

覆盖三块:

1. **TemporalExtractor** —— LLM 绝对化相对时间,四种形态各自正确
2. **tag 往返** —— spoken_at / valid_at 的生成与解析
3. **auto_extract 接线** —— fact 带上 spoken_at + valid_at,LLM 失败时降级

关键不变式:
    - ``valid_at`` 为空 ≠ ``permanent``。抽不到时间**不能**写成"永久"
    - 相对时间以 **spoken_at**(说话时刻)为基准,不是"现在"
    - ``spoken_at`` 由代码取(客观),``valid_at`` 由 LLM 抽(语义)
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from smartbutler.emotion.memory.auto_extract import AutoExtractHook
from smartbutler.emotion.memory.levels import (
    INVALIDATES_TAG,
    invalid_at_tag,
    parse_invalid_at,
    parse_spoken_at,
    parse_supersedes,
    parse_valid_at,
    spoken_at_tag,
    supersedes_tag,
    task_tag,
    user_tag,
    valid_at_tag,
)
from smartbutler.emotion.memory.task_index import TaskIndex, TaskMessageRecord
from smartbutler.emotion.memory.temporal import (
    TemporalExtractor,
    is_valid_at_active,
    is_valid_at_pending,
    parse_range,
    parse_recurring_md,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeLLM:
    """按 prompt 内容返回预设 JSON 的假 LLM。"""

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.prompts: list[str] = []

    async def chat(self, messages: Any, **kwargs: Any) -> Any:
        self.prompts.append(getattr(messages[-1], "content", "") or "")
        resp = MagicMock()
        resp.content = self.reply
        return resp


#: 参考时刻:2026-11-04(周三)22:00 UTC
_REF = datetime(2026, 11, 4, 22, 0, 0, tzinfo=UTC)


def _make_facade() -> MagicMock:
    f = MagicMock()
    f.remember = AsyncMock(return_value="key")
    return f


def _add_msg(
    task_index: TaskIndex,
    *,
    task_id: str,
    message_id: str,
    user_id: str,
    content: str,
    spoken_at: str = "2026-11-04T22:00:00+00:00",
    role: str = "user",
) -> None:
    task_index.append_message(
        TaskMessageRecord(
            task_id=task_id,
            message_id=message_id,
            thread_id="thread-1",
            role=role,
            user_id=user_id,
            spoken_at=spoken_at,
            content=content,
        )
    )


# ===========================================================================
# 1. tag 往返
# ===========================================================================


def test_time_tag_roundtrip() -> None:
    """spoken_at / valid_at tag 生成与解析自洽。"""
    s = spoken_at_tag("2026-11-04T22:00:00+00:00")
    assert s == "spoken_at:2026-11-04T22:00:00+00:00"
    assert parse_spoken_at([s]) == "2026-11-04T22:00:00+00:00"

    # 四种 valid_at 形态都能往返
    for v in (
        "2026-01-01",
        "2026-11-09/2026-11-15",
        "recurring:01-29",
        "permanent",
    ):
        tag = valid_at_tag(v)
        assert tag == f"valid_at:{v}"
        assert parse_valid_at([tag]) == v

    # 空值 → 不写 tag(而不是写成 permanent)
    assert valid_at_tag(None) == ""
    assert valid_at_tag("") == ""
    assert spoken_at_tag(None) == ""
    assert parse_valid_at([]) == ""
    assert parse_spoken_at([]) == ""


# ===========================================================================
# 2. TemporalExtractor
# ===========================================================================


async def test_extract_absolute_date_directly() -> None:
    """绝对时间直接抽,不做换算。"""
    llm = _FakeLLM('{"valid_at": "2026-01-01", "kind": "point"}')
    ex = TemporalExtractor(llm)
    info = await ex.extract("2026年1月1号是我领证了", reference_time=_REF)
    assert info.valid_at == "2026-01-01"
    assert info.kind == "point"
    assert info.has_time


async def test_extract_relative_uses_reference_timestamp() -> None:
    """相对时间以 reference timestamp 为基准换算。

    "明天" 必须相对于**说话时刻**,不是"现在"。
    """
    llm = _FakeLLM('{"valid_at": "2026-11-05", "kind": "point"}')
    ex = TemporalExtractor(llm)
    # reference 是 11-04 → 明天是 11-05
    await ex.extract("我明天要去上海", reference_time=_REF)
    prompt = llm.prompts[0]
    # prompt 里必须带上 reference timestamp(LLM 靠它换算)
    assert "2026-11-04T22:00:00+00:00" in prompt
    assert "我明天要去上海" in prompt


async def test_extract_range_for_next_week() -> None:
    """时间范围 → 斜杠分隔的区间。"""
    llm = _FakeLLM('{"valid_at": "2026-11-09/2026-11-15", "kind": "range"}')
    ex = TemporalExtractor(llm)
    info = await ex.extract("下周我想吃龙虾", reference_time=_REF)
    assert info.valid_at == "2026-11-09/2026-11-15"
    assert info.kind == "range"


async def test_extract_recurring_for_birthday() -> None:
    """周期性事实 → recurring:MM-DD。"""
    llm = _FakeLLM('{"valid_at": "recurring:01-29", "kind": "recurring"}')
    ex = TemporalExtractor(llm)
    info = await ex.extract("1月29号是我生日", reference_time=_REF)
    assert info.valid_at == "recurring:01-29"
    assert info.kind == "recurring"


async def test_extract_permanent_for_general_fact() -> None:
    """普适事实 → permanent。"""
    llm = _FakeLLM('{"valid_at": "permanent", "kind": "permanent"}')
    ex = TemporalExtractor(llm)
    info = await ex.extract("张女士是我朋友", reference_time=_REF)
    assert info.valid_at == "permanent"
    assert info.kind == "permanent"
    assert info.is_permanent


async def test_extract_detects_invalidation() -> None:
    """🆕 否定/修正语义:LLM 识别出"这条话终止了一个既有关系"。

    "张女士不再是我朋友了,我们掰了" →
        valid_at=permanent(从此刻起生效的新事实)
        invalidates=True(终止了旧事实)
    """
    llm = _FakeLLM(
        '{"valid_at": "permanent", "kind": "permanent", "invalidates": true}'
    )
    ex = TemporalExtractor(llm)
    info = await ex.extract("张女士不再是我朋友了,我们掰了", reference_time=_REF)
    assert info.invalidates is True
    assert info.is_invalidation
    # 否定类的话**仍然要正常给 valid_at**(它本身是新事实)
    assert info.valid_at == "permanent"


async def test_extract_invalidation_default_is_false() -> None:
    """普通事实不带 invalidates 字段 → 默认 False。"""
    llm = _FakeLLM('{"valid_at": "2026-11-05", "kind": "point"}')
    ex = TemporalExtractor(llm)
    info = await ex.extract("我明天去上海", reference_time=_REF)
    assert info.invalidates is False


async def test_extract_invalidation_coerces_string_true() -> None:
    """LLM 返回字符串 "true" 也要能识别(容错)。"""
    llm = _FakeLLM(
        '{"valid_at": "permanent", "kind": "permanent", "invalidates": "true"}'
    )
    ex = TemporalExtractor(llm)
    info = await ex.extract("我已经不在这家公司工作了", reference_time=_REF)
    assert info.invalidates is True


async def test_extract_none_when_no_time_semantics() -> None:
    """没有时间语义 → valid_at=None(不是 permanent)。"""
    llm = _FakeLLM('{"valid_at": null, "kind": "none"}')
    ex = TemporalExtractor(llm)
    info = await ex.extract("今天天气不错", reference_time=_REF)
    assert info.valid_at is None
    assert info.kind == "none"
    assert not info.has_time


async def test_extract_degrades_on_llm_failure() -> None:
    """LLM 抛异常 → 降级为 none(不写 valid_at)。"""

    class _Broken:
        async def chat(self, *a: Any, **k: Any) -> Any:
            raise RuntimeError("llm down")

    ex = TemporalExtractor(_Broken())
    info = await ex.extract("我明天去上海", reference_time=_REF)
    assert info.valid_at is None
    assert info.kind == "none"


async def test_extract_degrades_on_bad_json() -> None:
    """LLM 输出不是合法 JSON → 降级为 none。"""
    llm = _FakeLLM("我不知道")
    ex = TemporalExtractor(llm)
    info = await ex.extract("我明天去上海", reference_time=_REF)
    assert info.valid_at is None


async def test_extract_rejects_invalid_valid_at_value() -> None:
    """LLM 编出非法值(比如"待定")→ 丢弃,不当成有效时间。"""
    llm = _FakeLLM('{"valid_at": "待定", "kind": "point"}')
    ex = TemporalExtractor(llm)
    info = await ex.extract("我明天去上海", reference_time=_REF)
    assert info.valid_at is None
    assert info.kind == "none"


async def test_extract_without_llm_returns_none() -> None:
    """未注入 LLM → None(不抽)。"""
    ex = TemporalExtractor(None)
    assert not ex.enabled
    info = await ex.extract("我明天去上海", reference_time=_REF)
    assert info.valid_at is None


async def test_extract_handles_markdown_fenced_json() -> None:
    """LLM 经常无视指令包 ```json 围栏 —— 要能剥掉。"""
    llm = _FakeLLM('```json\n{"valid_at": "2026-01-01", "kind": "point"}\n```')
    ex = TemporalExtractor(llm)
    info = await ex.extract("2026年1月1号领证", reference_time=_REF)
    assert info.valid_at == "2026-01-01"


async def test_extract_handles_json_with_prose() -> None:
    """LLM 输出带前后解释文字 → 也要能抠出 JSON。"""
    llm = _FakeLLM(
        '好的,分析如下:\n{"valid_at": "2026-01-01", "kind": "point"}\n希望有帮助'
    )
    ex = TemporalExtractor(llm)
    info = await ex.extract("2026年1月1号领证", reference_time=_REF)
    assert info.valid_at == "2026-01-01"


# ===========================================================================
# 3. 编码值解析工具
# ===========================================================================


def test_parse_recurring_and_range() -> None:
    """周期 / 区间的解析工具。"""
    assert parse_recurring_md("recurring:01-29") == (1, 29)
    assert parse_recurring_md("recurring:13-01") is None, "月份非法要拒"
    assert parse_recurring_md("permanent") is None

    assert parse_range("2026-11-09/2026-11-15") == ("2026-11-09", "2026-11-15")
    assert parse_range("2026-11-09") is None
    assert parse_range("/2026-11-15") is None


def test_is_valid_at_active_semantics() -> None:
    """valid_at 的"当前是否成立"判定。"""
    at = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)

    # 永久 → 永远成立
    assert is_valid_at_active("permanent", at=at)
    # 空 → 不因为缺时间就误杀
    assert is_valid_at_active("", at=at)
    # 精确点 → 事件已发生即成立
    assert is_valid_at_active("2026-01-01", at=at)
    assert not is_valid_at_active("2026-12-01", at=at)
    # 区间 → 落在区间内(闭区间:两端都含)
    assert is_valid_at_active("2026-11-09/2026-11-15", at=at)
    assert is_valid_at_active("2026-11-09/2026-11-15", at=datetime(2026, 11, 15, 0, 0, tzinfo=UTC))
    assert not is_valid_at_active("2026-11-20/2026-11-25", at=at)


def test_is_valid_at_active_recurring_is_always_active() -> None:
    """🆕 周期事实**永远成立** —— 生日不是"只有生日那天才成立"。

    "1月29号是我生日" → recurring:01-29
    12 月问"我什么时候生日" → 这个事实依然为真(只是下次还没到)。
    旧实现要求月日精确匹配 → 12 月被判为"不成立" → 渲染成
    "每年 01-29 生效，当前已不适用" → 荒谬。
    """
    at = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
    assert is_valid_at_active("recurring:01-29", at=at), "非生日当天也应成立"
    assert is_valid_at_active("recurring:11-10", at=at), "生日当天当然成立"


def test_is_valid_at_pending_distinguishes_future_from_expired() -> None:
    """🆕 "尚未生效" 与 "当前已不适用" 必须能区分开。

    - "我明天去上海"(未来精确点)→ pending=True
    - "我那阵子在杭州出差"(已过期区间)→ pending=False
    两者 active 都是 False,但对 LLM 的意义相反。
    """
    at = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)

    # 未来精确点 → 尚未生效
    assert is_valid_at_pending("2026-12-01", at=at) is True
    assert is_valid_at_active("2026-12-01", at=at) is False

    # 已过期的区间 → 不是"尚未生效",是"已过期"
    assert is_valid_at_pending("2026-01-01/2026-01-31", at=at) is False
    assert is_valid_at_active("2026-01-01/2026-01-31", at=at) is False

    # 还没开始的区间 → 尚未生效
    assert is_valid_at_pending("2026-12-01/2026-12-31", at=at) is True

    # 永久 / 周期 / 空 → 不是"尚未生效"
    assert is_valid_at_pending("permanent", at=at) is False
    assert is_valid_at_pending("recurring:01-29", at=at) is False
    assert is_valid_at_pending("", at=at) is False

    # 过去的点 → 不是"尚未生效"
    assert is_valid_at_pending("2026-01-01", at=at) is False


# ===========================================================================
# 4. auto_extract 接线
# ===========================================================================


async def test_fact_carries_spoken_at_and_valid_at(tmp_path: Path) -> None:
    """fact 同时带 spoken_at(代码取)和 valid_at(LLM 抽)。"""
    task_index = TaskIndex(tmp_path / "tasks.db")
    facade = _make_facade()
    llm = _FakeLLM('{"valid_at": "2026-11-05", "kind": "point"}')

    hook = AutoExtractHook(
        facade=facade,
        short_term=MagicMock(),
        task_index=task_index,
        min_importance=0.0,
        temporal_extractor=TemporalExtractor(llm),
    )

    _add_msg(
        task_index,
        task_id="t1",
        message_id="m1",
        user_id="u_dad",
        content="我明天要去上海",
        spoken_at="2026-11-04T22:00:00+00:00",
    )

    await hook._extract_and_store(
        task_id="t1",
        category="booking",
        title="订机票",
        status="completed",
        thread_id="thread-1",
    )

    assert facade.remember.await_count == 1
    tags = facade.remember.await_args.kwargs["tags"]
    assert parse_spoken_at(tags) == "2026-11-04T22:00:00+00:00"
    assert parse_valid_at(tags) == "2026-11-05"


async def test_fact_spoken_at_is_earliest_in_bucket(tmp_path: Path) -> None:
    """同一人多条消息 → spoken_at 取最早那条。"""
    task_index = TaskIndex(tmp_path / "tasks.db")
    facade = _make_facade()

    hook = AutoExtractHook(
        facade=facade,
        short_term=MagicMock(),
        task_index=task_index,
        min_importance=0.0,
        temporal_extractor=TemporalExtractor(None),
    )

    _add_msg(
        task_index, task_id="t1", message_id="m1", user_id="u_dad",
        content="先说一件事", spoken_at="2026-11-04T10:00:00+00:00",
    )
    _add_msg(
        task_index, task_id="t1", message_id="m2", user_id="u_dad",
        content="再说一件", spoken_at="2026-11-04T20:00:00+00:00",
    )

    await hook._extract_and_store(
        task_id="t1", category="chitchat", title="闲聊",
        status="chitchat_done", thread_id="thread-1",
    )

    tags = facade.remember.await_args.kwargs["tags"]
    assert parse_spoken_at(tags) == "2026-11-04T10:00:00+00:00"


async def test_fact_without_temporal_extractor_has_spoken_at_only(tmp_path: Path) -> None:
    """没注入 LLM → 只写 spoken_at,**不写** valid_at。"""
    task_index = TaskIndex(tmp_path / "tasks.db")
    facade = _make_facade()

    hook = AutoExtractHook(
        facade=facade,
        short_term=MagicMock(),
        task_index=task_index,
        min_importance=0.0,
    )

    _add_msg(
        task_index, task_id="t1", message_id="m1", user_id="u_dad",
        content="我明天要去上海",
    )

    await hook._extract_and_store(
        task_id="t1", category="booking", title="订机票",
        status="completed", thread_id="thread-1",
    )

    tags = facade.remember.await_args.kwargs["tags"]
    assert parse_spoken_at(tags) == "2026-11-04T22:00:00+00:00"
    # 没有 LLM → 没有 valid_at tag(**不是** permanent)
    assert not any(t.startswith("valid_at:") for t in tags)


async def test_temporal_failure_does_not_break_extraction(tmp_path: Path) -> None:
    """LLM 时间抽取失败 → fact 照样入库(降级优先)。"""

    class _Broken:
        async def chat(self, *a: Any, **k: Any) -> Any:
            raise RuntimeError("llm down")

    task_index = TaskIndex(tmp_path / "tasks.db")
    facade = _make_facade()

    hook = AutoExtractHook(
        facade=facade,
        short_term=MagicMock(),
        task_index=task_index,
        min_importance=0.0,
        temporal_extractor=TemporalExtractor(_Broken()),
    )

    _add_msg(
        task_index, task_id="t1", message_id="m1", user_id="u_dad",
        content="我明天要去上海",
    )

    await hook._extract_and_store(
        task_id="t1", category="booking", title="订机票",
        status="completed", thread_id="thread-1",
    )

    assert facade.remember.await_count == 1, "LLM 挂了也要把 fact 存下来"
    tags = facade.remember.await_args.kwargs["tags"]
    assert parse_spoken_at(tags) == "2026-11-04T22:00:00+00:00"
    assert not any(t.startswith("valid_at:") for t in tags)


# ===========================================================================
# 5. 历史关系修正(Step 1 承载)
# ===========================================================================


async def test_invalidation_fact_is_marked(tmp_path: Path) -> None:
    """🆕 "张女士不再是我朋友了" → fact 带 invalidates 标记。

    Step 1 只负责**标记**(自动消解是 Step 3)。
    """
    task_index = TaskIndex(tmp_path / "tasks.db")
    facade = _make_facade()
    llm = _FakeLLM(
        '{"valid_at": "permanent", "kind": "permanent", "invalidates": true}'
    )

    hook = AutoExtractHook(
        facade=facade,
        short_term=MagicMock(),
        task_index=task_index,
        min_importance=0.0,
        temporal_extractor=TemporalExtractor(llm),
    )

    _add_msg(
        task_index, task_id="t1", message_id="m1", user_id="u_dad",
        content="张女士不再是我朋友了,我们掰了",
    )

    await hook._extract_and_store(
        task_id="t1", category="chitchat", title="闲聊",
        status="chitchat_done", thread_id="thread-1",
    )

    tags = facade.remember.await_args.kwargs["tags"]
    assert INVALIDATES_TAG in tags, "否定类事实必须带 invalidates 标记"
    assert parse_valid_at(tags) == "permanent"


async def test_invalidation_marker_survives_promotion() -> None:
    """🆕 升格时 invalidates 标记不能丢。

    为什么重要:LLM 摘要有时会把"不再"这种**否定词**丢掉 ——
    "张女士不再是我朋友了" 被压成 "张女士是我朋友" 是灾难性错误。
    所以这个标记由**代码**在每层升格时重写一遍。
    """
    from smartbutler.emotion.memory.consolidation import Consolidator

    store = MagicMock()
    store.store_memory = MagicMock(return_value=None)

    class _Emb:
        async def embed(self, text: str) -> list[float]:
            return [0.0] * 1024

    c = Consolidator(store=store, embedder=_Emb())  # type: ignore[arg-type]

    facts = [
        {
            "key": "f1",
            "content": "张女士不再是我朋友了,我们掰了",
            "importance": 0.8,
            "created_at": "2026-11-01T10:00:00+00:00",
            "tags": [user_tag("u_dad"), task_tag("t1"), INVALIDATES_TAG],
        },
    ]
    groups = c._group_by_subject(facts)
    assert groups[0].has_invalidates is True

    from smartbutler.emotion.memory.levels import MemoryLevel

    await c._write_summary(
        summary="摘要",
        target_level=MemoryLevel.DAILY,
        source_keys=["f1"],
        importance=0.8,
        period="昨天",
        source_count=1,
        group=groups[0],
    )
    tags = store.store_memory.call_args.kwargs["tags"]
    assert INVALIDATES_TAG in tags, "升格不能把 invalidates 标记洗掉"


def test_invalidation_tag_roundtrip() -> None:
    """invalid_at / supersedes tag 的生成与解析。"""
    assert invalid_at_tag("2026-11-04T10:00:00+00:00") == (
        "invalid_at:2026-11-04T10:00:00+00:00"
    )
    assert invalid_at_tag(None) == ""
    assert parse_invalid_at(["invalid_at:2026-11-04"]) == "2026-11-04"
    assert parse_invalid_at([]) == ""

    assert supersedes_tag("fact_b,fact_a") == "supersedes:fact_a,fact_b"
    assert supersedes_tag(None) == ""
    assert parse_supersedes(["supersedes:fact_a,fact_b"]) == ["fact_a", "fact_b"]


def test_describe_valid_at_translates_codes() -> None:
    """valid_at 编码值 → LLM 能懂的中文。"""
    from smartbutler.emotion.memory.facade import _describe_valid_at

    assert _describe_valid_at("permanent") == "长期有效"
    assert _describe_valid_at("2026-01-01") == "生效于 2026-01-01"
    assert "至" in _describe_valid_at("2026-11-09/2026-11-15")
    assert _describe_valid_at("recurring:01-29") == "每年 01-29 生效"
    assert _describe_valid_at("") == ""


def test_render_time_hint_marks_invalidation_and_expiry() -> None:
    """🆕 召回渲染:LLM 必须能看到"这条已被修正"和"当前已过期"。

    "张女士不再是我朋友了" → 要标出"修正了此前的说法"
    区间已过期的 fact     → 要标出"当前已不适用"
    """
    from smartbutler.emotion.memory.facade import MemoryFacade

    # 否定/修正
    hint = MemoryFacade._render_time_hint({
        "content": "张女士不再是我朋友了",
        "tags": [valid_at_tag("permanent"), INVALIDATES_TAG],
    })
    assert "修正了此前的说法" in hint

    # 已失效(有 invalid_at)
    hint2 = MemoryFacade._render_time_hint({
        "content": "我住在上海",
        "tags": [valid_at_tag("permanent"), invalid_at_tag("2026-11-04")],
    })
    assert "失效" in hint2

    # 区间已过期(现在是 2026-11-04,区间是 2026-01)
    hint3 = MemoryFacade._render_time_hint({
        "content": "我那阵子在杭州出差",
        "tags": [valid_at_tag("2026-01-01/2026-01-31")],
    })
    assert "当前已不适用" in hint3

    # 没有时间信息 → 不添乱
    assert MemoryFacade._render_time_hint({"content": "x", "tags": []}) == ""


def test_render_time_hint_distinguishes_pending_from_expired() -> None:
    """🆕 "尚未生效" ≠ "当前已不适用"。

    这是审计时发现并修掉的真实缺陷:
    - "我明天去上海"(未来)旧实现渲染成"当前已不适用"
      → LLM 会把用户明天的安排当成"已经作废"
    - "1月29号是我生日"(周期)旧实现渲染成"当前已不适用"
      → 荒谬,生日全年都成立
    """
    from smartbutler.emotion.memory.facade import MemoryFacade

    # 未来精确点 → 尚未生效
    hint_future = MemoryFacade._render_time_hint({
        "content": "我明天去上海",
        "tags": [valid_at_tag("2026-12-01")],
    })
    assert "尚未生效" in hint_future
    assert "当前已不适用" not in hint_future, "未来的事不能标成'已过期'"

    # 已过期的区间 → 当前已不适用(且不是"尚未生效")
    hint_expired = MemoryFacade._render_time_hint({
        "content": "我那阵子在杭州出差",
        "tags": [valid_at_tag("2026-01-01/2026-01-31")],
    })
    assert "当前已不适用" in hint_expired
    assert "尚未生效" not in hint_expired

    # 周期事实 → 不该被标成"当前已不适用"(它每年都成立)
    hint_recurring = MemoryFacade._render_time_hint({
        "content": "1月29号是我生日",
        "tags": [valid_at_tag("recurring:01-29")],
    })
    assert "当前已不适用" not in hint_recurring
    assert "尚未生效" not in hint_recurring
    assert "每年 01-29 生效" in hint_recurring

    # 永久 → 长期有效,不带任何失效标记
    hint_permanent = MemoryFacade._render_time_hint({
        "content": "张女士是我朋友",
        "tags": [valid_at_tag("permanent")],
    })
    assert hint_permanent == "(长期有效)"


def test_historical_relation_correction_end_to_end() -> None:
    """🆕 "张女士不再是我朋友了" 的完整链路(Step 1 范围)。

    验证:
    1. 新 fact 被标记 invalidates
    2. 新 fact 的 valid_at = permanent(从此刻起生效的新状态)
    3. 旧 fact(张女士是我朋友)因为**没有** invalid_at,仍然 valid_at=permanent
       —— Step 1 不改写历史,Step 3 才做自动消解
    4. 召回渲染时,新 fact 带"修正了此前的说法"标记
    """
    from smartbutler.emotion.memory.facade import MemoryFacade

    old_fact = {
        "key": "old",
        "content": "[user=u_dad] 张女士是我朋友",
        "tags": [user_tag("u_dad"), valid_at_tag("permanent")],
    }
    new_fact = {
        "key": "new",
        "content": "[user=u_dad] 张女士不再是我朋友了,我们掰了",
        "tags": [
            user_tag("u_dad"),
            valid_at_tag("permanent"),
            INVALIDATES_TAG,
            spoken_at_tag("2026-11-04T10:00:00+00:00"),
        ],
    }

    # 旧 fact 的时间语义不变(历史没被改写)
    assert parse_valid_at(old_fact["tags"]) == "permanent"
    assert parse_invalid_at(old_fact["tags"]) == ""
    assert INVALIDATES_TAG not in old_fact["tags"]

    # 新 fact 带着修正标记
    assert parse_valid_at(new_fact["tags"]) == "permanent"
    assert INVALIDATES_TAG in new_fact["tags"]

    # 渲染时区分得开
    assert "修正了此前的说法" in MemoryFacade._render_time_hint(new_fact)
    assert "修正了此前的说法" not in MemoryFacade._render_time_hint(old_fact)


def test_superseded_old_fact_marked_in_same_task() -> None:
    """🆕 同一 task 内,旧 fact 必须被标成"已被修正" —— 否则两条互相矛盾。

    这是审计时发现的真实缺陷:
    旧 fact("张女士是我朋友")渲染成"长期有效",
    新 fact("不再是我朋友了")渲染成"长期有效,修正了此前的说法"
    → 两条都说自己长期有效 → LLM 拿到矛盾信息,答案不唯一。

    Step 1 不建图,但**同一 task 内**的修正关系可以安全推断。
    """
    from smartbutler.emotion.memory.facade import MemoryFacade

    old_fact = {
        "key": "fact_old",
        "content": "张女士是我朋友",
        "tags": [
            user_tag("u_dad"), task_tag("t1"),
            spoken_at_tag("2026-01-01T10:00:00+00:00"),
            valid_at_tag("permanent"),
        ],
    }
    new_fact = {
        "key": "fact_new",
        "content": "张女士不再是我朋友了,我们掰了",
        "tags": [
            user_tag("u_dad"), task_tag("t1"),
            spoken_at_tag("2026-11-04T10:00:00+00:00"),
            valid_at_tag("permanent"),
            INVALIDATES_TAG,
        ],
    }

    superseded = MemoryFacade._compute_superseded_keys([old_fact, new_fact])
    assert superseded == {"fact_old"}, "同 task 的旧 fact 应被标记为被修正"

    old_hint = MemoryFacade._render_time_hint(old_fact, superseded=superseded)
    assert "已被后续说法修正" in old_hint
    assert "长期有效" in old_hint, "保留原本的时间语义,只是补充修正标记"

    new_hint = MemoryFacade._render_time_hint(new_fact, superseded=superseded)
    assert "修正了此前的说法" in new_hint


def test_superseded_does_not_cross_task() -> None:
    """🆕 跨 task **不能**乱标"已被修正"。

    不同 task 的两条 fact 可能是不同主题(甚至不同的人),
    跨 task 乱标会把不相关的事实打成"已修正" —— 比不标更糟。
    """
    from smartbutler.emotion.memory.facade import MemoryFacade

    other_task_fact = {
        "key": "fact_other_task",
        "content": "张女士是我另一个朋友",
        "tags": [
            user_tag("u_dad"), task_tag("t2"),
            spoken_at_tag("2026-02-01T10:00:00+00:00"),
            valid_at_tag("permanent"),
        ],
    }
    new_fact = {
        "key": "fact_new",
        "content": "张女士不再是我朋友了,我们掰了",
        "tags": [
            user_tag("u_dad"), task_tag("t1"),
            spoken_at_tag("2026-11-04T10:00:00+00:00"),
            valid_at_tag("permanent"),
            INVALIDATES_TAG,
        ],
    }

    superseded = MemoryFacade._compute_superseded_keys([other_task_fact, new_fact])
    assert superseded == set(), "跨 task 不能标"

    hint = MemoryFacade._render_time_hint(other_task_fact, superseded=superseded)
    assert "已被后续说法修正" not in hint


def test_superseded_respects_spoken_at_order() -> None:
    """🆕 旧 fact 的说话时间必须**早于**修正 fact,才可能被标记。

    否则会出现"后来的事实被更早的修正覆盖"的倒置。
    """
    from smartbutler.emotion.memory.facade import MemoryFacade

    # "修正"说于 2026-01,旧 fact 说于 2026-11(时间倒置)
    later_fact = {
        "key": "fact_later",
        "content": "张女士是我朋友",
        "tags": [
            user_tag("u_dad"), task_tag("t1"),
            spoken_at_tag("2026-11-04T10:00:00+00:00"),
            valid_at_tag("permanent"),
        ],
    }
    invalidator = {
        "key": "fact_inval",
        "content": "张女士不再是我朋友了",
        "tags": [
            user_tag("u_dad"), task_tag("t1"),
            spoken_at_tag("2026-01-01T10:00:00+00:00"),
            valid_at_tag("permanent"),
            INVALIDATES_TAG,
        ],
    }

    superseded = MemoryFacade._compute_superseded_keys([later_fact, invalidator])
    assert superseded == set(), "时间倒置时不该标记"


def test_superseded_ignores_invalidates_facts() -> None:
    """🆕 两条修正语句之间不该互相覆盖。"""
    from smartbutler.emotion.memory.facade import MemoryFacade

    inv1 = {
        "key": "inv1",
        "content": "张女士不再是我朋友了",
        "tags": [
            user_tag("u_dad"), task_tag("t1"),
            spoken_at_tag("2026-10-01T10:00:00+00:00"),
            valid_at_tag("permanent"), INVALIDATES_TAG,
        ],
    }
    inv2 = {
        "key": "inv2",
        "content": "我也不是她朋友了",
        "tags": [
            user_tag("u_dad"), task_tag("t1"),
            spoken_at_tag("2026-11-01T10:00:00+00:00"),
            valid_at_tag("permanent"), INVALIDATES_TAG,
        ],
    }

    superseded = MemoryFacade._compute_superseded_keys([inv1, inv2])
    assert superseded == set(), "修正 fact 之间不该互相标记"
