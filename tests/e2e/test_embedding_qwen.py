"""tests/e2e/test_embedding_qwen.py — Qwen Embedding API 真实连通测试。

默认 skip；启用方式：
    pytest -m e2e tests/e2e/test_embedding_qwen.py -v

本文件验证 Phase 6.2 P0 关键链路：
1. .env 中的 SMARTBUTLER_EMBEDDING_API_KEY / BASE_URL / MODEL 可用
2. QwenEmbedder 单文本 / 批量嵌入端到端连通
3. 向量维度 = SMARTBUTLER_EMBEDDING_DIMENSION(默认 1024)
4. 语义相似度: 相似文本相似度 > 不相似文本(粗校验)
5. 与 StubEmbedder 行为对比(确认真实 API 不是退化路径)

依赖 .env 中的 Embedding 凭证；没有凭证会自动 skip(与现有 e2e 模式一致)。
"""
from __future__ import annotations

import pytest

from smartbutler.capabilities.embedding import (
    Embedder,
    EmbeddingSettings,
    QwenEmbedder,
    StubEmbedder,
    load_embedding_settings,
)

pytestmark = pytest.mark.e2e


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def embedding_settings() -> EmbeddingSettings:
    """从 .env 读 Embedding 配置。"""
    return load_embedding_settings()


@pytest.fixture
def embedder(embedding_settings: EmbeddingSettings) -> Embedder:
    """构造真实 QwenEmbedder(不降级)。"""
    e = QwenEmbedder(embedding_settings)
    yield e
    e.close()


# ---------------------------------------------------------------------------
# 跳过条件: .env 里没配 api_key
# ---------------------------------------------------------------------------


def _require_api_key(settings: EmbeddingSettings) -> None:
    """无 api_key 跳过(避免裸跑 e2e 时报一堆无意义错)。"""
    if not settings.api_key:
        pytest.skip("SMARTBUTLER_EMBEDDING_API_KEY 未配,跳过 e2e 测试")


# ---------------------------------------------------------------------------
# 1. 配置加载
# ---------------------------------------------------------------------------


def test_settings_load_with_real_config(embedding_settings: EmbeddingSettings) -> None:
    """.env 中真实配置能被加载(只要 api_key 存在就断言完整)。"""
    _require_api_key(embedding_settings)
    assert embedding_settings.api_key
    assert embedding_settings.base_url.startswith("http")
    assert embedding_settings.model  # 走 .env 的 model(qwen3.7-text-embedding-flash)
    assert embedding_settings.dimension > 0
    assert embedding_settings.timeout > 0


# ---------------------------------------------------------------------------
# 2. 单文本嵌入
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_embed_single_text(embedder: Embedder) -> None:
    """单文本 embed 返回维度正确的非零向量。"""
    v = await embedder.embed("你好,世界")
    assert isinstance(v, list)
    assert len(v) == embedder.dimension
    # 至少有一些非零分量(避免意外拿到全 0)
    assert any(x != 0.0 for x in v)


@pytest.mark.asyncio
async def test_embed_returns_float_list(embedder: Embedder) -> None:
    """向量元素是 float(非 int / str)。"""
    v = await embedder.embed("hello")
    assert all(isinstance(x, float) for x in v)


# ---------------------------------------------------------------------------
# 3. 批量嵌入
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_embed_batch(embedder: Embedder) -> None:
    """批量 embed 顺序与输入对齐。"""
    texts = ["第一句", "second sentence", "第三句内容"]
    vs = await embedder.embed_batch(texts)
    assert len(vs) == 3
    for v in vs:
        assert len(v) == embedder.dimension
        assert any(x != 0.0 for x in v)


@pytest.mark.asyncio
async def test_embed_batch_with_empty(embedder: Embedder) -> None:
    """批量有空文本时空位补 0 向量,顺序保持。"""
    texts = ["A", "", "B"]
    vs = await embedder.embed_batch(texts)
    assert len(vs) == 3
    assert any(x != 0.0 for x in vs[0])  # A 非空 → 非全 0
    assert vs[1] == [0.0] * embedder.dimension  # 空 → 全 0
    assert any(x != 0.0 for x in vs[2])  # B 非空 → 非全 0


# ---------------------------------------------------------------------------
# 4. 语义相似度(粗校验)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_similar_texts_have_higher_similarity(embedder: Embedder) -> None:
    """相似文本的余弦相似度 > 不相似文本。

    真实 embedding API 应当满足;如果 qwen 接口出 bug 退化到 hash 之类的
    实现,这条会失败,触发告警。
    """
    import math

    def cosine(a: list[float], b: list[float]) -> float:
        dot = sum(x * y for x, y in zip(a, b))
        na = math.sqrt(sum(x * x for x in a))
        nb = math.sqrt(sum(x * x for x in b))
        if na == 0 or nb == 0:
            return 0.0
        return dot / (na * nb)

    v_coffee1 = await embedder.embed("我喜欢喝美式咖啡")
    v_coffee2 = await embedder.embed("我爱喝咖啡")
    v_car = await embedder.embed("今天开车去上班")

    sim_close = cosine(v_coffee1, v_coffee2)
    sim_far = cosine(v_coffee1, v_car)

    # 弱校验:相似度差 > 0.05(API 出错时这条必然 fail)
    assert sim_close > sim_far, (
        f"相似度断言失败:sim_close={sim_close:.4f}, sim_far={sim_far:.4f}。"
        "可能 Qwen embedding API 行为异常或网络问题。"
    )
    assert sim_close - sim_far > 0.05, (
        f"语义区分度过低:close={sim_close:.4f}, far={sim_far:.4f}"
    )


# ---------------------------------------------------------------------------
# 5. 与 StubEmbedder 对比(确认走的是真实 API,不是降级)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_real_embedder_differs_from_stub(embedder: Embedder) -> None:
    """QwenEmbedder 与 StubEmbedder 同输入应得到**完全不同**的向量。

    防止 .env 配置错了导致 QwenEmbedder 走降级路径但没报错。
    """
    stub = StubEmbedder(dimension=embedder.dimension)
    text = "对比测试:确认走了真实 API"
    v_real = await embedder.embed(text)
    v_stub = await stub.embed(text)
    # 真实模型向量几乎不可能跟 SHA-256 字节流一致
    assert v_real != v_stub, (
        "QwenEmbedder 返回与 StubEmbedder 完全一致的向量,"
        "怀疑 QwenEmbedder 实际走了降级路径。"
    )


# ---------------------------------------------------------------------------
# 6. 维度一致性(防止 embedding dimension 跟 qdrant_vector_size 不匹配)
# ---------------------------------------------------------------------------


def test_dimension_matches_settings(embedding_settings: EmbeddingSettings) -> None:
    """dimension 属性 == .env SMARTBUTLER_EMBEDDING_DIMENSION。"""
    _require_api_key(embedding_settings)
    e = QwenEmbedder(embedding_settings)
    try:
        assert e.dimension == embedding_settings.dimension
        # 默认应该 = 1024(跟 qdrant_vector_size 配套)
        # 用户改了 .env 的话就以 .env 为准
    finally:
        e.close()
