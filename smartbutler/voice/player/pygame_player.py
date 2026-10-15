"""pygame 音频播放器实现。

Dependencies:
    pygame (pip install pygame)

懒加载 pygame,避免未安装时无法导入。

Author: SmartButler Team
Date: 2026-10-15
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from typing import AsyncIterator

import numpy as np

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

    def _ensure_init(self) -> None:
        """懒加载 pygame。"""
        if self._initialized:
            return
        try:
            import pygame
            pygame.mixer.init()
            self._initialized = True
        except ImportError as e:
            raise AudioPlayerError("pygame not installed: pip install pygame") from e

    async def play(self, audio_stream: AsyncIterator[bytes]) -> None:
        """pygame 播放音频流。

        策略:收集所有 chunks → 写入临时文件 → pygame.mixer.Sound 播放
        原因:pygame 支持文件播放,不支持流式播放。
        """
        # 收集所有 chunks
        chunks: list[bytes] = []
        async for chunk in audio_stream:
            chunks.append(chunk)

        if not chunks:
            return

        audio_bytes = b"".join(chunks)

        # 检测音频格式并播放
        if self._is_wav(audio_bytes):
            await self._play_wav_bytes(audio_bytes)
        else:
            # mp3 或其他格式 → 写临时文件
            await self._play_temp_file(audio_bytes, is_mp3=True)

    def _is_wav(self, data: bytes) -> bool:
        """检查是否是 WAV 格式。"""
        return data[:4] == b"RIFF" and data[8:12] == b"WAVE"

    async def _play_wav_bytes(self, wav_bytes: bytes) -> None:
        """播放 WAV 格式 bytes。"""
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

    async def _play_temp_file(self, data: bytes, is_mp3: bool = True) -> None:
        """播放临时文件(MP3 等格式)。"""
        suffix = ".mp3" if is_mp3 else ".audio"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
            f.write(data)
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
