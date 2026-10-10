"""Embedder 抽象层 + Stub 降级 单测(Phase 6.2 P0)。"""

from __future__ import annotations

import pytest

from smartbutler.capabilities.embedding import StubEmbedder
from smartbutler.capabilities.embedding.base import Embedder, EmbedderError


@pytest.mark.asyncio
async def test_stub_embedder_dimension() -> None:
    """dimension 属性等于构造参数。"""
    e = StubEmbedder(dimension=512)
    assert e.dimension == 512


@pytest.mark.asyncio
async def test_stub_embedder_deterministic() -> None:
    """同一文本每次得到相同向量。"""
    e = StubEmbedder(dimension=64)
    v1 = await e.embed("hello")
    v2 = await e.embed("hello")
    assert v1 == v2


@pytest.mark.asyncio
async def test_stub_embedder_different_text_different_vec() -> None:
    """不同文本得到不同向量(大概率)。"""
    e = StubEmbedder(dimension=64)
    v1 = await e.embed("hello")
    v2 = await e.embed("world")
    assert v1 != v2


@pytest.mark.asyncio
async def test_stub_embedder_empty_text() -> None:
    """空文本返回全 0 向量。"""
    e = StubEmbedder(dimension=8)
    v = await e.embed("")
    assert v == [0.0] * 8
    v2 = await e.embed("   ")
    assert v2 == [0.0] * 8


@pytest.mark.asyncio
async def test_stub_embedder_length_matches_dimension() -> None:
    """embed 返回的向量长度 == dimension。"""
    for dim in (4, 64, 1024):
        e = StubEmbedder(dimension=dim)
        v = await e.embed("test")
        assert len(v) == dim


@pytest.mark.asyncio
async def test_stub_embedder_batch_alignment() -> None:
    """embed_batch 返回顺序与输入对齐。"""
    e = StubEmbedder(dimension=16)
    out = await e.embed_batch(["hello", "", "world"])
    assert len(out) == 3
    assert len(out[0]) == 16
    # 中间空文本 → 全 0
    assert out[1] == [0.0] * 16
    # 头尾非空 → 非全 0
    assert any(x != 0.0 for x in out[0])
    assert any(x != 0.0 for x in out[2])


@pytest.mark.asyncio
async def test_stub_embedder_batch_empty() -> None:
    """空列表返回空列表。"""
    e = StubEmbedder(dimension=8)
    assert await e.embed_batch([]) == []


@pytest.mark.asyncio
async def test_stub_embedder_invalid_dimension() -> None:
    """dimension <= 0 抛错。"""
    with pytest.raises(ValueError):
        StubEmbedder(dimension=0)
    with pytest.raises(ValueError):
        StubEmbedder(dimension=-1)


@pytest.mark.asyncio
async def test_stub_implements_protocol() -> None:
    """StubEmbedder 实现 Embedder Protocol。"""
    e: Embedder = StubEmbedder(dimension=8)
    # 满足 Protocol 的核心接口
    assert hasattr(e, "embed")
    assert hasattr(e, "embed_batch")
    assert hasattr(e, "dimension")
