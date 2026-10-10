"""QdrantStorage.similarity_search_with_filters 单测(Phase 6.2 P0)。

验证:
- min_importance 走 Range 服务端过滤
- tag 走 MatchValue 精确匹配
- 二者 AND 组合
- 都不传 = 不过滤
- 维度不一致抛错
"""

from __future__ import annotations

from pathlib import Path

import pytest

from smartbutler.storage import QdrantStorage, StorageError


@pytest.fixture
def store(tmp_path: Path) -> QdrantStorage:
    s = QdrantStorage(
        path=tmp_path / "q",
        collection="t",
        vector_size=4,
    )
    yield s
    try:
        s._client.close()  # type: ignore[attr-defined]
    except Exception:
        pass


def test_filters_min_importance_server_side(store: QdrantStorage) -> None:
    """min_importance 过滤走服务端。"""
    store.upsert_with_vector("hi", [0.1, 0.2, 0.3, 0.4], {"importance": 0.9, "tags": []})
    store.upsert_with_vector("lo", [0.1, 0.2, 0.3, 0.4], {"importance": 0.1, "tags": []})
    hits = store.similarity_search_with_filters(
        [0.1, 0.2, 0.3, 0.4], k=5, min_importance=0.5,
    )
    keys = [h[0] for h in hits]
    assert "hi" in keys
    assert "lo" not in keys


def test_filters_tag_match(store: QdrantStorage) -> None:
    """tag 精确匹配。"""
    store.upsert_with_vector("a", [0.1, 0.2, 0.3, 0.4], {"importance": 0.5, "tags": ["food"]})
    store.upsert_with_vector("b", [0.1, 0.2, 0.3, 0.4], {"importance": 0.5, "tags": ["drink"]})
    hits = store.similarity_search_with_filters(
        [0.1, 0.2, 0.3, 0.4], k=5, tag="food",
    )
    keys = [h[0] for h in hits]
    assert "a" in keys
    assert "b" not in keys


def test_filters_combined_and(store: QdrantStorage) -> None:
    """min_importance + tag 组合 AND。"""
    # 满足两条的: 命中
    store.upsert_with_vector(
        "match",
        [0.1, 0.2, 0.3, 0.4],
        {"importance": 0.9, "tags": ["food"]},
    )
    # 缺一个的: 不命中
    store.upsert_with_vector(
        "low_imp",
        [0.1, 0.2, 0.3, 0.4],
        {"importance": 0.1, "tags": ["food"]},
    )
    store.upsert_with_vector(
        "wrong_tag",
        [0.1, 0.2, 0.3, 0.4],
        {"importance": 0.9, "tags": ["drink"]},
    )
    hits = store.similarity_search_with_filters(
        [0.1, 0.2, 0.3, 0.4], k=5, min_importance=0.5, tag="food",
    )
    keys = [h[0] for h in hits]
    assert "match" in keys
    assert "low_imp" not in keys
    assert "wrong_tag" not in keys


def test_filters_none_returns_all(store: QdrantStorage) -> None:
    """min_importance=None + tag=None = 不过滤。"""
    store.upsert_with_vector("a", [0.1, 0.2, 0.3, 0.4], {"importance": 0.1, "tags": []})
    store.upsert_with_vector("b", [0.5, 0.6, 0.7, 0.8], {"importance": 0.9, "tags": []})
    hits = store.similarity_search_with_filters(
        [0.1, 0.2, 0.3, 0.4], k=5,
    )
    assert len(hits) >= 2


def test_filters_rejects_wrong_dimension(store: QdrantStorage) -> None:
    """向量维度不匹配抛 StorageError。"""
    with pytest.raises(StorageError):
        store.similarity_search_with_filters([0.1, 0.2], k=5)


def test_filters_handles_tag_with_colon(store: QdrantStorage) -> None:
    """tag 含冒号(如 source:alice)能精确匹配(Qdrant 数组元素匹配)。"""
    store.upsert_with_vector(
        "a",
        [0.1, 0.2, 0.3, 0.4],
        {"importance": 0.5, "tags": ["source:alice"]},
    )
    store.upsert_with_vector(
        "b",
        [0.1, 0.2, 0.3, 0.4],
        {"importance": 0.5, "tags": ["source:bob"]},
    )
    hits = store.similarity_search_with_filters(
        [0.1, 0.2, 0.3, 0.4], k=5, tag="source:alice",
    )
    keys = [h[0] for h in hits]
    assert "a" in keys
    assert "b" not in keys
