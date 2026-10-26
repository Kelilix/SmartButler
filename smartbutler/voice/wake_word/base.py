"""BaseWakeWordDetector — 唤醒词检测抽象接口。

设计原则:
- 抽象唤醒词检测实现(Picovoice Porcupine / snowboy / 百度唤醒),业务方不感知底层算法。
- 暴露 ``listen()`` 异步生成器接口,持续监听,检测到唤醒词时 yield 回来。
- 同时暴露 ``is_awake(word)`` 方法,判断当前是否为唤醒状态。
- 单测:直接 mock listen() 即可。

Author: SmartButler Team
Date: 2026-10-15
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator
from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class WakeWordResult:
    """唤醒词检测结果。

    Attributes:
        keyword: 唤醒词名称(如 "hey_butler" / "小乐小乐")。
        confidence: 置信度 [0.0, 1.0]。
        audio_start_ms: 唤醒词在音频流中的起始时间(毫秒)。
    """

    keyword: str
    confidence: float
    audio_start_ms: int = 0


class BaseWakeWordDetector(ABC):
    """唤醒词检测抽象接口。

    不变量:
    - ``listen()`` 是异步生成器,持续 yield AudioChunk 直到检测到唤醒词。
    - 检测到唤醒词时 yield WakeWordResult,然后可继续监听。
    - 异常统一抛出 WakeWordError。
    """

    @abstractmethod
    def listen(self) -> AsyncGenerator[WakeWordResult | np.ndarray, None]:
        """持续监听唤醒词。

        注意:这里是**普通 def**,不是 async def。
        子类实现成 ``async def listen(...): yield ...``(异步生成器),
        调用方用 ``async for event in detector.listen()`` 消费。
        基类若写成 ``async def ... -> AsyncIterator[...]``,
        mypy 会把方法体只有 ``...``(无 yield)的基类误判为协程函数,
        返回 ``Coroutine[..., AsyncIterator[...]]``,
        导致所有调用点的 ``async for`` 报 "has no attribute __aiter__"。

        Yields:
            np.ndarray: 音频块(未检测到唤醒词时)。
            WakeWordResult: 唤醒词检测结果(检测到时)。

        Raises:
            WakeWordError: 监听异常。
        """
        ...

    @abstractmethod
    def is_awake(self) -> bool:
        """判断当前是否处于唤醒状态。"""
        ...

    @abstractmethod
    async def wake(self) -> None:
        """手动唤醒(模拟唤醒词检测)。"""
        ...

    @abstractmethod
    async def sleep(self) -> None:
        """进入休眠(取消唤醒状态)。"""
        ...

    @abstractmethod
    async def close(self) -> None:
        """释放资源。"""
        ...


class WakeWordError(Exception):
    """唤醒词检测异常。"""
    pass


__all__ = ["BaseWakeWordDetector", "WakeWordResult", "WakeWordError"]
