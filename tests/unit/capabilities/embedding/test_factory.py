"""Embedder 工厂 + 配置 单测(Phase 6.2 P0)。"""

from __future__ import annotations

import pytest

from smartbutler.capabilities.embedding.config import (
    EmbeddingSettings,
    load_embedding_settings,
)
from smartbutler.capabilities.embedding.factory import build_default_embedder
from smartbutler.capabilities.embedding.qwen import QwenEmbedder
from smartbutler.capabilities.embedding.stub import StubEmbedder


def test_factory_returns_stub_when_no_api_key() -> None:
    """无 api_key → 工厂降级到 StubEmbedder。"""
    s = EmbeddingSettings(api_key=None, base_url="https://x", dimension=64)
    embedder = build_default_embedder(s)
    assert isinstance(embedder, StubEmbedder)
    assert embedder.dimension == 64


def test_factory_returns_qwen_when_api_key_present() -> None:
    """有 api_key → 工厂返回 QwenEmbedder。"""
    s = EmbeddingSettings(api_key="test", base_url="https://x", dimension=64)
    embedder = build_default_embedder(s)
    assert isinstance(embedder, QwenEmbedder)
    assert embedder.dimension == 64


def test_factory_provider_stub_explicit() -> None:
    """provider=stub 显式选 StubEmbedder(不调 Qwen)。"""
    s = EmbeddingSettings(provider="stub", api_key="ignored", base_url="https://x", dimension=32)
    embedder = build_default_embedder(s)
    assert isinstance(embedder, StubEmbedder)
    assert embedder.dimension == 32


def test_load_embedding_settings_default_dimension() -> None:
    """默认 dimension = 1024。"""
    s = load_embedding_settings()
    assert s.dimension == 1024
    assert s.model == "qwen3.7-text-embedding-flash"
    assert s.provider == "qwen"


def test_load_embedding_settings_strict_mismatch_raises() -> None:
    """strict=True + qdrant_vector_size 不一致 → 抛 ValueError。"""
    with pytest.raises(ValueError) as exc:
        load_embedding_settings(strict=True, qdrant_vector_size=1536)
    assert "维度" in str(exc.value) or "不匹配" in str(exc.value)


def test_load_embedding_settings_strict_match_ok() -> None:
    """strict=True + qdrant_vector_size 一致 → 正常返回。"""
    s = load_embedding_settings(strict=True, qdrant_vector_size=1024)
    assert s.dimension == 1024
