"""BaseASR — 自动语音识别抽象接口。

设计原则:
- 抽象 ASR 实现(FunASR / whisper.cpp / 阿里云 ASR),业务方不感知底层算法。
- 暴露 ``transcribe(audio_segment) -> str`` 接口,输入音频片段,输出文本。
- 流式 ASR:StreamingASR 包装 BaseASR,支持增量转写。
- 单测:直接 mock transcribe() 即可。

Author: SmartButler Team
Date: 2026-10-15
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

import numpy as np

from smartbutler.voice.capture.base import AudioSegment


@dataclass(frozen=True)
class ASRResult:
    """ASR 识别结果。

    Attributes:
        text: 识别文本。
        language: 语种(BCP-47)。
        confidence: 置信度 [0.0, 1.0]。
        words: 分词信息(含时间戳),可选。
    """

    text: str
    language: str = "zh-CN"
    confidence: float = 1.0
    words: list[dict[str, Any]] | None = None


class BaseASR(ABC):
    """ASR 抽象接口。

    不变量:
    - ``transcribe()`` 输入 AudioSegment,输出文本。
    - 返回空字符串 表示无语音/无法识别(不抛异常)。
    - language 参数决定语种识别策略。
    """

    @abstractmethod
    async def transcribe(self, audio: AudioSegment) -> ASRResult:
        """语音转文字。

        Args:
            audio: 音频片段,16kHz mono float32。

        Returns:
            ASRResult: 识别结果(含文本/语种/置信度)。
        """
        ...

    @abstractmethod
    async def close(self) -> None:
        """释放资源。"""
        ...


class ASRError(Exception):
    """ASR 异常。"""
    pass


__all__ = ["BaseASR", "ASRResult", "ASRError"]
