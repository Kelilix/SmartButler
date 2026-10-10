"""Embedding 子能力（Phase 6.2 P0）。

对外暴露:
- ``Embedder``: Protocol,业务层只 import 这个
- ``QwenEmbedder``: ``qwen3.7-text-embedding-flash`` 实现
- ``StubEmbedder``: Qwen 不可用时的降级实现(hash → 固定 1024 维)
- ``build_default_embedder``: 工厂,按 env 选实现 + 失败自动降级

设计要点:
1. **业务层零感知**: ``emotion/memory/`` 只 import ``Embedder`` Protocol
2. **Qwen 走 OpenAI 兼容协议**: ``/compatible-mode/v1/embeddings`` 端点
3. **降级链**: Qwen 失败 → StubEmbedder(hash 化,无 API 调用)
4. **可替换**: 后续要加 LocalEmbedder(sentence-transformers)只新增 .py,不动业务层
"""

from __future__ import annotations

from smartbutler.capabilities.embedding.base import Embedder
from smartbutler.capabilities.embedding.config import (
    EmbeddingSettings,
    load_embedding_settings,
)
from smartbutler.capabilities.embedding.factory import build_default_embedder
from smartbutler.capabilities.embedding.qwen import QwenEmbedder
from smartbutler.capabilities.embedding.stub import StubEmbedder

__all__ = [
    "Embedder",
    "EmbeddingSettings",
    "QwenEmbedder",
    "StubEmbedder",
    "build_default_embedder",
    "load_embedding_settings",
]
