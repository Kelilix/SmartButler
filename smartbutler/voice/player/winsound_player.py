"""WinsoundAudioPlayer — Windows 原生音频播放器。

使用 Windows 自带的 winsound 播放 WAV,无需安装 pygame。

适用场景:
- Windows 平台
- 任意 TTS 输出格式(edge-tts 的 MP3 会被自动转码成 WAV)
- 不想装 pygame 的环境

实现说明:
    winsound 只能直接播放 WAV,所以非 WAV 输入(Edge TTS 的 MP3)会先经
    ``audio_decode.decode_to_wav`` 转码,再交给 winsound 播放。
    早期版本用 Windows Media Player COM 组件播放 MP3,实测其 playState
    永远停在 Transitioning(9),会导致播放流程挂死,故已移除该路径。

Author: SmartButler Team
Date: 2026-10-15
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from collections.abc import AsyncIterator

from smartbutler.voice.player.audio_decode import decode_to_wav
from smartbutler.voice.player.base import AudioPlayerError, BaseAudioPlayer


class WinsoundAudioPlayer(BaseAudioPlayer):
    """Windows 原生音频播放器。

    把任意格式的 TTS 音频转成 WAV 后用 winsound 播放。
    """

    def __init__(self) -> None:
        self._is_windows = sys.platform == "win32"

    async def play(self, audio_stream: AsyncIterator[bytes]) -> None:
        """Windows 播放音频流。

        流程:
        1. 收集所有 chunks
        2. 转码成 WAV(MP3 等压缩格式)
        3. 调 winsound 播放
        """
        if not self._is_windows:
            raise AudioPlayerError("WinsoundAudioPlayer 只支持 Windows")

        chunks: list[bytes] = []
        async for chunk in audio_stream:
            chunks.append(chunk)

        if not chunks:
            return

        wav_bytes = decode_to_wav(b"".join(chunks))

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._play_wav_sync, wav_bytes)

    def _play_wav_sync(self, wav_bytes: bytes) -> None:
        """同步播放 WAV(winsound 必须在主线程)。"""
        import winsound

        try:
            # 优先用 SND_MEMORY 直接播内存字节,免去临时文件。
            # 部分 Windows 版本对 SND_MEMORY 支持不佳,失败则回退到临时文件。
            try:
                winsound.PlaySound(wav_bytes, winsound.SND_MEMORY)
                return
            except RuntimeError:
                pass

            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                f.write(wav_bytes)
                temp_path = f.name

            try:
                # SND_FILENAME:从文件播放
                # SND_NODEFAULT:不播放默认声音
                winsound.PlaySound(
                    temp_path,
                    winsound.SND_FILENAME | winsound.SND_NODEFAULT,
                )
            finally:
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass
        except RuntimeError as e:
            raise AudioPlayerError(f"winsound.PlaySound failed: {e}") from e

    async def close(self) -> None:
        """无需释放资源。"""
        pass


__all__ = ["WinsoundAudioPlayer"]
