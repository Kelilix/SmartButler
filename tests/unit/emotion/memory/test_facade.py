"""MemoryFacade 单测(Phase 6.2 P0 核心)。"""

from __future__ import annotations

from pathlib import Path

import pytest

from smartbutler.capabilities.embedding import StubEmbedder
from smartbutler.emotion.memory.facade import MemoryContext, MemoryFacade
from smartbutler.emotion.memory.hooks import MemoryEventType, MemoryHooks
from smartbutler.emotion.memory.importance import ImportanceScorer
from smartbutler.emotion.memory.retrieval import MemoryRetriever
from smartbutler.emotion.memory.long_term import LongTermStore
from smartbutler.storage import QdrantStorage


@pytest.fixture
def facade(tmp_path: Path) -> tuple[MemoryFacade, MemoryHooks, LongTermStore]:
    q = QdrantStorage(
        path=tmp_path / "q",
        collection="t",
        vector_size=64,
    )
    store = LongTermStore(q)
    embedder = StubEmbedder(dimension=64)
    retriever = MemoryRetriever(store, embedder, ImportanceScorer())
    hooks = MemoryHooks()
    f = MemoryFacade(retriever, hooks, store, embedder)
    return f, hooks, store


@pytest.mark.asyncio
async def test_facade_remember_returns_key(facade: tuple[MemoryFacade, MemoryHooks, LongTermStore]) -> None:
    """remember 返回非空 key。"""
    f, _, _ = facade
    key = await f.remember("我住在上海")
    assert key


@pytest.mark.asyncio
async def test_facade_remember_triggers_on_store(facade: tuple[MemoryFacade, MemoryHooks, LongTermStore]) -> None:
    """remember 触发 ON_STORE 事件。"""
    f, hooks, _ = facade
    seen: list[str] = []

    def cb(*, key: str, content: str) -> None:  # noqa: ARG001
        seen.append(key)

    hooks.register(MemoryEventType.ON_STORE, cb)
    await f.remember("测试")
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_facade_remember_empty_content_raises(facade: tuple[MemoryFacade, MemoryHooks, LongTermStore]) -> None:
    """remember 空 content 抛 ValueError。"""
    f, _, _ = facade
    with pytest.raises(ValueError):
        await f.remember("")


@pytest.mark.asyncio
async def test_facade_recall_empty_query_returns_empty(facade: tuple[MemoryFacade, MemoryHooks, LongTermStore]) -> None:
    """recall 空 query 返回空。"""
    f, _, _ = facade
    assert await f.recall("") == []


@pytest.mark.asyncio
async def test_facade_recall_empty_db_returns_empty(facade: tuple[MemoryFacade, MemoryHooks, LongTermStore]) -> None:
    """recall 空 db 返回空。"""
    f, _, _ = facade
    assert await f.recall("anything") == []


@pytest.mark.asyncio
async def test_facade_recall_triggers_on_recall_per_hit(facade: tuple[MemoryFacade, MemoryHooks, LongTermStore]) -> None:
    """recall 每条 hit 触发一次 ON_RECALL。"""
    f, hooks, _ = facade
    await f.remember("咖啡")
    await f.remember("咖啡馆")
    seen: list[str] = []

    def cb(*, key: str, score: float) -> None:  # noqa: ARG001
        seen.append(key)

    hooks.register(MemoryEventType.ON_RECALL, cb)
    hits = await f.recall("咖啡", k=5, min_importance=0.0)
    assert len(seen) == len(hits)


@pytest.mark.asyncio
async def test_facade_format_for_prompt_empty_db(facade: tuple[MemoryFacade, MemoryHooks, LongTermStore]) -> None:
    """空 db 时 format_for_prompt 返回空字符串。"""
    f, _, _ = facade
    assert await f.format_for_prompt("anything") == ""


@pytest.mark.asyncio
async def test_facade_format_for_prompt_returns_markdown(facade: tuple[MemoryFacade, MemoryHooks, LongTermStore]) -> None:
    """召回后 format_for_prompt 返回 markdown 片段。"""
    f, _, _ = facade
    await f.remember("我喜欢喝美式咖啡")
    await f.remember("我住在上海浦东")
    block = await f.format_for_prompt("喝什么", k=5)
    assert "### 相关历史记忆" in block
    assert "咖啡" in block or "上海" in block


@pytest.mark.asyncio
async def test_facade_format_for_prompt_does_not_truncate(facade: tuple[MemoryFacade, MemoryHooks, LongTermStore]) -> None:
    """🆕 2026-10-12: **召回优先于 prompt 预算** —— ``max_chars`` 不再裁剪。

    旧行为(已废弃):``max_chars=50`` 会把 100+ 字符的 fact 截断。
    新行为:召回到什么原样注入什么,``max_chars`` 保留仅为签名兼容。

    为什么:按字符位置截断会把 fact **中段**的关键事实砍掉 ——
    实测宠物 task fact(8 轮 / 2678 字符)里"养了几个月,蜕过一次皮,
    足展 5-6 厘米"在位置 1279,被 ``long_budget=1200`` 砍没,LLM 于是
    回答"没记录过"。单条 fact 上限现由 ``.env`` 的
    ``SMARTBUTLER_MEMORY_MAX_FACT_CHARS`` 控制(默认 0=不截断)。
    """
    f, _, _ = facade
    await f.remember("内容 " * 50)  # 100+ 字符
    block = await f.format_for_prompt("内容", k=5, max_chars=50)
    # 内容完整注入,不因 max_chars=50 被砍
    assert "内容 " * 50 in block
    assert len(block) > 50


@pytest.mark.asyncio
async def test_truncate_middle_keeps_head_and_tail() -> None:
    """``_truncate_middle`` 砍中间保首尾(重新启用预算时的行为)。

    保留测试是为了锁住这个函数的语义 —— 它现在默认不生效
    (``max_fact_chars=0``),但一旦 ``.env`` 调大该值就会走到这里。
    """
    from smartbutler.emotion.memory.facade import _truncate_middle

    # limit=None → 原样返回
    text = "abcdefghij" * 30
    assert _truncate_middle(text, None) == text

    # 超限时首尾都在
    out = _truncate_middle(text, 100)
    assert len(out) <= 100
    assert out.startswith("a")
    assert out.endswith("j")
    assert "省略中间" in out


@pytest.mark.asyncio
async def test_truncate_middle_is_identity_within_limit() -> None:
    """未超限时 ``_truncate_middle`` 不改动内容。"""
    from smartbutler.emotion.memory.facade import _truncate_middle

    assert _truncate_middle("短文本", 100) == "短文本"


@pytest.mark.asyncio
async def test_facade_recall_crosses_users(facade: tuple[MemoryFacade, MemoryHooks, LongTermStore]) -> None:
    """🆕 2026-10-12: 管家记忆是全局池 —— 不按 source 分箱。

    旧行为(已废弃):alice 召回只看得到 alice 的记忆(source tag 过滤)。
    新行为:管家用单一身份服务所有人,任何用户都能召回所有人的相关事实,
    由 LLM 综合判断(male 说去上海 / female 说去北京 → 管家应同时看到两条,
    然后追问"要改成北京吗")。
    """
    f, _, _ = facade
    await f.remember("Alice 的旅行计划", ctx=MemoryContext(source="alice"))
    await f.remember("Bob 的购物清单", ctx=MemoryContext(source="bob"))
    # alice 召 → **也能**看到 bob 的(全局池)
    hits = await f.recall("计划", ctx=MemoryContext(source="alice"), k=5, min_importance=0.0)
    assert any("Alice" in h["content"] for h in hits)
    assert any("Bob" in h["content"] for h in hits)


@pytest.mark.asyncio
async def test_facade_recall_ignores_source_mismatch(facade: tuple[MemoryFacade, MemoryHooks, LongTermStore]) -> None:
    """回归:同一事实换个 source 存,另一个 source 也能召回到。

    实测踩坑(2026-10-12):``--user u-male`` 问"火龙果培训内容",
    库里存的是 ``source=u-male-v3-b-default``,tag 对不上 → 召回为空。
    这个测试锁死该行为不再回归。
    """
    f, _, _ = facade
    await f.remember(
        "软件需求分析培训内容有火龙果软件",
        ctx=MemoryContext(source="u-male-v3-b-default"),
    )
    # 用完全不同的 source 查 —— 仍应召回到
    hits = await f.recall(
        "火龙果 需求分析 培训", ctx=MemoryContext(source="u-male"), k=5,
        min_importance=0.0,
    )
    assert any("火龙果" in h["content"] for h in hits), (
        f"换个 source 就召不回了,说明 source 过滤没删干净:{hits}"
    )


@pytest.mark.asyncio
async def test_facade_recall_still_tags_source(facade: tuple[MemoryFacade, MemoryHooks, LongTermStore]) -> None:
    """🆕 2026-10-12: 不参与过滤 ≠ 不记录。

    source 仍写进 tags,供 CLI ``memory_cli recall --source`` 人工翻查。
    """
    f, _, _ = facade
    key = await f.remember("带 source 的记忆", ctx=MemoryContext(source="alice"))
    assert key
    hits = await f.recall("记忆", k=5, min_importance=0.0)
    tagged = [h for h in hits if h["content"] == "带 source 的记忆"]
    assert tagged, "召回结果里找不到刚存的记忆"
    assert "source:alice" in tagged[0]["tags"], (
        f"source 没写进 tags:{tagged[0]['tags']}"
    )


@pytest.mark.asyncio
async def test_facade_source_empty_sees_all(facade: tuple[MemoryFacade, MemoryHooks, LongTermStore]) -> None:
    """source=""(单用户模式)能召回所有 source 的记忆。"""
    f, _, _ = facade
    await f.remember("Alice 的事", ctx=MemoryContext(source="alice"))
    await f.remember("Bob 的事", ctx=MemoryContext(source="bob"))
    # 无 source → 召回全部
    hits = await f.recall("事", k=5, min_importance=0.0)
    assert any("Alice" in h["content"] for h in hits)
    assert any("Bob" in h["content"] for h in hits)


def test_facade_exposes_dependencies(facade: tuple[MemoryFacade, MemoryHooks, LongTermStore]) -> None:
    """facade 暴露 embedder / retriever 给 thinking 直接用。"""
    f, _, _ = facade
    assert f.embedder is not None
    assert f.retriever is not None


def test_memory_context_defaults() -> None:
    """MemoryContext 默认空字段。"""
    ctx = MemoryContext()
    assert ctx.user_id is None
    assert ctx.source == ""
    assert ctx.session_id is None


# ---------------------------------------------------------------------------
# 🆕 7.4 H3:MemoryFacade.remember 多模态 ref 测试
# 7.4 透传 image_ref / audio_ref 到 LongTermStore;7.9 启动时由
# EmbeddingGemma2Embedder 补 image_embedding。
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_facade_remember_with_image_ref(
    facade: tuple[MemoryFacade, MemoryHooks, LongTermStore],
) -> None:
    """7.4 H3:facade.remember 接受 image_ref,透传到 LongTermStore + modality tag。"""
    f, _, store = facade
    key = await f.remember(
        "看图说话",
        ctx=MemoryContext(source="camera"),
        image_ref="/data/butler/img/2026/10/14/xxx.jpg",
    )
    assert key
    # 透传到 store:查 payload 应含 image_ref
    payload = store.get_payload(key)
    assert payload is not None
    assert payload["image_ref"] == "/data/butler/img/2026/10/14/xxx.jpg"
    # tags 含 modality:image(供 7.9 按 modality 过滤)
    assert "modality:image" in payload["tags"]


@pytest.mark.asyncio
async def test_facade_remember_with_audio_ref(
    facade: tuple[MemoryFacade, MemoryHooks, LongTermStore],
) -> None:
    """7.4 H3:facade.remember 接受 audio_ref,透传到 LongTermStore + modality tag。"""
    f, _, store = facade
    key = await f.remember(
        "听音说话",
        ctx=MemoryContext(source="voice"),
        audio_ref="/data/butler/aud/2026/10/14/xxx.wav",
    )
    assert key
    payload = store.get_payload(key)
    assert payload is not None
    assert payload["audio_ref"] == "/data/butler/aud/2026/10/14/xxx.wav"
    assert "modality:audio" in payload["tags"]


@pytest.mark.asyncio
async def test_facade_remember_legacy_no_modality_tag(
    facade: tuple[MemoryFacade, MemoryHooks, LongTermStore],
) -> None:
    """7.4 H3 兼容性:老调用(无 image_ref / audio_ref)零破坏,tag 不带 modality:。"""
    f, _, store = facade
    key = await f.remember("老调用零破坏", ctx=MemoryContext(source="user"))
    payload = store.get_payload(key)
    assert payload is not None
    # 不应有 modality: 前缀的 tag
    for t in payload["tags"]:
        assert not t.startswith("modality:"), f"老调用不应该写 modality: tag,得到 {t}"
    # 也不应有 image_ref / audio_ref 字段
    assert "image_ref" not in payload
    assert "audio_ref" not in payload
