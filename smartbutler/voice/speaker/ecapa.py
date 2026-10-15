"""EcapaTdnnEmbedder — ECAPA-TDNN 声纹识别实现(备选)。

Dependencies:
    speechbrain (pip install speechbrain)

Note:
    ECAPA-TDNN 是当前 SOTA 声纹模型,精度优于 resemblyzer,
    但依赖 speechbrain(较重,约 500MB),作为 Phase 7.6+ 备选。

Author: SmartButler Team
Date: 2026-10-15
"""

from __future__ import annotations

import numpy as np

from smartbutler.voice.speaker.base import (
    BaseSpeakerEmbedder,
    SpeakerEmbedderError,
    normalize_embedding,
)


class EcapaTdnnEmbedder(BaseSpeakerEmbedder):
    """ECAPA-TDNN 声纹 embedding 实现。

    模型:ECAPA-TDNN(spkrec-ecapa-voxceleb)
    输出:192 维向量 → normalize_embedding → 512 维

    Usage:
        embedder = EcapaTdnnEmbedder()
        embedding = await embedder.embed(audio_segment.audio)
    """

    NATIVE_DIM = 192

    def __init__(self, device: str = "cpu") -> None:
        """
        Args:
            device: 运行设备,cpu/cuda。
        """
        self._device = device
        self._model: object | None = None
        self._loaded = False

    def _ensure_model(self) -> None:
        """懒加载 ECAPA-TDNN 模型。"""
        if self._loaded:
            return
        try:
            from speechbrain.inference import SpeakerRecognition

            self._model = SpeakerRecognition.from_hparams(
                source="speechbrain/spkrec-ecapa-voxceleb",
                savedir="pretrained_models/spkrec-ecapa-voxceleb",
                device=self._device,
            )
            self._loaded = True
        except ImportError as e:
            raise SpeakerEmbedderError("speechbrain not installed: pip install speechbrain") from e
        except Exception as e:
            raise SpeakerEmbedderError(f"Failed to load ECAPA-TDNN model: {e}") from e

    async def embed(self, audio: np.ndarray, sample_rate: int = 16000) -> list[float]:
        """ECAPA-TDNN 声纹 embedding。"""
        self._ensure_model()

        try:
            # SpeechBrain 期望 numpy array 或 tensor
            embedding = self._model.encode_batch(
                np.asarray(audio, dtype=np.float32).reshape(1, -1)
            )
            # 输出 shape=(1, 192), squeeze
            raw_embedding = embedding.squeeze().cpu().numpy()
            return normalize_embedding(raw_embedding, target_dim=512)
        except Exception as e:
            raise SpeakerEmbedderError(f"ECAPA-TDNN embed failed: {e}") from e

    async def close(self) -> None:
        """SpeechBrain 无显式 close。"""
        pass

    def __repr__(self) -> str:
        return f"EcapaTdnnEmbedder(device={self._device})"


__all__ = [
    "EcapaTdnnEmbedder",
    "BaseSpeakerEmbedder",
    "SpeakerEmbedderError",
    "normalize_embedding",
]
