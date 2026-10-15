"""音频播放器抽象接口。

设计原则:
- 抽象音频播放(物理音箱 / pygame / 虚拟音频线),业务方不感知底层。
- 暴露 ``play(audio_stream)`` 异步生成器接口,消费音频分片并播放。
- 同时暴露 ``play_bytes(text, tts)`` 便捷方法,直接 TTS + 播放。
- 单测:直接 mock play() 即可。

Author: SmartButler Team
Date: 2026-10-15
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import AsyncIterator


class BaseAudioPlayer(ABC):
    """音频播放器抽象接口。

    不变量:
    - ``play()`` 输入 AsyncIterator[bytes] 音频流,消费并播放。
    - 音频格式由 TTS 实现决定(edge-tts=mp3, piper=wav)。
    - 异常统一抛出 AudioPlayerError。
    """

    @abstractmethod
    async def play(self, audio_stream: AsyncIterator[bytes]) -> None:
        """播放音频流。

        Args:
            audio_stream: 音频分片 AsyncIterator,每个 chunk 是原始音频 bytes。

        Raises:
            AudioPlayerError: 播放异常(设备不存在/权限拒绝等)。
        """
        ...

    @abstractmethod
    async def close(self) -> None:
        """释放资源。"""
        ...


class AudioPlayerError(Exception):
    """音频播放器异常。"""
    pass


__all__ = ["BaseAudioPlayer", "AudioPlayerError"]
