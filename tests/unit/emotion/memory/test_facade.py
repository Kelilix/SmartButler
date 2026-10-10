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
async def test_facade_format_for_prompt_max_chars_truncate(facade: tuple[MemoryFacade, MemoryHooks, LongTermStore]) -> None:
    """max_chars 截断。"""
    f, _, _ = facade
    await f.remember("内容 " * 50)  # 100+ 字符
    block = await f.format_for_prompt("内容", k=5, max_chars=50)
    assert len(block) <= 50 + 3  # 允许 "..." 后缀


@pytest.mark.asyncio
async def test_facade_source_isolation(facade: tuple[MemoryFacade, MemoryHooks, LongTermStore]) -> None:
    """不同 source 的记忆不串。"""
    f, _, _ = facade
    await f.remember("Alice 的旅行计划", ctx=MemoryContext(source="alice"))
    await f.remember("Bob 的购物清单", ctx=MemoryContext(source="bob"))
    # alice 召 → 只看到 alice 的
    hits = await f.recall("计划", ctx=MemoryContext(source="alice"), k=5, min_importance=0.0)
    assert all("Alice" in h["content"] for h in hits)
    assert not any("Bob" in h["content"] for h in hits)


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
