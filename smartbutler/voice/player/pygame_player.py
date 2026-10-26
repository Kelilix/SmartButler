"""pygame 音频播放器实现。

Dependencies:
    pygame (pip install pygame)

懒加载 pygame,避免未安装时无法导入。

注意:
    pygame 2.6.1 的 Windows wheel 最高只到 cp313,Python 3.14 上需源码编译,
    当前环境装不上。因此 Windows 上的默认播放器是 ``WinsoundAudioPlayer``。
    本实现保留供 Linux/macOS 或装有 pygame 的环境使用。

Author: SmartButler Team
Date: 2026-10-15
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from collections.abc import AsyncIterator

from smartbutler.voice.player.audio_decode import decode_to_wav, is_wav
from smartbutler.voice.player.base import AudioPlayerError, BaseAudioPlayer


class PygameAudioPlayer(BaseAudioPlayer):
    """pygame 音频播放器。

    Usage:
        player = PygameAudioPlayer()
        tts = EdgeTTSImpl()

        async def play_tts():
            await player.play(tts.speak("你好"))

        # 或便捷方法
        await player.play_bytes("你好", tts)
    """

    def __init__(self) -> None:
        self._stream = None
        self._initialized = False
        self._sample_rate: int | None = None

    def _ensure_init(self, sample_rate: int | None = None) -> None:
        """懒加载并初始化 pygame mixer。

        必须显式调用 —— ``import pygame`` 不会自动初始化 mixer,
        后续 ``pygame.mixer.Sound()`` 会抛 ``pygame.error: mixer not initialized``。

        Args:
            sample_rate: 音频采样率。pygame 无法对 Sound 重采样,
                mixer 需按资源实际采样率初始化,否则会变调/变速。
                传 None 时使用 pygame 默认值(44100)。
        """
        if self._initialized and (sample_rate is None or sample_rate == self._sample_rate):
            return
        try:
            import pygame
        except ImportError as e:
            raise AudioPlayerError("pygame not installed: pip install pygame") from e

        try:
            if self._initialized:
                # 采样率变了,需要重置 mixer。
                pygame.mixer.quit()

            if sample_rate is not None:
                pygame.mixer.init(frequency=sample_rate)
            else:
                pygame.mixer.init()

            self._sample_rate = sample_rate
            self._initialized = True
        except Exception as e:
            raise AudioPlayerError(f"pygame mixer 初始化失败: {e}") from e

    async def play(self, audio_stream: AsyncIterator[bytes]) -> None:
        """pygame 播放音频流。

        策略:收集所有 chunks → 转成 WAV → 写入临时文件 → pygame.mixer.Sound 播放
        原因:pygame 支持文件播放,不支持流式播放。
        """
        chunks: list[bytes] = []
        async for chunk in audio_stream:
            chunks.append(chunk)

        if not chunks:
            return

        audio_bytes = b"".join(chunks)

        # 非 WAV(Edge TTS 的 MP3)先转码,pygame 虽然也能读 MP3,
        # 但统一转 WAV 可让 mixer 采样率与资源严格一致。
        if not is_wav(audio_bytes):
            audio_bytes = decode_to_wav(audio_bytes)

        await self._play_wav_bytes(audio_bytes)

    async def _play_wav_bytes(self, wav_bytes: bytes) -> None:
        """播放 WAV 格式 bytes。"""
        from smartbutler.voice.player.audio_decode import wav_info

        sample_rate, _channels = wav_info(wav_bytes)
        self._ensure_init(sample_rate)

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            f.write(wav_bytes)
            temp_path = f.name

        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self._play_file_sync, temp_path)
        finally:
            try:
                os.unlink(temp_path)
            except OSError:
                pass

    def _play_file_sync(self, path: str) -> None:
        """同步播放文件(pygame 操作必须在主线程或用 executor)。"""
        import pygame

        try:
            sound = pygame.mixer.Sound(path)
            sound.play()
            # 等待播放完成
            while pygame.mixer.get_busy():
                pygame.time.delay(50)
        except Exception as e:
            raise AudioPlayerError(f"Failed to play {path}: {e}") from e

    async def close(self) -> None:
        """释放 pygame 资源。"""
        try:
            import pygame

            pygame.mixer.quit()
        except Exception:
            pass
        self._initialized = False


# ---------------------------------------------------------------------------
# Fake 实现(单测用)
# ---------------------------------------------------------------------------


class FakeAudioPlayer(BaseAudioPlayer):
    """单测用假音频播放器,记录调用但不实际播放。"""

    def __init__(self) -> None:
        self._played: list[bytes] = []

    async def play(self, audio_stream: AsyncIterator[bytes]) -> None:
        async for chunk in audio_stream:
            self._played.append(chunk)

    async def close(self) -> None:
        pass

    @property
    def played_chunks(self) -> list[bytes]:
        """返回播放过的所有 chunks。"""
        return self._played


__all__ = ["PygameAudioPlayer", "FakeAudioPlayer", "BaseAudioPlayer", "AudioPlayerError"]
