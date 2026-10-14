"""Embedding 抽象 + 异常（Phase 6.2 P0）。

``Embedder`` 是业务层唯一依赖,后续要换实现(Qwen / Local / Stub)只换工厂,业务零改。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


class EmbedderError(Exception):
    """Embedding 子能力任何错误都包成这个,方便上层统一捕获。"""


@runtime_checkable
class Embedder(Protocol):
    """嵌入器协议（业务层依赖这个,不依赖具体实现）。

    任何 Embedder 必须支持:
    - ``embed(text)``: 单文本 → 向量
    - ``embed_batch(texts)``: 多文本 → 向量列表
    - ``dimension``: 向量维度(常量,启动时校验)
    """

    async def embed(self, text: str) -> list[float]:
        """单文本嵌入。失败抛 ``EmbedderError``。"""
        ...

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """批量嵌入(顺序与输入对齐)。失败抛 ``EmbedderError``。"""
        ...

    @property
    def dimension(self) -> int:
        """向量维度,业务层启动期跟 qdrant_vector_size 校验。"""
        ...


__all__ = ["Embedder", "EmbedderError"]
