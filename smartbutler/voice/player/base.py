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

import time
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator


class BaseAudioPlayer(ABC):
    """音频播放器抽象接口。

    不变量:
    - ``play()`` 输入 AsyncIterator[bytes] 音频流,消费并播放。
    - 音频格式由 TTS 实现决定(edge-tts=mp3, piper=wav)。
    - 异常统一抛出 AudioPlayerError。

    🆕 2026-11-01 (Phase 7.6.1): 回声抑制门控。

    ## 为什么门控在播放器层

    管家播报有**两条**路径:
        1. ``VoiceAdapter.speak()``      → player.play()
        2. ``TTSOutputAdapter.push()``   → player.play()   ← Agent 的正常回复走这条

    两条路径共用**同一个 player 实例**,所以门控放在 player 里
    才能覆盖全部播报场景。

    ## 为什么需要门控

    ``mic_capture`` 是裸采集,没有 AEC。管家播报时,麦克风会把
    **管家自己的声音**录进去 → 污染声纹 embedding → 识别率下降。

    门控机制:播放**开始前**打开,播放**结束后再延长一点**(扬声器尾音
    + 房间反射),采集侧在这段时间内丢弃所有音频。
    """

    def __init__(self, playback_tail_seconds: float = 0.3) -> None:
        """构造播放器。

        Args:
            playback_tail_seconds: 播放结束后额外门控的秒数
                (扬声器尾音 + 房间反射消散)。默认 0.3s。
        """
        self._playback_tail_seconds = playback_tail_seconds
        self._playback_gate_until: float = 0.0

    @property
    def playback_tail_seconds(self) -> float:
        """播放结束后的门控延长秒数。"""
        return self._playback_tail_seconds

    @property
    def playback_gate_until(self) -> float:
        """门控截止时间戳(``time.monotonic()`` 基准)。

        当前时间 **小于** 这个值时,说明正在播报(或刚播完),
        采集侧应该丢弃音频。详见 :meth:`play_gated`。
        """
        return self._playback_gate_until

    def is_playback_gated(self) -> bool:
        """当前是否处于门控期(正在播报 / 刚播完)。"""
        return time.monotonic() < self._playback_gate_until

    async def play_gated(self, audio_stream: AsyncIterator[bytes]) -> None:
        """带回声抑制门控的播放(推荐所有播报路径都用这个)。

        与裸 ``play()`` 的区别:
        - 播放**开始前**就打开门控(保证播放期间的音频全被丢弃)
        - 播放**结束后**门控再延长 ``playback_tail_seconds``

        Args:
            audio_stream: 音频分片流。
        """
        # 播放前开门控
        self._playback_gate_until = time.monotonic() + self._playback_tail_seconds
        try:
            await self.play(audio_stream)
        finally:
            # 播放后延长(扬声器尾音 + 房间反射)
            self._playback_gate_until = time.monotonic() + self._playback_tail_seconds

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
