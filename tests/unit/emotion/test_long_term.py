"""长期记忆(LongTermStore)单元测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from smartbutler.emotion.memory import LongTermStore, create_long_term_store
from smartbutler.storage import QdrantStorage, StorageError


@pytest.fixture
def lt(tmp_path: Path) -> LongTermStore:
    """建一个嵌入式 LongTermStore,每个测试独立目录。"""
    q = QdrantStorage(
        path=tmp_path / "q",
        collection="t",
        vector_size=4,
        distance="Cosine",
    )
    return LongTermStore(q)


def _vec(base: list[float], shift: float) -> list[float]:
    """生成跟 base 偏移 shift 的 4 维向量,用于制造不同的相似度。"""
    return [v + shift for v in base]


def test_create_long_term_store_default_path() -> None:
    """不传 path 时应走默认 data/qdrant(嵌入式)。"""
    lt = create_long_term_store(collection="default_test", vector_size=4)
    assert lt.mode == "embedded"
    assert lt.collection == "default_test"


def test_store_and_search(lt: LongTermStore) -> None:
    """存 3 条向量 + 检索,最近邻应是 base。"""
    base = [0.1, 0.2, 0.3, 0.4]
    lt.store_memory("a", base, "a content", importance=0.9, tags=["food"])
    lt.store_memory("b", _vec(base, 0.5), "b content", importance=0.4)
    lt.store_memory("c", _vec(base, 1.0), "c content", importance=0.1)
    hits = lt.search_memory(base, k=3)
    assert len(hits) == 3
    # a 应该最相似(距离 0)
    assert hits[0]["key"] == "a"
    assert hits[0]["score"] >= hits[-1]["score"]


def test_search_filters_by_min_importance(lt: LongTermStore) -> None:
    """min_importance 应过滤掉低分记忆。"""
    base = [0.1, 0.2, 0.3, 0.4]
    lt.store_memory("high", base, "hi", importance=0.9)
    lt.store_memory("low", _vec(base, 0.01), "lo", importance=0.1)
    hits = lt.search_memory(base, k=5, min_importance=0.5)
    keys = [h["key"] for h in hits]
    assert "high" in keys
    assert "low" not in keys


def test_search_filters_by_tag(lt: LongTermStore) -> None:
    """tag 过滤。"""
    base = [0.1, 0.2, 0.3, 0.4]
    lt.store_memory("food1", base, "noodle", tags=["food"])
    lt.store_memory("drink1", _vec(base, 0.1), "tea", tags=["drink"])
    hits = lt.search_memory(base, k=5, tag="food")
    keys = [h["key"] for h in hits]
    assert "food1" in keys
    assert "drink1" not in keys


def test_delete_memory(lt: LongTermStore) -> None:
    """delete_memory 应能按 key 删。"""
    import asyncio

    base = [0.1, 0.2, 0.3, 0.4]
    lt.store_memory("a", base, "content")
    assert asyncio.run(lt.delete_memory("a")) is True
    hits = lt.search_memory(base, k=5)
    assert all(h["key"] != "a" for h in hits)


def test_count(lt: LongTermStore) -> None:
    """count 应反映集合条数。"""
    base = [0.1, 0.2, 0.3, 0.4]
    assert lt.count() == 0
    lt.store_memory("a", base, "x")
    lt.store_memory("b", _vec(base, 0.1), "y")
    assert lt.count() == 2


def test_long_term_store_rejects_non_qdrant() -> None:
    """LongTermStore 拒绝非 QdrantStorage。"""
    with pytest.raises(TypeError):
        LongTermStore("not a qdrant")  # type: ignore[arg-type]


def test_factory_path_and_url_conflict_raises(tmp_path: Path) -> None:
    """url 和 path 同时给 → QdrantStorage 拒。"""
    from smartbutler.storage.qdrant import QdrantStorage

    with pytest.raises(StorageError):
        QdrantStorage(path=tmp_path / "q", url="http://localhost:6333")
