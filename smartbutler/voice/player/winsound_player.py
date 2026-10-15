"""WinsoundAudioPlayer — Windows 原生音频播放器。

使用 Windows 自带的 winsound 播放 WAV 文件,无需安装 pygame。

适用场景:
- Windows 平台
- WAV 格式音频(EdgeTTS 输出 mp3 时需要先转码)
- 不想装 pygame 的环境

Author: SmartButler Team
Date: 2026-10-15
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
from typing import AsyncIterator

from smartbutler.voice.player.base import AudioPlayerError, BaseAudioPlayer


class WinsoundAudioPlayer(BaseAudioPlayer):
    """Windows 原生音频播放器。

    优先用 winsound(标准库,零依赖)播放 WAV。
    遇到 mp3 时,自动用 Windows 自带的 wmplayer.exe / mciSendString 兜底。
    """

    def __init__(self) -> None:
        self._is_windows = sys.platform == "win32"

    async def play(self, audio_stream: AsyncIterator[bytes]) -> None:
        """Windows 播放音频流。

        流程:
        1. 收集所有 chunks
        2. 写到临时文件
        3. 调系统命令播放
        """
        # 收集所有 chunks
        chunks: list[bytes] = []
        async for chunk in audio_stream:
            chunks.append(chunk)

        if not chunks:
            return

        audio_bytes = b"".join(chunks)

        # 检测格式
        is_wav = audio_bytes[:4] == b"RIFF" and audio_bytes[8:12] == b"WAVE"

        if is_wav:
            await self._play_wav(audio_bytes)
        else:
            # mp3 / 其他 → 用 Windows Media Player 命令行播放
            await self._play_generic(audio_bytes)

    async def _play_wav(self, wav_bytes: bytes) -> None:
        """播放 WAV 字节流(纯标准库,零依赖)。"""
        if not self._is_windows:
            raise AudioPlayerError("WinsoundAudioPlayer 只支持 Windows")

        # 写临时文件
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            f.write(wav_bytes)
            temp_path = f.name

        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self._play_wav_sync, temp_path)
        finally:
            try:
                os.unlink(temp_path)
            except OSError:
                pass

    def _play_wav_sync(self, path: str) -> None:
        """同步播放 WAV(winsound 必须在主线程)。"""
        import winsound

        try:
            # SND_FILENAME:从文件播放
            # SND_NODEFAULT:不播放默认声音
            # SND_ASYNC:不阻塞当前线程(后台播放)
            winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_NODEFAULT)
        except RuntimeError as e:
            raise AudioPlayerError(f"winsound.PlaySound failed: {e}") from e

    async def _play_generic(self, audio_bytes: bytes) -> None:
        """播放 mp3 等非 WAV 格式(用 Windows Media Player)。"""
        if not self._is_windows:
            raise AudioPlayerError("非 Windows 平台,需用 pygame / 厂商 SDK")

        # 写临时 mp3 文件
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
            f.write(audio_bytes)
            temp_path = f.name

        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self._play_mp3_sync, temp_path)
        finally:
            # 延迟删除(等播放器读完)
            try:
                os.unlink(temp_path)
            except OSError:
                pass

    def _play_mp3_sync(self, path: str) -> None:
        """用 Windows Media Player 播放 mp3(同步阻塞)。"""
        try:
            # 用 PowerShell 调 WMP(Windows Media Player)
            # 用户必须已安装 wmplayer.exe
            ps_cmd = (
                f"$player = New-Object -ComObject WMPlayer.OCX.7; "
                f"$player.URL = '{path}'; "
                f"$player.controls.play(); "
                f"while ($player.playState -ne 1) {{ Start-Sleep -Milliseconds 200 }}; "
                f"$player.close()"
            )
            subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps_cmd],
                check=True,
                capture_output=True,
                timeout=300,  # 5 分钟超时
            )
        except FileNotFoundError:
            raise AudioPlayerError(
                "PowerShell 不存在,无法播放 mp3。\n"
                "建议:1) 转 Edge TTS 输出 WAV  2) 装 pygame"
            )
        except subprocess.CalledProcessError as e:
            raise AudioPlayerError(f"WMP 播放失败: {e.stderr.decode('utf-8', errors='ignore')}") from e
        except subprocess.TimeoutExpired:
            raise AudioPlayerError("WMP 播放超时(5 分钟)") from None

    async def close(self) -> None:
        """无需释放资源。"""
        pass


__all__ = ["WinsoundAudioPlayer"]
