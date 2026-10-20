"""FunASR — FunASR 中文 ASR 实现。

Dependencies:
    funasr (pip install funasr)

Author: SmartButler Team
Date: 2026-10-15
"""

from __future__ import annotations

import numpy as np

from smartbutler.voice.asr.base import ASRError, ASRResult, BaseASR
from smartbutler.voice.capture.base import AudioSegment


class FunASR(BaseASR):
    """FunASR Paraformer-zh 中文 ASR 实现。

    Usage:
        asr = FunASR()
        result = await asr.transcribe(audio_segment)
        print(result.text)

    Attributes:
        model_name: 模型名称,默认 paraformer-zh(SOTA 中文模型)。
        device: 运行设备,auto/cpu/cuda。
    """

    def __init__(
        self,
        model_name: str = "paraformer-zh",
        device: str = "cpu",
    ) -> None:
        self._model_name = model_name
        self._device = device
        self._model = None
        self._punc_model = None
        self._loaded = False
        self._load_error: Exception | None = None

    def _ensure_model(self) -> None:
        """懒加载 FunASR 模型(首次调用时加载)。

        🐛 必须在后台线程调用!FunASR AutoModel 同步加载会卡住 asyncio 事件循环。
        应该用 await self.ensure_ready() 预加载。
        """
        if self._loaded:
            return
        if self._load_error is not None:
            raise self._load_error
        try:
            from funasr import AutoModel

            self._model = AutoModel(
                model=self._model_name,
                device=self._device,
                disable_update=True,
            )
            self._loaded = True
        except ImportError as e:
            err = ASRError("funasr not installed: pip install funasr")
            err.__cause__ = e
            self._load_error = err
            raise self._load_error
        except Exception as e:
            err = ASRError(f"Failed to load FunASR model: {e}")
            err.__cause__ = e
            self._load_error = err
            raise self._load_error

    async def ensure_ready(self) -> None:
        """异步预加载模型(在后台线程跑,避免阻塞事件循环)。"""
        if self._loaded or self._load_error is not None:
            if self._load_error is not None:
                raise self._load_error
            return
        import asyncio
        await asyncio.to_thread(self._ensure_model)

    async def transcribe(self, audio: AudioSegment) -> ASRResult:
        """FunASR 语音转文字。"""
        self._ensure_model()

        try:
            # FunASR 期望 int16 PCM
            audio_int16 = audio.to_int16()

            result = self._model.generate(
                input=audio_int16.tobytes(),
                batch_size_s=300,
                hotword="",
            )

            if not result or len(result) == 0:
                return ASRResult(text="", language="zh-CN", confidence=0.0)

            res = result[0]
            text = res.get("text", "")
            # FunASR 返回的概率是 [0, 1]
            confidence = res.get("sentence_info", [{}])[0].get("confidence", 0.8)

            return ASRResult(
                text=text.strip(),
                language="zh-CN",
                confidence=confidence,
            )
        except Exception as e:
            raise ASRError(f"FunASR transcribe failed: {e}") from e

    async def close(self) -> None:
        """FunASR 无显式 close。"""
        pass

    def __repr__(self) -> str:
        return f"FunASR(model={self._model_name}, device={self._device})"


# ---------------------------------------------------------------------------
# Fake 实现(单测用)
# ---------------------------------------------------------------------------

class FakeASR(BaseASR):
    """单测用假 ASR,直接返回预设文本。"""

    def __init__(self, text: str = "测试文本", confidence: float = 0.95) -> None:
        self._text = text
        self._confidence = confidence

    async def transcribe(self, audio: AudioSegment) -> ASRResult:
        return ASRResult(
            text=self._text,
            language="zh-CN",
            confidence=self._confidence,
        )

    async def close(self) -> None:
        pass


__all__ = ["FunASR", "FakeASR", "ASRError", "ASRResult", "BaseASR"]
