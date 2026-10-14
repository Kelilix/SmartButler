"""QdrantStorage 单元测试（嵌入式模式）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from smartbutler.storage import QdrantStorage, StorageError


@pytest.fixture
def store(tmp_path: Path) -> QdrantStorage:
    s = QdrantStorage(
        path=tmp_path / "qdrant",
        collection="t",
        vector_size=4,
        distance="Cosine",
    )
    yield s
    # sync close in fixture is OK since client.close is sync
    try:
        s._client.close()  # type: ignore[attr-defined]
    except Exception:
        pass


def test_constructor_requires_path_or_url(tmp_path: Path) -> None:
    """path 和 url 都必填其一,都填也错。"""
    with pytest.raises(StorageError):
        QdrantStorage()


def test_constructor_rejects_both(tmp_path: Path) -> None:
    with pytest.raises(StorageError):
        QdrantStorage(path=tmp_path / "q", url="http://x")


def test_constructor_rejects_bad_distance(tmp_path: Path) -> None:
    with pytest.raises(StorageError):
        QdrantStorage(path=tmp_path / "q", distance="Bogus")


def test_embedded_mode() -> None:
    s = QdrantStorage(path=Path("/tmp/_nonexistent_qdrant_test"))
    assert s.mode == "embedded"


def test_ensure_collection(tmp_path: Path) -> None:
    """首次 ensure_collection 应建出集合。"""
    s = QdrantStorage(path=tmp_path / "q", vector_size=4, collection="c1")
    s._ensure_collection()
    assert s._client.collection_exists("c1") is True


def test_upsert_with_vector_and_search(store: QdrantStorage) -> None:
    """原生向量 API:写入 + 语义检索。"""
    store.upsert_with_vector("a", [0.1, 0.2, 0.3, 0.4], {"tag": "x"})
    store.upsert_with_vector("b", [0.2, 0.3, 0.4, 0.5], {"tag": "y"})
    hits = store.similarity_search([0.1, 0.2, 0.3, 0.4], limit=2)
    assert len(hits) >= 1
    keys = [h[0] for h in hits]
    assert "a" in keys


def test_upsert_with_vector_rejects_size_mismatch(store: QdrantStorage) -> None:
    """向量维度不对就抛错。"""
    with pytest.raises(StorageError):
        store.upsert_with_vector("a", [0.1, 0.2, 0.3], {"tag": "x"})


def test_kv_view_set_get(store: QdrantStorage) -> None:
    """BaseStorage KV 视图(Phase 6.1 的"管家偏好"等结构化数据走这条)。"""
    import asyncio

    asyncio.run(_kv_set_get(store))


async def _kv_set_get(store: QdrantStorage) -> None:
    await store.set("user:alice", b'{"name":"alice"}')
    v = await store.get("user:alice")
    assert v == b'{"name":"alice"}'


def test_kv_view_exists_delete(store: QdrantStorage) -> None:
    import asyncio

    asyncio.run(_kv_exists_delete(store))


async def _kv_exists_delete(store: QdrantStorage) -> None:
    await store.set("k", b"v")
    assert await store.exists("k") is True
    assert await store.delete("k") is True
    assert await store.exists("k") is False


def test_kv_view_list_keys(store: QdrantStorage) -> None:
    import asyncio

    asyncio.run(_kv_list(store))


async def _kv_list(store: QdrantStorage) -> None:
    await store.set("user:1", b"a")
    await store.set("user:2", b"b")
    await store.set("config:x", b"c")
    keys = await store.list_keys(prefix="user:")
    assert set(keys) == {"user:1", "user:2"}
