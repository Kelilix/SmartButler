"""vacuum.py 单元测试(Phase 6.3 P1)。"""

from __future__ import annotations

from datetime import datetime, UTC, timedelta
from pathlib import Path

import pytest

from smartbutler.capabilities.embedding import StubEmbedder
from smartbutler.emotion.memory.archive_store import ArchiveStore
from smartbutler.emotion.memory.forgetting import ForgettingService
from smartbutler.emotion.memory.long_term import LongTermStore
from smartbutler.emotion.memory.vacuum import VacuumService
from smartbutler.storage import QdrantStorage


@pytest.fixture
def vacuum_setup(tmp_path: Path) -> tuple[VacuumService, LongTermStore, ArchiveStore, StubEmbedder]:
    q = QdrantStorage(path=tmp_path / "q", collection="t", vector_size=64)
    s = LongTermStore(q)
    embedder = StubEmbedder(dimension=64)
    arc = ArchiveStore(tmp_path / "arc.db")
    vs = VacuumService(store=s, archive=arc, embedder=embedder)
    return vs, s, arc, embedder


@pytest.mark.asyncio
async def test_vacuum_dry_run_no_changes(
    vacuum_setup: tuple[VacuumService, LongTermStore, ArchiveStore, StubEmbedder],
) -> None:
    """dry_run 不动数据,只统计。"""
    vs, store, archive, embedder = vacuum_setup
    emb = await embedder.embed("x")
    store.store_memory("k1", emb, "x", importance=0.5)
    fs = ForgettingService(store)
    await fs.forget_by_user_request("k1")
    # 人为改 deleted_at 让它"超过 7 天"
    pl = store.get_payload("k1")
    pl["deleted_at"] = (datetime.now(UTC) - timedelta(days=10)).isoformat()
    store.qdrant.upsert_with_vector("k1", emb, pl)
    # 跑 dry_run
    report = vs.vacuum_once(dry_run=True)
    assert report.to_archive == 1
    assert report.archived_now == 0  # dry_run 不动
    # 数据原状
    assert store.get_payload("k1") is not None
    assert archive.count() == 0


@pytest.mark.asyncio
async def test_vacuum_archives_old_deleted(
    vacuum_setup: tuple[VacuumService, LongTermStore, ArchiveStore, StubEmbedder],
) -> None:
    """软删除 > 7 天的被搬进 archived SQLite + 从 Qdrant 删。"""
    vs, store, archive, embedder = vacuum_setup
    emb = await embedder.embed("x")
    store.store_memory("k1", emb, "x", importance=0.5)
    fs = ForgettingService(store)
    await fs.forget_by_user_request("k1")
    # 改 deleted_at 到 10 天前
    pl = store.get_payload("k1")
    pl["deleted_at"] = (datetime.now(UTC) - timedelta(days=10)).isoformat()
    store.qdrant.upsert_with_vector("k1", emb, pl)
    # 真跑
    report = vs.vacuum_once(dry_run=False)
    assert report.archived_now == 1
    # Qdrant 已无 k1
    assert store.get_payload("k1") is None
    # archived 有 k1
    assert archive.count() == 1
    item = archive.get("k1")
    assert item is not None
    assert item.content == "x"
    assert item.deleted_reason == "user_request"


@pytest.mark.asyncio
async def test_vacuum_purges_old_archived(
    vacuum_setup: tuple[VacuumService, LongTermStore, ArchiveStore, StubEmbedder],
) -> None:
    """archived 超过 30 天的被物理删。"""
    vs, store, archive, embedder = vacuum_setup
    # 预先在 archive 加 1 条 40 天前 archived
    archive.add(ArchivedMemory := __import__(
        "smartbutler.emotion.memory.archive_store",
        fromlist=["ArchivedMemory"],
    ).ArchivedMemory(
        key="old", content="old", importance=0.5,
        archived_at=(datetime.now(UTC) - timedelta(days=40)).isoformat(),
    ))
    assert archive.count() == 1
    report = vs.vacuum_once(dry_run=False)
    assert report.to_purge == 1
    assert report.purged_now == 1
    assert archive.count() == 0


@pytest.mark.asyncio
async def test_list_deleted_filters(
    vacuum_setup: tuple[VacuumService, LongTermStore, ArchiveStore, StubEmbedder],
) -> None:
    """list_deleted 按 level 过滤。"""
    vs, store, archive, embedder = vacuum_setup
    emb = await embedder.embed("d")
    store.store_memory("d1", emb, "d1", importance=0.5, level="daily")
    store.store_memory("w1", emb, "w1", importance=0.5, level="weekly")
    fs = ForgettingService(store)
    await fs.forget_by_user_request("d1")
    await fs.forget_by_user_request("w1")
    # list all
    items = vs.list_deleted(level="any", limit=10)
    assert len(items) == 2
    # filter by daily
    items = vs.list_deleted(level="daily", limit=10)
    assert len(items) == 1
    assert items[0].key == "d1"
    assert items[0].level == "daily"


@pytest.mark.asyncio
async def test_purge_now_requires_confirm(
    vacuum_setup: tuple[VacuumService, LongTermStore, ArchiveStore, StubEmbedder],
) -> None:
    """purge_now 必须 confirm=yes。"""
    vs, store, archive, embedder = vacuum_setup
    emb = await embedder.embed("p")
    store.store_memory("p1", emb, "p1", importance=0.5)
    # confirm=no → False
    assert vs.purge_now("p1", confirm="no") is False
    # 数据还在
    assert store.get_payload("p1") is not None
    # confirm=yes → True
    assert vs.purge_now("p1", confirm="yes") is True
    # 数据没了
    assert store.get_payload("p1") is None


def test_vacuum_stats_reports_counts(
    vacuum_setup: tuple[VacuumService, LongTermStore, ArchiveStore, StubEmbedder],
) -> None:
    """vacuum_stats 返回 active / deleted_recoverable / archived。"""
    vs, _, _, _ = vacuum_setup
    stats = vs.vacuum_stats()
    assert "active" in stats
    assert "deleted_recoverable" in stats
    assert "archived" in stats
    assert "qdrant_storage_bytes" in stats
    assert "archive_storage_bytes" in stats
    assert "total_storage_bytes" in stats
    # 空 store → 全 0
    assert stats["active"] == 0
    assert stats["archived"] == 0


@pytest.mark.asyncio
async def test_restore_from_qdrant_works(
    vacuum_setup: tuple[VacuumService, LongTermStore, ArchiveStore, StubEmbedder],
) -> None:
    """restore 从 Qdrant 主体恢复软删除的事实。"""
    vs, store, _, embedder = vacuum_setup
    emb = await embedder.embed("r")
    store.store_memory("r1", emb, "r1", importance=0.5)
    fs = ForgettingService(store)
    await fs.forget_by_user_request("r1")
    # 验证已软删
    assert store.get_payload("r1").get("deleted") is True
    # restore
    ok = vs.restore("r1")
    assert ok is True
    pl = store.get_payload("r1")
    assert pl is not None
    assert pl.get("deleted") is not True


@pytest.mark.asyncio
async def test_restore_from_archive_re_embeds(
    vacuum_setup: tuple[VacuumService, LongTermStore, ArchiveStore, StubEmbedder],
) -> None:
    """restore 从 archived SQLite 恢复(re-embed + upsert)。"""
    vs, store, archive, embedder = vacuum_setup
    # 预先在 archive 加 1 条
    archive.add(ArchivedMemory := __import__(
        "smartbutler.emotion.memory.archive_store",
        fromlist=["ArchivedMemory"],
    ).ArchivedMemory(
        key="arc1", content="archived content", importance=0.7, level="daily",
    ))
    assert store.get_payload("arc1") is None
    ok = vs.restore("arc1")
    assert ok is True
    # Qdrant 有了
    pl = store.get_payload("arc1")
    assert pl is not None
    assert pl.get("content") == "archived content"
    # archive 删了
    assert archive.get("arc1") is None


def test_restore_not_found_returns_false(
    vacuum_setup: tuple[VacuumService, LongTermStore, ArchiveStore, StubEmbedder],
) -> None:
    """restore 不存在的 key → False。"""
    vs, _, _, _ = vacuum_setup
    assert vs.restore("nonexistent") is False


# ------------------- 6.4 增强:age-summary / bulk_purge_* -------------------

@pytest.mark.asyncio
async def test_count_deleted_by_age_buckets(
    vacuum_setup: tuple[VacuumService, LongTermStore, ArchiveStore, StubEmbedder],
) -> None:
    """count_deleted_by_age 按年龄分桶。"""
    vs, store, _, embedder = vacuum_setup
    emb = await embedder.embed("x")
    # 存 3 条 + 软删 + 改 deleted_at
    fs = ForgettingService(store)
    cases = [
        ("new", 1),       # <7d
        ("mid", 20),      # 7-30d
        ("old", 100),     # 90-180d
    ]
    for key, days_ago in cases:
        store.store_memory(key, emb, key, importance=0.5)
        await fs.forget_by_user_request(key)
        # 改 deleted_at
        pl = store.get_payload(key)
        pl["deleted_at"] = (datetime.now(UTC) - timedelta(days=days_ago)).isoformat()
        store.qdrant.upsert_with_vector(key, emb, pl)
    buckets = vs.count_deleted_by_age()
    assert buckets["<7d"] == 1
    assert buckets["7-30d"] == 1
    assert buckets["90-180d"] == 1
    assert buckets["_total"] == 3


def test_count_deleted_by_age_empty(
    vacuum_setup: tuple[VacuumService, LongTermStore, ArchiveStore, StubEmbedder],
) -> None:
    """空库 count 全 0。"""
    vs, _, _, _ = vacuum_setup
    buckets = vs.count_deleted_by_age()
    assert buckets["_total"] == 0
    for label in ("<7d", "7-30d", "30-90d", "90-180d", "180-365d", ">365d"):
        assert buckets[label] == 0


@pytest.mark.asyncio
async def test_bulk_purge_before_requires_confirm(
    vacuum_setup: tuple[VacuumService, LongTermStore, ArchiveStore, StubEmbedder],
) -> None:
    """bulk_purge_before 没 confirm → 0 操作。"""
    vs, store, _, embedder = vacuum_setup
    emb = await embedder.embed("x")
    store.store_memory("k1", emb, "k1", importance=0.5)
    fs = ForgettingService(store)
    await fs.forget_by_user_request("k1")
    pl = store.get_payload("k1")
    pl["deleted_at"] = (datetime.now(UTC) - timedelta(days=30)).isoformat()
    store.qdrant.upsert_with_vector("k1", emb, pl)
    result = vs.bulk_purge_before(older_than_days=7, confirm="no")
    assert result["purged"] == 0
    assert result["dry_run"] is True
    # 数据原状
    assert store.get_payload("k1") is not None


@pytest.mark.asyncio
async def test_bulk_purge_before_enforces_7d_grace(
    vacuum_setup: tuple[VacuumService, LongTermStore, ArchiveStore, StubEmbedder],
) -> None:
    """bulk_purge_before < 7 天 → 拒执行(反悔期)。"""
    vs, _, _, _ = vacuum_setup
    result = vs.bulk_purge_before(older_than_days=3, confirm="yes")
    assert result["purged"] == 0
    assert "反悔期" in result["errors"][0]


@pytest.mark.asyncio
async def test_bulk_purge_before_purges_old(
    vacuum_setup: tuple[VacuumService, LongTermStore, ArchiveStore, StubEmbedder],
) -> None:
    """bulk_purge_before ≥ 7 天 → 真物理删。"""
    vs, store, _, embedder = vacuum_setup
    emb = await embedder.embed("x")
    # 1 条 30 天前软删(该删), 1 条 5 天前软删(不该删,反悔期内)
    store.store_memory("old", emb, "old", importance=0.5)
    store.store_memory("recent", emb, "recent", importance=0.5)
    fs = ForgettingService(store)
    await fs.forget_by_user_request("old")
    await fs.forget_by_user_request("recent")
    pl = store.get_payload("old")
    pl["deleted_at"] = (datetime.now(UTC) - timedelta(days=30)).isoformat()
    store.qdrant.upsert_with_vector("old", emb, pl)
    pl = store.get_payload("recent")
    pl["deleted_at"] = (datetime.now(UTC) - timedelta(days=5)).isoformat()
    store.qdrant.upsert_with_vector("recent", emb, pl)
    # 删 ≥ 7 天的
    result = vs.bulk_purge_before(older_than_days=7, confirm="yes")
    assert result["purged"] == 1
    assert result["requested"] == 1
    # old 真删了
    assert store.get_payload("old") is None
    # recent 还在
    assert store.get_payload("recent") is not None


def test_bulk_purge_archived_empties_old(
    vacuum_setup: tuple[VacuumService, LongTermStore, ArchiveStore, StubEmbedder],
) -> None:
    """bulk_purge_archived 清 archived_at 超过 N 天的 SQLite 记录。"""
    vs, _, archive, _ = vacuum_setup
    # 预先 add 2 条:1 条 40 天前(该清), 1 条 1 天前(不该清)
    for i, days_ago in enumerate([40, 1]):
        archive.add(ArchivedMemory := __import__(
            "smartbutler.emotion.memory.archive_store",
            fromlist=["ArchivedMemory"],
        ).ArchivedMemory(
            key=f"a{i}", content=f"a{i}", importance=0.5,
            archived_at=(datetime.now(UTC) - timedelta(days=days_ago)).isoformat(),
        ))
    result = vs.bulk_purge_archived(older_than_days=30, confirm="yes")
    assert result["purged"] == 1
    assert archive.get("a0") is None
    assert archive.get("a1") is not None


@pytest.mark.asyncio
async def test_age_summary_to_purge_before_e2e(
    vacuum_setup: tuple[VacuumService, LongTermStore, ArchiveStore, StubEmbedder],
) -> None:
    """端到端:age-summary 发现 30 天前的 → purge-before 删掉。"""
    vs, store, _, embedder = vacuum_setup
    emb = await embedder.embed("e2e")
    # 加 1 条 30 天前软删
    store.store_memory("e2e_old", emb, "e2e_old", importance=0.5)
    fs = ForgettingService(store)
    await fs.forget_by_user_request("e2e_old")
    pl = store.get_payload("e2e_old")
    pl["deleted_at"] = (datetime.now(UTC) - timedelta(days=30)).isoformat()
    store.qdrant.upsert_with_vector("e2e_old", emb, pl)
    # 1. age-summary 看到 30-90d 有 1 条
    buckets = vs.count_deleted_by_age()
    assert buckets["30-90d"] == 1
    # 2. 干跑 purge-before dry-run(默认)
    items = vs.list_deleted(limit=50_000, older_than_days=7)
    assert len(items) == 1
    # 3. 真跑
    result = vs.bulk_purge_before(older_than_days=7, confirm="yes")
    assert result["purged"] == 1
    # 4. 验证
    assert store.get_payload("e2e_old") is None
    buckets2 = vs.count_deleted_by_age()
    assert buckets2["30-90d"] == 0
