"""PiperTTSImpl — Piper TTS 实现(离线备选)。

Dependencies:
    piper-tts (pip install piper-tts)
    piper-phonemize (pip install piper-phonemize)

Note:
    Piper 是本地 TTS,无需网络,但模型文件较大(约 50-200MB per voice)。
    Phase 7.4+ edge-tts 不稳定时启用,作为离线备选。

Author: SmartButler Team
Date: 2026-10-15
"""

from __future__ import annotations

import struct
from typing import AsyncIterator

from smartbutler.voice.tts.base import BaseTTS, TTSError, TTSVoice, ZH_DEFAULT_VOICE


class PiperTTSImpl(BaseTTS):
    """Piper TTS 本地实现。

    Usage:
        tts = PiperTTSImpl(model_path="zh_CN-huayan-medium.onnx")
        async for chunk in tts.speak("你好"):
            write_to_audio(chunk)

    Attributes:
        model_path: Piper ONNX 模型路径。
        sample_rate: 采样率(Hz),由模型决定。
    """

    def __init__(self, model_path: str) -> None:
        self._model_path = model_path
        self._sample_rate = 22050  # 默认,实际由模型决定
        self._model = None

    def _ensure_model(self) -> None:
        """懒加载 Piper 模型。"""
        if self._model is not None:
            return
        try:
            import piper
        except ImportError as e:
            raise TTSError("piper-tts not installed: pip install piper-tts") from e

        try:
            self._model = piper.load_model(self._model_path)
            self._sample_rate = self._model.sample_rate
        except Exception as e:
            raise TTSError(f"Failed to load Piper model: {e}") from e

    async def speak(
        self,
        text: str,
        voice: TTSVoice | None = None,
    ) -> AsyncIterator[bytes]:
        """Piper TTS 文本转语音流。"""
        self._ensure_model()

        try:
            # Piper 输出 wav 头 + 分片数据
            wav_header = self._make_wav_header(self._sample_rate, 1, 16)
            yield wav_header  # 先发送 WAV header

            # 分片 yield(每 sentence yield 一次)
            import piper
            sampler, audio_float32 = piper.synthesize(text, self._model)

            # 分批发送(4096 样本一批)
            batch_size = 4096
            audio_int16 = (audio_float32 * 32767).astype("<h")
            for i in range(0, len(audio_int16), batch_size):
                chunk = audio_int16[i : i + batch_size]
                yield chunk.tobytes()
        except Exception as e:
            raise TTSError(f"Piper TTS failed: {e}") from e

    def _make_wav_header(
        self,
        sample_rate: int,
        channels: int = 1,
        bits_per_sample: int = 16,
    ) -> bytes:
        """生成 WAV 文件头。"""
        # 简单 WAV header(不含 data size,允许后续追加)
        data = b"RIFF"
        data += struct.pack("<I", 0)  # placeholder file size
        data += b"WAVE"
        data += b"fmt "
        data += struct.pack("<I", 16)  # fmt chunk size
        data += struct.pack("<H", 1)  # PCM format
        data += struct.pack("<H", channels)
        data += struct.pack("<I", sample_rate)
        data += struct.pack("<I", sample_rate * channels * bits_per_sample // 8)  # byte rate
        data += struct.pack("<H", channels * bits_per_sample // 8)  # block align
        data += struct.pack("<H", bits_per_sample)
        data += b"data"
        data += struct.pack("<I", 0)  # placeholder data size
        return data

    async def list_voices(self, lang: str | None = None) -> list[TTSVoice]:
        """Piper 音色列表(Piper 用模型文件,不是音色名)。"""
        return [TTSVoice(id=self._model_path, lang="zh-CN", gender="neutral")]

    async def close(self) -> None:
        """Piper 无显式 close。"""
        pass

    def __repr__(self) -> str:
        return f"PiperTTSImpl(model={self._model_path}, sr={self._sample_rate})"


__all__ = ["PiperTTSImpl", "BaseTTS", "TTSVoice", "TTSError"]
