"""SimpleWakeWordDetector — 简单的能量阈值唤醒词检测。

不依赖外部 API,适合开发测试。
使用音频能量阈值检测唤醒词(比如大声喊出特定词)。

Dependencies:
    无外部依赖

Author: SmartButler Team
Date: 2026-10-15
"""

from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator

import numpy as np

from smartbutler.voice.wake_word.base import (
    BaseWakeWordDetector,
    WakeWordError,
    WakeWordResult,
)


class SimpleWakeWordDetector(BaseWakeWordDetector):
    """简单的能量阈值唤醒词检测器。

    不依赖外部 API,使用音频能量阈值检测唤醒。
    适用于开发测试,或作为"物理按钮唤醒"的软件模拟。

    Usage:
        # 模拟按钮唤醒
        detector = SimpleWakeWordDetector(keyword="button", energy_threshold=0.0)
        # 任何音频都会触发

        # 能量阈值唤醒(大声说话)
        detector = SimpleWakeWordDetector(keyword="hey_butler", energy_threshold=0.3)

        # 手动唤醒
        await detector.wake()
    """

    def __init__(
        self,
        keyword: str = "hey_butler",
        energy_threshold: float = 0.2,
        min_duration_ms: int = 500,
        cooldown_ms: int = 1000,
    ) -> None:
        """
        Args:
            keyword: 唤醒词名称(返回在 WakeWordResult 中)。
            energy_threshold: 能量阈值 [0.0, 1.0],超过此阈值视为"有声音"。
                0.0 = 任何音频都触发(适合按钮模拟)。
            min_duration_ms: 最小触发时长(毫秒),防止误触发。
            cooldown_ms: 冷却时间(毫秒),触发后多久内不再触发。
        """
        self._keyword = keyword
        self._energy_threshold = energy_threshold
        self._min_duration_ms = min_duration_ms
        self._cooldown_ms = cooldown_ms

        self._awake = False
        self._last_trigger_time: float = 0
        self._trigger_count = 0

    def _compute_energy(self, audio: np.ndarray) -> float:
        """计算音频能量 [0.0, 1.0]。

        使用 RMS 计算。
        """
        if len(audio) == 0:
            return 0.0
        # RMS
        rms = np.sqrt(np.mean(audio.astype(np.float32) ** 2))
        # 归一化到 [0, 1]
        return min(1.0, rms * 2)

    async def listen(self) -> AsyncIterator[WakeWordResult | np.ndarray]:
        """持续监听唤醒词(需要外部注入音频流)。"""
        raise NotImplementedError(
            "SimpleWakeWordDetector.listen() 需要外部注入音频流,"
            "请用 VoiceAdapter 或手动调用 process_audio()"
        )

    async def process_audio(self, audio: np.ndarray) -> WakeWordResult | None:
        """处理单个音频块,检测唤醒词。

        Args:
            audio: 16kHz mono float32 音频数据。

        Returns:
            WakeWordResult: 检测到唤醒词时返回;None=未检测到。
        """
        import time

        # 检查冷却时间
        current_time = time.time() * 1000
        if current_time - self._last_trigger_time < self._cooldown_ms:
            return None

        # 计算能量
        energy = self._compute_energy(audio)

        if energy > self._energy_threshold:
            self._trigger_count += 1

            # 检查是否达到最小触发时长
            # 简化:连续 N 帧超过阈值即触发
            # 这里假设每帧约 100ms,需要 min_duration_ms / 100 帧
            required_frames = max(1, self._min_duration_ms // 100)

            if self._trigger_count >= required_frames:
                self._last_trigger_time = current_time
                self._trigger_count = 0
                self._awake = True

                return WakeWordResult(
                    keyword=self._keyword,
                    confidence=min(1.0, energy * 2),
                )
        else:
            self._trigger_count = max(0, self._trigger_count - 1)

        return None

    def is_awake(self) -> bool:
        """判断当前是否处于唤醒状态。"""
        return self._awake

    async def wake(self) -> None:
        """手动唤醒(模拟唤醒词检测)。"""
        self._awake = True

    async def sleep(self) -> None:
        """进入休眠(取消唤醒状态)。"""
        self._awake = False
        self._trigger_count = 0

    async def close(self) -> None:
        """释放资源。"""
        self._awake = False
        self._trigger_count = 0

    def __repr__(self) -> str:
        return f"SimpleWakeWordDetector(keyword={self._keyword}, threshold={self._energy_threshold})"


__all__ = ["SimpleWakeWordDetector"]
