"""forgetting.py 单元测试(Phase 6.3 P1)。"""

from __future__ import annotations

from datetime import datetime, UTC, timedelta
from pathlib import Path

import pytest

from smartbutler.capabilities.embedding import StubEmbedder
from smartbutler.emotion.memory.forgetting import (
    ForgetReason,
    ForgettingService,
)
from smartbutler.emotion.memory.long_term import LongTermStore
from smartbutler.storage import QdrantStorage


@pytest.fixture
def store_with_embedder(tmp_path: Path) -> tuple[LongTermStore, StubEmbedder]:
    q = QdrantStorage(path=tmp_path / "q", collection="t", vector_size=64)
    s = LongTermStore(q)
    return s, StubEmbedder(dimension=64)


@pytest.mark.asyncio
async def test_forget_by_user_request_soft_deletes(
    store_with_embedder: tuple[LongTermStore, StubEmbedder],
) -> None:
    """user_request 软删除成功 → payload.deleted=True。"""
    store, embedder = store_with_embedder
    emb = await embedder.embed("测试内容")
    store.store_memory("k1", emb, "测试内容", importance=0.7)
    fs = ForgettingService(store)
    result = await fs.forget_by_user_request("k1")
    assert result.forgotten == 1
    assert result.reason == ForgetReason.USER_REQUEST
    pl = store.get_payload("k1")
    assert pl is not None
    assert pl.get("deleted") is True
    assert pl.get("deleted_reason") == "user_request"


@pytest.mark.asyncio
async def test_forget_missing_key_returns_skipped(
    store_with_embedder: tuple[LongTermStore, StubEmbedder],
) -> None:
    """不存在的 key → skipped=1,forgotten=0。"""
    store, _ = store_with_embedder
    fs = ForgettingService(store)
    result = await fs.forget_by_user_request("nonexistent")
    assert result.forgotten == 0
    assert result.skipped == 1


@pytest.mark.asyncio
async def test_forget_already_deleted_idempotent(
    store_with_embedder: tuple[LongTermStore, StubEmbedder],
) -> None:
    """已软删的 key 再删 → 返 skipped(不重复打戳)。"""
    store, embedder = store_with_embedder
    emb = await embedder.embed("dup")
    store.store_memory("dup", emb, "dup", importance=0.5)
    fs = ForgettingService(store)
    r1 = await fs.forget_by_user_request("dup")
    assert r1.forgotten == 1
    # 第二次调:已软删,被识别为 skipped
    r2 = await fs.forget_by_user_request("dup")
    assert r2.forgotten == 0
    assert r2.skipped == 1
    # payload 仍只有一份软删戳
    pl = store.get_payload("dup")
    assert pl.get("deleted") is True
    assert pl.get("deleted_reason") == "user_request"


@pytest.mark.asyncio
async def test_forget_by_ttl_picks_low_importance_old(
    store_with_embedder: tuple[LongTermStore, StubEmbedder],
) -> None:
    """TTL 规则:importance < 0.2 且 age > 7 天 → 软删。"""
    store, embedder = store_with_embedder
    emb = await embedder.embed("low")
    # 存两条:低 importance / 高 importance
    old_date = (datetime.now(UTC) - timedelta(days=30)).isoformat()
    store.store_memory("low", emb, "low", importance=0.1, created_at=datetime.fromisoformat(old_date))
    store.store_memory("hi", emb, "hi", importance=0.8, created_at=datetime.fromisoformat(old_date))
    fs = ForgettingService(store)
    result = await fs.forget_by_ttl()
    assert result.forgotten == 1
    pl_low = store.get_payload("low")
    pl_hi = store.get_payload("hi")
    assert pl_low is not None and pl_low.get("deleted") is True
    assert pl_hi is not None and pl_hi.get("deleted") is not True


@pytest.mark.asyncio
async def test_forget_by_conflict_only_when_new_higher(
    store_with_embedder: tuple[LongTermStore, StubEmbedder],
) -> None:
    """conflict:仅当 new > old importance 才忘旧。"""
    store, embedder = store_with_embedder
    emb = await embedder.embed("c")
    store.store_memory("old", emb, "old", importance=0.5)
    store.store_memory("new", emb, "new", importance=0.3)
    fs = ForgettingService(store)
    # new < old → 不忘
    result = await fs.forget_by_conflict("old", "new", new_importance=0.3, old_importance=0.5)
    assert result.forgotten == 0
    # new > old → 忘
    result = await fs.forget_by_conflict("old", "new", new_importance=0.9, old_importance=0.5)
    assert result.forgotten == 1
    pl = store.get_payload("old")
    assert pl is not None and pl.get("deleted") is True


@pytest.mark.asyncio
async def test_forget_by_promote_batch(
    store_with_embedder: tuple[LongTermStore, StubEmbedder],
) -> None:
    """promote:批量软删源 fact。"""
    store, embedder = store_with_embedder
    emb = await embedder.embed("promote")
    store.store_memory("s1", emb, "s1", importance=0.5)
    store.store_memory("s2", emb, "s2", importance=0.5)
    store.store_memory("kept", emb, "kept", importance=0.5)
    fs = ForgettingService(store)
    result = await fs.forget_by_promote(["s1", "s2", "missing"], new_key="new_fact")
    assert result.forgotten == 2
    assert result.skipped == 1
    assert set(result.forgotten_keys) == {"s1", "s2"}
    assert "missing" in result.skipped_keys
    # kept 不该被删
    pl = store.get_payload("kept")
    assert pl is not None and pl.get("deleted") is not True


@pytest.mark.asyncio
async def test_restore_undeletes(
    store_with_embedder: tuple[LongTermStore, StubEmbedder],
) -> None:
    """restore 恢复软删除的事实。"""
    store, embedder = store_with_embedder
    emb = await embedder.embed("restore")
    store.store_memory("r", emb, "restore", importance=0.5)
    fs = ForgettingService(store)
    await fs.forget_by_user_request("r")
    ok = await fs.restore("r")
    assert ok is True
    pl = store.get_payload("r")
    assert pl is not None
    assert pl.get("deleted") is not True
    assert "deleted_at" not in pl
    assert "deleted_reason" not in pl


@pytest.mark.asyncio
async def test_restore_not_deleted_returns_false(
    store_with_embedder: tuple[LongTermStore, StubEmbedder],
) -> None:
    """未软删的事实调 restore → False。"""
    store, embedder = store_with_embedder
    emb = await embedder.embed("not_deleted")
    store.store_memory("n", emb, "not_deleted", importance=0.5)
    fs = ForgettingService(store)
    assert await fs.restore("n") is False


def test_get_reason_returns_enum(tmp_path: Path) -> None:
    """get_reason 返 ForgetReason 枚举(非软删返 None)。"""
    q = QdrantStorage(path=tmp_path / "q", collection="t", vector_size=64)
    s = LongTermStore(q)
    embedder = StubEmbedder(dimension=64)
    import asyncio
    emb = asyncio.run(embedder.embed("r"))
    s.store_memory("x", emb, "x", importance=0.5)
    fs = ForgettingService(s)
    # 未软删
    assert fs.get_reason("x") is None
    assert fs.get_reason("missing") is None
