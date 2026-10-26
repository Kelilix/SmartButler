"""BaseAudioCapture — 麦克风采集抽象接口。

设计原则:
- 抽象音频源(物理麦克风 / 虚拟音频线 / 模拟 buffer),业务方不感知底层。
- 暴露 ``stream()`` 异步生成器接口,每步 yield ``AudioChunk``。
- 采样率/声道/位深全部归一化到 16kHz mono float32 PCM(ASR/VAD 通用格式)。
- 单测:直接注入 List[AudioChunk] 即可,无需 sounddevice。

References:
- whisper.cpp 要求 16kHz mono int16 PCM
- Silero VAD 要求 16kHz mono float32
- FunASR 要求 16kHz mono int16
=> 统一输出 float32,使用方自行转

Author: SmartButler Team
Date: 2026-10-15
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator
from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class AudioChunk:
    """音频块。

    Attributes:
        audio: 音频数据,shape=(samples,),dtype=float32,范围 [-1.0, 1.0]。
        sample_rate: 采样率(Hz),固定 16000。
        channels: 声道数,固定 1(mono)。
        is_final: 是否为流末尾(麦克风关闭时 True)。
    """

    audio: np.ndarray  # shape=(samples,), dtype=np.float32, range [-1, 1]
    sample_rate: int = 16000
    channels: int = 1
    is_final: bool = False

    def __post_init__(self) -> None:
        assert self.audio.ndim == 1, "audio must be 1D"
        assert self.audio.dtype == np.float32, f"audio dtype must be float32, got {self.audio.dtype}"
        assert 8000 <= self.sample_rate <= 48000, f"sample_rate must be 8-48kHz, got {self.sample_rate}"
        assert self.channels == 1, "only mono supported"


@dataclass(frozen=True)
class AudioSegment:
    """一段完整音频(含多帧),用于 ASR / 声纹识别。

    Attributes:
        audio: 音频数据,shape=(samples,),dtype=float32,范围 [-1.0, 1.0]。
        sample_rate: 采样率(Hz),固定 16000。
        duration_ms: 时长(毫秒)。
    """

    audio: np.ndarray  # shape=(samples,), dtype=np.float32
    sample_rate: int = 16000

    @property
    def duration_ms(self) -> int:
        return int(len(self.audio) / self.sample_rate * 1000)

    def to_int16(self) -> np.ndarray:
        """转 int16 PCM(FunASR / whisper 等用)。"""
        return (self.audio * 32767).astype(np.int16)

    def to_bytes(self) -> bytes:
        """转原始 PCM bytes。"""
        return self.audio.astype(np.float32).tobytes()


class BaseAudioCapture(ABC):
    """麦克风采集抽象接口。

    不变量:
    - ``stream()`` yield 的 AudioChunk 全部归一化到 16kHz mono float32。
    - stream() 是异步生成器,调用方负责 ``async for``。
    - 异常时统一抛出 AudioCaptureError。
    """

    @abstractmethod
    def stream(self) -> AsyncGenerator[AudioChunk, None]:
        """异步音频流。

        注意:这里是**普通 def**,不是 async def。
        子类实现成 ``async def stream(...): yield ...``(异步生成器),
        调用方用 ``async for chunk in capture.stream()`` 消费。
        基类若写成 ``async def ... -> AsyncIterator[AudioChunk]``,
        mypy 会把方法体只有 ``...``(无 yield)的基类误判为协程函数,
        返回 ``Coroutine[..., AsyncIterator[AudioChunk]]``,
        导致所有调用点的 ``async for`` 报 "has no attribute __aiter__"。

        Yields:
            AudioChunk: 音频块,最后一块 is_final=True。

        Raises:
            AudioCaptureError: 采集异常(设备不存在/权限拒绝等)。
        """
        ...

    @abstractmethod
    async def start(self) -> None:
        """启动采集(可与 stream() 合并,拆开是为了支持显式生命周期控制)。"""
        ...

    @abstractmethod
    async def stop(self) -> None:
        """停止采集,释放硬件资源。"""
        ...


class AudioCaptureError(Exception):
    """采集异常。"""
    pass


__all__ = [
    "BaseAudioCapture",
    "AudioChunk",
    "AudioSegment",
    "AudioCaptureError",
]
