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

    🆕 7.4 多模态扩展位(7.9 启动时由 EmbeddingGemma2Embedder 实现):
    - ``embed_image(path)``: 图片 → 向量
    - ``embed_audio(path)``: 音频 → 向量
    - ``embed_video(path)``: 视频 → 向量

    7.4-7.8 业务层不调这 3 个方法,默认实现抛 NotImplementedError
    (Protocol 鸭子类型允许,子类不必重写)。Qwen / Stub 继续抛
    → 降级闭合(7.9 启动前调这 3 个方法 = 立刻报错,显式而非静默失败)。
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

    # 🆕 7.4 多模态方法(默认 NotImplementedError,7.9 启动时由 EmbeddingGemma2Embedder 实现)
    async def embed_image(self, image_path: str) -> list[float]:
        """图片 embedding(7.4-7.8 抛 NotImplementedError)。

        7.9 启动时由 EmbeddingGemma2Embedder 重写,从
        ``SentenceTransformer("google/embeddinggemma-2")`` 算向量。
        Qwen / Stub embedder 继续抛(降级)。

        Args:
            image_path: 图片文件绝对路径(jpg / png / webp)。

        Returns:
            768 维统一向量(text / image / audio 共空间)。

        Raises:
            NotImplementedError: 当前 Embedder 不支持图片 embedding(7.9 启动前)。
        """
        raise NotImplementedError(
            "image embedding 7.9 启动时实现,见 PHASE_7_TECHNICAL_DESIGN.md §12.6 H1"
        )

    async def embed_audio(self, audio_path: str) -> list[float]:
        """音频 embedding(7.4-7.8 抛 NotImplementedError)。

        7.9 启动时由 EmbeddingGemma2Embedder 重写。

        Args:
            audio_path: 音频文件绝对路径(wav / mp3 / m4a)。

        Returns:
            768 维统一向量。

        Raises:
            NotImplementedError: 当前 Embedder 不支持音频 embedding(7.9 启动前)。
        """
        raise NotImplementedError(
            "audio embedding 7.9 启动时实现,见 PHASE_7_TECHNICAL_DESIGN.md §12.6 H1"
        )

    async def embed_video(self, video_path: str) -> list[float]:
        """视频 embedding(7.4-7.8 抛 NotImplementedError)。

        7.9 启动时由 EmbeddingGemma2Embedder 重写(直接抽帧 + embed_image 平均)。

        Args:
            video_path: 视频文件绝对路径(mp4 / mov / avi)。

        Returns:
            768 维统一向量。

        Raises:
            NotImplementedError: 当前 Embedder 不支持视频 embedding(7.9 启动前)。
        """
        raise NotImplementedError(
            "video embedding 7.9 启动时实现,见 PHASE_7_TECHNICAL_DESIGN.md §12.6 H1"
        )


__all__ = ["Embedder", "EmbedderError"]
