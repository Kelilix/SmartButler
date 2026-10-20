"""BaseVAD — 语音活动检测抽象接口。

设计原则:
- 抽象 VAD 实现(Silero / WebRTC / snowboy),业务方不感知底层算法。
- 暴露 ``is_speech(audio) -> bool`` 接口,判断当前帧是否有人说话。
- 单测:直接 mock is_speech() 即可。

Author: SmartButler Team
Date: 2026-10-15
"""

from __future__ import annotations

from abc import ABC, abstractmethod

import numpy as np


class BaseVAD(ABC):
    """语音活动检测抽象接口。

    不变量:
    - ``is_speech()`` 输入 audio 必须是 16kHz mono float32 ndarray。
    - 返回 True=有语音,False=无语音(含静音/噪音)。
    - VAD 状态由子类自己管理(如 Silero 的内部 state)。
    """

    @abstractmethod
    def is_speech(self, audio: np.ndarray) -> bool:
        """判断是否有人说话。

        Args:
            audio: 音频数据,shape=(samples,),dtype=float32,16kHz mono。

        Returns:
            bool: True=有语音,False=静音/噪音。
        """
        ...

    @abstractmethod
    def reset(self) -> None:
        """重置 VAD 状态(用于一段话结束后)。"""
        ...

    async def ensure_ready(self) -> None:
        """异步预加载模型/资源(在后台线程跑,避免阻塞事件循环)。

        默认实现为空操作,子类(如 SileroVAD)需要下载/加载模型时应重写。
        """
        return None


class VADError(Exception):
    """VAD 异常。"""
    pass


__all__ = ["BaseVAD", "VADError"]
