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


# ---------------------------------------------------------------------------
# 🆕 7.4 H2:LongTermStore 多模态 payload 字段测试
# ---------------------------------------------------------------------------


def test_store_image_ref_and_audio_ref(lt: LongTermStore) -> None:
    """7.4 H2:store_memory 支持 image_ref / audio_ref 两个新字段。"""
    base = [0.1, 0.2, 0.3, 0.4]
    lt.store_memory(
        "img_key", base, "看图说话",
        image_ref="/data/butler/img/2026/10/14/xxx.jpg",
        audio_ref=None,
    )
    lt.store_memory(
        "aud_key", base, "听音说话",
        image_ref=None,
        audio_ref="/data/butler/aud/2026/10/14/xxx.wav",
    )
    # 检索时应能透传
    hits = lt.search_memory(base, k=5)
    by_key = {h["key"]: h for h in hits}
    assert by_key["img_key"]["image_ref"] == "/data/butler/img/2026/10/14/xxx.jpg"
    assert "audio_ref" not in by_key["img_key"]  # None 时不存字段
    assert by_key["aud_key"]["audio_ref"] == "/data/butler/aud/2026/10/14/xxx.wav"
    assert "image_ref" not in by_key["aud_key"]


def test_store_image_embedding_and_model(lt: LongTermStore) -> None:
    """7.4 H2:store_memory 支持 image_embedding / embedding_model 字段(7.9 启动时由 EmbeddingGemma2Embedder 写)。"""
    base = [0.1, 0.2, 0.3, 0.4]
    img_vec = [0.5, 0.6, 0.7, 0.8, 0.9]  # 任意长度(768 维在真实场景)
    lt.store_memory(
        "img_emb_key", base, "image fact",
        image_ref="/tmp/xxx.jpg",
        image_embedding=img_vec,
        embedding_model="embeddinggemma-2",
    )
    hits = lt.search_memory(base, k=5)
    by_key = {h["key"]: h for h in hits}
    assert by_key["img_emb_key"]["image_embedding"] == img_vec
    assert by_key["img_emb_key"]["embedding_model"] == "embeddinggemma-2"


def test_store_legacy_no_multimodal_fields(lt: LongTermStore) -> None:
    """7.4 H2 兼容性:老调用(只传老字段)零破坏,payload 没有 image_ref 等字段。"""
    base = [0.1, 0.2, 0.3, 0.4]
    lt.store_memory("legacy", base, "old style", importance=0.5, tags=["x"])
    hits = lt.search_memory(base, k=5)
    by_key = {h["key"]: h for h in hits}
    assert "image_ref" not in by_key["legacy"]
    assert "audio_ref" not in by_key["legacy"]
    assert "image_embedding" not in by_key["legacy"]
    assert "embedding_model" not in by_key["legacy"]


def test_payload_constants_exposed() -> None:
    """7.4 H2:4 个多模态字段常量在 __all__ 里(供 7.9 EmbeddingGemma2Embedder 复用)。"""
    from smartbutler.emotion.memory import long_term as lt_mod
    assert "PAYLOAD_IMAGE_REF" in lt_mod.__all__
    assert "PAYLOAD_AUDIO_REF" in lt_mod.__all__
    assert "PAYLOAD_IMAGE_EMBEDDING" in lt_mod.__all__
    assert "PAYLOAD_EMBEDDING_MODEL" in lt_mod.__all__
    # 字段名常量值
    assert lt_mod.PAYLOAD_IMAGE_REF == "image_ref"
    assert lt_mod.PAYLOAD_AUDIO_REF == "audio_ref"
    assert lt_mod.PAYLOAD_IMAGE_EMBEDDING == "image_embedding"
    assert lt_mod.PAYLOAD_EMBEDDING_MODEL == "embedding_model"
