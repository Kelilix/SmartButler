"""BaseSpeakerEmbedder — 声纹识别抽象接口。

设计原则:
- 抽象声纹 embedding 模型(resemblyzer / ecapa-tdnn),业务方不感知底层算法。
- 暴露 ``embed(audio) -> list[float]`` 接口,输入音频片段,输出 512 维向量。
- 维度归一化:无论模型原始输出多少维(resemblyzer=256, ecapa-tdnn=192),
  统一 padding/truncate 到 512 维(SpeakerInfo.embedding 协议固定 512 维)。
- 单测:直接 mock embed() 即可。

Author: SmartButler Team
Date: 2026-10-15
"""

from __future__ import annotations

from abc import ABC, abstractmethod

import numpy as np

from smartbutler.events.core.event import SPEAKER_EMBEDDING_DIM


class BaseSpeakerEmbedder(ABC):
    """声纹识别抽象接口。

    不变量:
    - ``embed()`` 输入 AudioSegment,输出固定 512 维向量(list[float])。
    - 异常统一抛出 SpeakerEmbedderError。
    """

    # 子类应重写此属性,声明实际模型输出维度
    NATIVE_DIM: int = SPEAKER_EMBEDDING_DIM

    @abstractmethod
    async def embed(self, audio: np.ndarray, sample_rate: int = 16000) -> list[float]:
        """从音频提取声纹 embedding。

        Args:
            audio: 音频数据,shape=(samples,),dtype=float32,16kHz mono。
            sample_rate: 采样率(Hz),默认 16000。

        Returns:
            list[float]: 512 维声纹向量。
        """
        ...

    @abstractmethod
    async def close(self) -> None:
        """释放资源。"""
        ...


def normalize_embedding(embedding: np.ndarray, target_dim: int = SPEAKER_EMBEDDING_DIM) -> list[float]:
    """归一化 embedding 到固定维度。

    - 若 embedding 维度 < target_dim → padding zeros
    - 若 embedding 维度 > target_dim → truncate
    - 若 = → 直接返回

    Args:
        embedding: 原始 embedding 向量。
        target_dim: 目标维度,默认 512。

    Returns:
        list[float]: 固定维度向量。
    """
    if len(embedding) == target_dim:
        return embedding.tolist()

    if len(embedding) < target_dim:
        # padding zeros
        normalized = np.pad(embedding, (0, target_dim - len(embedding)), mode="constant")
    else:
        # truncate
        normalized = embedding[:target_dim]

    return normalized.tolist()


class SpeakerEmbedderError(Exception):
    """声纹识别异常。"""
    pass


__all__ = [
    "BaseSpeakerEmbedder",
    "SpeakerEmbedderError",
    "normalize_embedding",
    "SPEAKER_EMBEDDING_DIM",
]
