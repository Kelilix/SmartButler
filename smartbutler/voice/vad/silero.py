"""SileroVAD — Silero 语音活动检测实现。

Dependencies:
    torch (pip install torch)
    silero-vad (pip install silero-vad)

懒加载 torch,避免未安装时无法导入。

Author: SmartButler Team
Date: 2026-10-15
"""

from __future__ import annotations

from typing import Any

import numpy as np

from smartbutler.voice.vad.base import BaseVAD, VADError


class SileroVAD(BaseVAD):
    """Silero VAD 实现。

    Usage:
        vad = SileroVAD()
        is_speech = vad.is_speech(audio_chunk)

    Attributes:
        threshold: 语音阈值 [0.0, 1.0],默认 0.5。高于此值判定为语音。
        min_speech_duration_ms: 最小语音时长(毫秒),默认 250ms。
        min_silence_duration_ms: 最小静音时长(毫秒),默认 200ms。
    """

    def __init__(
        self,
        threshold: float = 0.5,
        min_speech_duration_ms: int = 250,
        min_silence_duration_ms: int = 200,
    ) -> None:
        self._threshold = threshold
        self._min_speech = min_speech_duration_ms
        self._min_silence = min_silence_duration_ms
        self._model: Any = None
        self._get_speech_timestamps: Any = None
        self._loaded = False

    def _ensure_model(self) -> None:
        """懒加载 Silero VAD 模型(首次调用时加载)。"""
        if self._loaded:
            return
        try:
            import torch

            torch.set_num_threads(1)
            model, utils = torch.hub.load(
                repo_or_dir="snakers4/silero-vad",
                model="silero_vad",
                trust_repo=True,
            )
            self._model = model
            self._get_speech_timestamps = utils[0]
            self._loaded = True
        except ImportError as e:
            raise VADError("torch not installed: pip install torch") from e
        except Exception as e:
            raise VADError(f"Failed to load Silero VAD model: {e}") from e

    def is_speech(self, audio: np.ndarray) -> bool:
        """Silero VAD 判断是否有人说话。"""
        self._ensure_model()

        # 确保 float32 16kHz
        if audio.dtype != np.float32:
            audio = audio.astype(np.float32)

        import torch

        with torch.no_grad():
            # Silero 期望 torch.Tensor (1, samples)
            tensor = torch.from_numpy(audio).float()
            if tensor.dim() == 1:
                tensor = tensor.unsqueeze(0)

            # get_speech_probs 返回每帧概率
            prob = self._model(tensor, 16000).item()
            return prob > self._threshold

    def reset(self) -> None:
        """Silero 无状态,reset 为空操作。"""
        pass

    def __repr__(self) -> str:
        return f"SileroVAD(threshold={self._threshold})"


# ---------------------------------------------------------------------------
# Fake 实现(单测用)
# ---------------------------------------------------------------------------

class FakeVAD(BaseVAD):
    """单测用假 VAD,固定返回预设值。

    Usage:
        vad = FakeVAD(is_speech_values=[True, True, False, True])
        for expected in [True, True, False, True]:
            assert vad.is_speech(some_audio) == expected
    """

    def __init__(self, is_speech_values: list[bool] | None = None) -> None:
        """
        Args:
            is_speech_values: 每次调用 is_speech 返回的值列表(循环使用)。
                               None=永远返回 True。
        """
        self._values = is_speech_values or [True]
        self._idx = 0

    def is_speech(self, audio: np.ndarray) -> bool:
        result = self._values[self._idx % len(self._values)]
        self._idx += 1
        return result

    def reset(self) -> None:
        self._idx = 0


__all__ = ["SileroVAD", "FakeVAD", "VADError", "BaseVAD"]
