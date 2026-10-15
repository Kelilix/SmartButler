"""ResemblyzerEmbedder — Resemblyzer 声纹识别实现。

Dependencies:
    resemblyzer (pip install resemblyzer)

Author: SmartButler Team
Date: 2026-10-15
"""

from __future__ import annotations

from typing import Any

import numpy as np

from smartbutler.voice.speaker.base import (
    BaseSpeakerEmbedder,
    SpeakerEmbedderError,
    normalize_embedding,
)


class ResemblyzerEmbedder(BaseSpeakerEmbedder):
    """Resemblyzer 声纹 embedding 实现。

    模型:SpeechEmbedder(inference_mode="embed_speaker")
    输出:256 维向量 → normalize_embedding → 512 维

    Usage:
        embedder = ResemblyzerEmbedder()
        embedding = await embedder.embed(audio_segment.audio)
    """

    NATIVE_DIM = 256

    def __init__(self, inference_mode: str = "embed_speaker") -> None:
        """
        Args:
            inference_mode: embed_speaker(全音频聚合) / embed_utterance(单句话)。
                            声纹注册用 embed_speaker,实时识别用 embed_utterance。
        """
        self._inference_mode = inference_mode
        self._encoder: Any = None
        self._loaded = False

    def _ensure_model(self) -> None:
        """懒加载 Resemblyzer 模型。"""
        if self._loaded:
            return
        try:
            from resemblyzer import SpeechEncoder

            self._encoder = SpeechEncoder()
            self._loaded = True
        except ImportError as e:
            raise SpeakerEmbedderError("resemblyzer not installed: pip install resemblyzer") from e
        except Exception as e:
            raise SpeakerEmbedderError(f"Failed to load Resemblyzer model: {e}") from e

    async def embed(self, audio: np.ndarray, sample_rate: int = 16000) -> list[float]:
        """Resemblyzer 声纹 embedding。"""
        self._ensure_model()

        try:
            # Resemblyzer 期望原始 PCM 16kHz mono float32
            if audio.dtype != np.float32:
                audio = audio.astype(np.float32)

            # embed_raw 返回 256 维 numpy array
            raw_embedding = self._encoder.embed_raw(audio, sample_rate)

            # 归一化到 512 维
            return normalize_embedding(raw_embedding, target_dim=512)
        except Exception as e:
            raise SpeakerEmbedderError(f"Resemblyzer embed failed: {e}") from e

    async def close(self) -> None:
        """Resemblyzer 无显式 close。"""
        pass

    def __repr__(self) -> str:
        return f"ResemblyzerEmbedder(mode={self._inference_mode})"


# ---------------------------------------------------------------------------
# Fake 实现(单测用)
# ---------------------------------------------------------------------------

class FakeSpeakerEmbedder(BaseSpeakerEmbedder):
    """单测用假声纹识别,固定返回随机向量。"""

    NATIVE_DIM = 256

    def __init__(self, fixed_embedding: list[float] | None = None) -> None:
        """
        Args:
            fixed_embedding: 固定返回值;None=每次返回不同的随机向量。
        """
        self._fixed = fixed_embedding

    async def embed(self, audio: np.ndarray, sample_rate: int = 16000) -> list[float]:
        if self._fixed is not None:
            return self._fixed
        return (np.random.rand(512) * 0.5 + 0.25).tolist()

    async def close(self) -> None:
        pass


__all__ = [
    "ResemblyzerEmbedder",
    "FakeSpeakerEmbedder",
    "BaseSpeakerEmbedder",
    "SpeakerEmbedderError",
    "normalize_embedding",
]
