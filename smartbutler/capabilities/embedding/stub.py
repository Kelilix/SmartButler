"""Stub Embedder(Qwen 不可用时的降级实现,Phase 6.2 P0)。

用 SHA-256 把文本 hash 后,截断/扩展成 ``dimension`` 维固定向量。
**不调任何 API,纯本地**;召回准确度低,但不崩。

适用场景:
- 单元测试 / CI
- Qwen API 不可用(限流、key 失效)时的降级
- 用户暂时不配 ``SMARTBUTLER_EMBEDDING_API_KEY``
"""

from __future__ import annotations

import hashlib

from smartbutler.capabilities.embedding.base import Embedder
from smartbutler.utils.logging import get_logger

logger = get_logger(__name__)


class StubEmbedder:
    """基于文本 hash 的本地 stub embedder(不调 API,纯 CPU)。"""

    def __init__(self, dimension: int = 1024) -> None:
        if dimension <= 0:
            raise ValueError(f"dimension 必须 > 0,得到 {dimension}")
        self._dimension = dimension

    @property
    def dimension(self) -> int:
        return self._dimension

    async def embed(self, text: str) -> list[float]:
        if not text or not text.strip():
            return [0.0] * self._dimension
        return self._hash_to_vector(text)

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [await self.embed(t) for t in texts]

    def _hash_to_vector(self, text: str) -> list[float]:
        """SHA-256 → 字节 → 归一化到 [-1, 1] → 截断/重复到 dimension 维。

        同一文本每次结果一致(deterministic),但语义相似 ≠ hash 相似,召回仅作降级。
        """
        digest = hashlib.sha256(text.encode("utf-8")).digest()  # 32 bytes
        # 扩展到足够长(每字节 1 个 float),用循环
        raw: list[float] = []
        i = 0
        while len(raw) < self._dimension:
            raw.append((digest[i % len(digest)] - 128) / 128.0)
            i += 1
        return raw[: self._dimension]


__all__ = ["StubEmbedder"]
