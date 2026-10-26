"""PvPorcupineImpl — Picovoice Porcupine 唤醒词检测实现。

Dependencies:
    pvporcupine (pip install pvporcupine)

Note:
    Porcupine 是本地唤醒词检测,无需网络,支持自定义唤醒词训练。
    免费配额:每月 1000 次唤醒(个人项目够用)。
    自定义唤醒词:https://console.picovoice.ai/

懒加载 pvporcupine,避免未安装时无法导入。

Author: SmartButler Team
Date: 2026-10-15
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

import numpy as np

from smartbutler.voice.wake_word.base import (
    BaseWakeWordDetector,
    WakeWordError,
    WakeWordResult,
)


class PvPorcupineImpl(BaseWakeWordDetector):
    """Picovoice Porcupine 唤醒词检测实现。

    Usage:
        detector = PvPorcupineImpl(keywords=["hey_butler", "小乐小乐"])
        async for event in detector.listen():
            if isinstance(event, WakeWordResult):
                print(f"唤醒: {event.keyword}")
                await voice_adapter.handle_wake()
            else:
                # 音频流
                process_audio(event)
    """

    def __init__(
        self,
        keywords: list[str] | None = None,
        access_key: str | None = None,
    ) -> None:
        """
        Args:
            keywords: 唤醒词列表。内置关键词:"ok google"(美国英语),"alexa"(美国英语),
                      "hey siri"(美国英语),"picovoice"(美国英语)。自定义唤醒词需上传到
                      Picovoice Console 训练并下载 .ppn 文件路径。
            access_key: Picovoice AccessKey(免费注册:https://console.picovoice.ai/)。
        """
        self._keywords = keywords or ["picovoice"]
        self._access_key = access_key or ""
        self._porcupine: Any = None
        self._awake = False
        self._loaded = False

    def _ensure_model(self) -> None:
        """懒加载 Porcupine。"""
        if self._loaded:
            return
        import pvporcupine

        try:
            self._porcupine = pvporcupine.create(
                keywords=self._keywords,
                access_key=self._access_key,
            )
            self._loaded = True
        except pvporcupine.PorcupineInvalidArgumentError as e:
            raise WakeWordError(f"Invalid Porcupine argument: {e}") from e
        except pvporcupine.PorcupineActivationError as e:
            raise WakeWordError(f"Porcupine activation failed: {e}") from e
        except pvporcupine.PorcupineRuntimeError as e:
            raise WakeWordError(f"Porcupine runtime error: {e}") from e
        except Exception as e:
            raise WakeWordError(f"Failed to load Porcupine: {e}") from e

    async def listen(self) -> AsyncGenerator[WakeWordResult | np.ndarray, None]:
        """持续监听唤醒词。

        注意:这是个**真正的异步生成器**(方法体含 yield)。
        若写成 ``async def ... -> AsyncIterator[...]`` 但方法体里没有 yield,
        Python 会把它当成普通协程函数 —— 调用方 ``async for event in
        detector.listen()`` 会直接抛
        ``TypeError: 'async for' requires an object with __aiter__ method``。
        """
        self._ensure_model()

        porcupine = self._porcupine

        while True:
            # 从标准输入读音频(实际使用时替换为真实音频流)
            # 这里返回生成器,外部注入音频块
            raise NotImplementedError(
                "PvPorcupineImpl.listen() 是抽象方法,"
                "实际使用中由 VoiceAdapter 注入音频流"
            )
            yield  # pragma: no cover - 让本函数成为真正的异步生成器

    async def process_audio(self, audio: np.ndarray) -> WakeWordResult | None:
        """处理单个音频块,检测唤醒词。

        由 VoiceAdapter 调用,判断是否触发唤醒。

        Args:
            audio: 16kHz mono float32 音频数据。

        Returns:
            WakeWordResult: 检测到唤醒词时返回;None=未检测到。
        """
        self._ensure_model()

        # 转换音频格式
        if audio.dtype != np.int16:
            audio_int16 = (audio * 32767).astype(np.int16)
        else:
            audio_int16 = audio

        keyword_index = self._porcupine.process(audio_int16)

        if keyword_index >= 0:
            result = WakeWordResult(
                keyword=self._keywords[keyword_index],
                confidence=1.0,  # Porcupine 只返回索引,不返回置信度
            )
            self._awake = True
            return result
        return None

    def is_awake(self) -> bool:
        return self._awake

    async def wake(self) -> None:
        self._awake = True

    async def sleep(self) -> None:
        self._awake = False

    async def close(self) -> None:
        if self._porcupine is not None:
            self._porcupine.delete()
            self._porcupine = None
        self._loaded = False

    def __repr__(self) -> str:
        return f"PvPorcupineImpl(keywords={self._keywords})"


# ---------------------------------------------------------------------------
# Fake 实现(单测用)
# ---------------------------------------------------------------------------

class FakeWakeWordDetector(BaseWakeWordDetector):
    """单测用假唤醒词检测,固定触发或不触发。"""

    def __init__(
        self,
        trigger_after: int = 0,
        keyword: str = "hey_butler",
    ) -> None:
        """
        Args:
            trigger_after: 触发前忽略多少个音频块。
            keyword: 返回的唤醒词名称。
        """
        self._trigger_after = trigger_after
        self._keyword = keyword
        self._count = 0
        self._awake = False

    async def listen(self) -> AsyncGenerator[WakeWordResult | np.ndarray, None]:
        raise NotImplementedError("FakeWakeWordDetector.listen() not implemented")
        yield  # pragma: no cover - 让本函数成为真正的异步生成器

    def is_awake(self) -> bool:
        return self._awake

    async def process_audio(self, audio: np.ndarray) -> WakeWordResult | None:
        self._count += 1
        if self._count > self._trigger_after:
            self._awake = True
            return WakeWordResult(keyword=self._keyword, confidence=0.95)
        return None

    async def wake(self) -> None:
        self._awake = True

    async def sleep(self) -> None:
        self._awake = False

    async def close(self) -> None:
        pass


__all__ = [
    "PvPorcupineImpl",
    "FakeWakeWordDetector",
    "BaseWakeWordDetector",
    "WakeWordResult",
    "WakeWordError",
]
