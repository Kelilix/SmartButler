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


def _resample(audio: np.ndarray, orig_sr: int, target_sr: int) -> np.ndarray:
    """简单线性重采样(Resemblyzer 要求 16kHz)。

    Resemblyzer 必须 16kHz mono;若传入非 16kHz，用线性插值近似重采样。
    生产环境应该用 torchaudio / scipy.signal.resample,但增加依赖。
    这里保持零依赖策略，用 numpy 实现简单线性重采样。
    """
    if orig_sr == target_sr:
        return audio
    duration = len(audio) / orig_sr
    target_len = int(duration * target_sr)
    if target_len <= 1:
        return audio
    # 用 np.interp 做线性插值
    x_old = np.linspace(0, duration, len(audio))
    x_new = np.linspace(0, duration, target_len)
    return np.interp(x_new, x_old, audio).astype(np.float32)


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
            from resemblyzer import VoiceEncoder

            self._encoder = VoiceEncoder()
            self._loaded = True
        except ImportError as e:
            raise SpeakerEmbedderError("resemblyzer not installed: pip install resemblyzer") from e
        except Exception as e:
            raise SpeakerEmbedderError(f"Failed to load Resemblyzer model: {e}") from e

    async def embed(self, audio: np.ndarray, sample_rate: int = 16000) -> list[float]:
        """Resemblyzer 声纹 embedding。

        新版 Resemblyzer API:
        - ``embed_utterance(wav)`` — 单句话，返回 L2 归一化的 256 维向量
        - ``embed_speaker(wavs)`` — 多句话聚合，返回 L2 归一化的 256 维向量

        实时识别用 embed_utterance，声纹注册(多句)用 embed_speaker。
        Resemblyzer 已经 L2 归一化，不需要再 normalize。
        """
        self._ensure_model()

        try:
            # Resemblyzer 期望原始 PCM 16kHz mono float32
            if audio.dtype != np.float32:
                audio = audio.astype(np.float32)

            # Resemblyzer 期望 16kHz 采样率;若不匹配则重采样
            if sample_rate != 16000:
                audio = _resample(audio, sample_rate, 16000)

            # 根据 inference_mode 选 API:
            # - embed_speaker: 接受 List[np.ndarray]，适合多句聚合
            # - embed_utterance: 接受单 np.ndarray，适合实时识别
            # 两个 API 都返回已 L2 归一化的 256 维向量
            if self._inference_mode == "embed_speaker":
                raw_embedding = self._encoder.embed_speaker([audio])
            else:
                raw_embedding = self._encoder.embed_utterance(audio)

            # 转 list[float]
            emb_list = raw_embedding.tolist() if hasattr(raw_embedding, "tolist") else list(raw_embedding)

            # Resemblyzer 已经 L2 归一化，但仍按约定 pad 到 512 维
            return normalize_embedding(emb_list, target_dim=512)
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
