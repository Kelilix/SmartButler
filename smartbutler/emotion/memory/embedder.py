"""业务层 Embedder 薄包装(Phase 6.2 P0)。

存在的目的:
1. 给 ``emotion/memory/`` 一个统一的"业务层 import 入口"
2. 后续要在业务层加 Embedder 相关的特定行为(如"返回前打印维度一致性日志")
   时,只动这个文件,不动 ``capabilities/embedding/``

P0 阶段: 直接 re-export capabilities 工厂。
"""

from __future__ import annotations

from smartbutler.capabilities.embedding import (
    Embedder,
    EmbeddingSettings,
    QwenEmbedder,
    StubEmbedder,
    build_default_embedder,
    load_embedding_settings,
)

__all__ = [
    "Embedder",
    "EmbeddingSettings",
    "QwenEmbedder",
    "StubEmbedder",
    "build_default_embedder",
    "load_embedding_settings",
]
