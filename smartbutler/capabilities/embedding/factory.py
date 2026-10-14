"""Embedder 工厂(Phase 6.2 P0)。

按 env 选实现:
- ``provider=qwen`` + 配 api_key → ``QwenEmbedder``
- 否则 → ``StubEmbedder``(降级)

调用方拿到的总是 ``Embedder`` Protocol 实例,无感知。
"""

from __future__ import annotations

from smartbutler.capabilities.embedding.base import Embedder, EmbedderError
from smartbutler.capabilities.embedding.config import (
    EmbeddingSettings,
    load_embedding_settings,
)
from smartbutler.capabilities.embedding.qwen import QwenEmbedder
from smartbutler.capabilities.embedding.stub import StubEmbedder
from smartbutler.utils.logging import get_logger

logger = get_logger(__name__)


def build_default_embedder(
    settings: EmbeddingSettings | None = None,
) -> Embedder:
    """按 env 选实现,失败自动降级到 StubEmbedder。

    Args:
        settings: 传入时优先用;不传则从 .env 读。

    Returns:
        ``Embedder`` Protocol 实例(QwenEmbedder 或 StubEmbedder)。
    """
    s = settings or load_embedding_settings()
    if s.provider == "stub":
        logger.info("embedder.factory.stub", dimension=s.dimension)
        return StubEmbedder(dimension=s.dimension)
    # provider=qwen (默认)
    try:
        embedder: Embedder = QwenEmbedder(s)
    except EmbedderError as e:
        logger.warning(
            "embedder.factory.qwen_init_failed,降级到 StubEmbedder",
            error=str(e),
        )
        return StubEmbedder(dimension=s.dimension)
    logger.info(
        "embedder.factory.qwen",
        model=s.model,
        dimension=s.dimension,
    )
    return embedder


__all__ = ["build_default_embedder"]
