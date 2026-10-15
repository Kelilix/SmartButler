"""MicCapture — sounddevice 麦克风采集实现。

实现 BaseAudioCapture 接口,统一输出 16kHz mono float32 PCM。

Dependencies:
    sounddevice (pip install sounddevice)

单测:用 FakeMicCapture 注 mock stream 即可。

Author: SmartButler Team
Date: 2026-10-15
"""

from __future__ import annotations

import asyncio
from typing import AsyncIterator

import numpy as np

from smartbutler.voice.capture.base import (
    AudioCaptureError,
    AudioChunk,
    BaseAudioCapture,
)


# ---------------------------------------------------------------------------
# 辅助:音频格式转换
# ---------------------------------------------------------------------------

def _to_float32_normalized(audio: np.ndarray) -> np.ndarray:
    """统一转 16kHz mono float32 [-1, 1]。

    处理情况:
    - int16 PCM → float32
    - int32/float64 → float32
    - 多声道 → mono(平均)
    - 任何采样率 → 16kHz(简单线性重采样,临时用;生产环境建议用 sox/ffmpeg)
    """
    target_sr = 16000

    # 类型归一化
    if audio.dtype == np.int16:
        audio = audio.astype(np.float32) / 32768.0
    elif audio.dtype == np.int32:
        audio = audio.astype(np.float32) / 2147483648.0
    elif audio.dtype == np.float64:
        audio = audio.astype(np.float32)
    elif audio.dtype != np.float32:
        audio = audio.astype(np.float32)

    # 声道归一化 → mono
    if audio.ndim == 2:
        # shape=(samples, channels)
        audio = audio.mean(axis=1)
    elif audio.ndim == 1:
        pass
    else:
        raise AudioCaptureError(f"Unexpected audio ndim: {audio.ndim}")

    # 重采样 → 16kHz(若需要)
    current_sr = getattr(audio, "_sr", target_sr)
    if current_sr != target_sr:
        audio = _resample_simple(audio, current_sr, target_sr)

    return audio.astype(np.float32)


def _resample_simple(audio: np.ndarray, from_sr: int, to_sr: int) -> np.ndarray:
    """简单线性重采样(质量一般,延迟不敏感场景够用)。"""
    if from_sr == to_sr:
        return audio
    ratio = len(audio) * to_sr / from_sr
    new_len = int(ratio)
    indices = np.linspace(0, len(audio) - 1, new_len)
    return np.interp(indices, np.arange(len(audio)), audio).astype(np.float32)


# ---------------------------------------------------------------------------
# MicCapture 实现
# ---------------------------------------------------------------------------

class MicCapture(BaseAudioCapture):
    """sounddevice 麦克风采集。

    Usage:
        async with MicCapture(device_index=0, chunk_ms=100) as capture:
            async for chunk in capture.stream():
                process(chunk.audio)

    Attributes:
        device_index: sounddevice 设备索引;None=默认设备。
        chunk_ms: 每块音频时长(毫秒),默认 100ms。
        ready: 是否已启动。
    """

    def __init__(
        self,
        device_index: int | None = None,
        chunk_ms: int = 100,
        target_sr: int = 16000,
    ) -> None:
        self._device_index = device_index
        self._chunk_ms = chunk_ms
        self._target_sr = target_sr
        self._ready = False
        self._stop_event = asyncio.Event()
        self._queue: asyncio.Queue[AudioChunk | None] = asyncio.Queue()
        self._stream: "sd.InputStream | None" = None

    async def start(self) -> None:
        if self._ready:
            return
        if self._stop_event.is_set():
            self._stop_event.clear()
        self._ready = True

        # 在新线程运行 sounddevice 同步流(避免阻塞事件循环)
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._start_stream)

    def _start_stream(self) -> None:
        """同步启动 sounddevice 流。"""
        import sounddevice as sd

        try:
            self._stream = sd.InputStream(
                device=self._device_index,
                samplerate=self._target_sr,
                channels=1,
                dtype=np.float32,
                blocksize=int(self._target_sr * self._chunk_ms / 1000),
                callback=self._on_audio,
            )
            self._stream.start()
        except sd.PortAudioError as e:
            raise AudioCaptureError(f"Failed to open microphone: {e}") from e

    def _on_audio(
        self,
        indata: np.ndarray,
        frames: int,
        time_info: dict,
        status: "sd.CallbackFlags",
    ) -> None:
        """sounddevice 回调(在音频线程同步调用)。"""
        import logging

        if status:
            # 溢出/超时警告,记录但继续
            logging.getLogger(__name__).warning(f"snddevice status: {status}")

        chunk = AudioChunk(
            audio=indata[:, 0].astype(np.float32),  # 确保 float32
            sample_rate=self._target_sr,
            channels=1,
            is_final=False,
        )
        try:
            self._queue.put_nowait(chunk)
        except asyncio.QueueFull:
            pass  # 丢帧,避免阻塞音频线程

    async def stream(self) -> AsyncIterator[AudioChunk]:
        """异步音频流。"""
        if not self._ready:
            await self.start()

        try:
            while True:
                chunk = await self._queue.get()
                if chunk is None:  # 结束信号
                    break
                yield chunk
        finally:
            pass

    async def stop(self) -> None:
        """停止采集。"""
        if not self._ready:
            return
        self._ready = False
        self._stop_event.set()

        # 发送结束信号
        try:
            self._queue.put_nowait(None)
        except asyncio.QueueFull:
            pass

        # 停止 sounddevice 流
        if self._stream is not None:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self._stream.stop)
            self._stream = None

    def __repr__(self) -> str:
        return f"MicCapture(device={self._device_index}, sr={self._target_sr})"


# ---------------------------------------------------------------------------
# Fake 实现(单测用)
# ---------------------------------------------------------------------------

class FakeMicCapture(BaseAudioCapture):
    """单测用假麦克风,直接注入 AudioChunk list。

    Usage:
        fake = FakeMicCapture([
            AudioChunk(audio=np.random.rand(1600).astype(np.float32)),
            AudioChunk(audio=np.random.rand(1600).astype(np.float32), is_final=True),
        ])
        async for chunk in fake.stream():
            ...

    Attributes:
        chunks: 要 yield 的音频块列表。
    """

    def __init__(self, chunks: list[AudioChunk]) -> None:
        self._chunks = chunks
        self._ready = False

    async def start(self) -> None:
        self._ready = True

    async def stop(self) -> None:
        self._ready = False

    async def stream(self) -> AsyncIterator[AudioChunk]:
        if not self._ready:
            await self.start()
        for chunk in self._chunks:
            yield chunk


__all__ = [
    "MicCapture",
    "FakeMicCapture",
    "AudioCaptureError",
    "AudioChunk",
    "BaseAudioCapture",
]
