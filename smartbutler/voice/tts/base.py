"""BaseTTS — 文本转语音抽象接口。

设计原则:
- 抽象 TTS 实现(edge-tts / piper / 阿里云 TTS),业务方不感知底层算法。
- 暴露 ``speak(text, voice) -> AsyncGenerator[bytes, None]`` 接口,输入文本,yield 音频分片。
- 同时暴露 ``speak_to_file(text, path)`` 便捷方法,直接写 wav。
- 内部实现可插拔:先用 edge-tts 快速跑通,后期换 piper 离线也不影响业务方。
- 单测:直接 mock speak() 即可。

Author: SmartButler Team
Date: 2026-10-15
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator
from dataclasses import dataclass


@dataclass(frozen=True)
class TTSVoice:
    """TTS 音色配置。

    Attributes:
        id: 音色 ID(edge-tts voice name / piper voice name)。
        lang: 语种(BCP-47)。
        gender: 性别(male/female/neutral)。
    """

    id: str
    lang: str = "zh-CN"
    gender: str = "neutral"


# 中文常用音色(默认值)
ZH_DEFAULT_VOICE = TTSVoice(id="zh-CN-XiaoxiaoNeural", lang="zh-CN", gender="female")
ZH_MALE_VOICE = TTSVoice(id="zh-CN-YunxiNeural", lang="zh-CN", gender="male")


class BaseTTS(ABC):
    """TTS 抽象接口。

    不变量:
    - ``speak()`` 是异步生成器,每次 yield 一个音频分片(bytes)。
    - 最终音频格式由子类决定(edge-tts=mp3, piper=wav)。
    - speak_to_file() 是便捷方法,调用 speak() 写文件。
    - 异常统一抛出 TTSError。
    """

    @abstractmethod
    def speak(
        self,
        text: str,
        voice: TTSVoice | None = None,
    ) -> AsyncGenerator[bytes, None]:
        """文本转语音流。

        注意:这里是**普通 def**,不是 async def。
        子类实现成 ``async def speak(...): yield ...``(异步生成器),
        调用方用 ``async for chunk in tts.speak(...)`` 消费。
        基类若写成 ``async def ... -> AsyncIterator[bytes]``,
        mypy 会把方法体只有 ``...``(无 yield)的基类误判为协程函数,
        返回 ``Coroutine[..., AsyncIterator[bytes]]``,
        导致所有调用点的 ``async for`` 报 "has no attribute __aiter__"。

        Args:
            text: 要转换的文本。
            voice: 音色配置;None=默认音色。

        Yields:
            bytes: 音频分片(edge-tts=mp3 帧,piper=wav 分片)。

        Raises:
            TTSError: TTS 异常(网络不可达/模型加载失败等)。
        """
        ...

    async def speak_to_file(
        self,
        text: str,
        path: str,
        voice: TTSVoice | None = None,
    ) -> None:
        """文本转语音并写入文件。

        Args:
            text: 要转换的文本。
            path: 输出文件路径(.wav / .mp3)。
            voice: 音色配置;None=默认音色。

        Raises:
            TTSError: TTS 异常。
        """
        with open(path, "wb") as f:
            async for chunk in self.speak(text, voice):
                f.write(chunk)

    @abstractmethod
    async def list_voices(self, lang: str | None = None) -> list[TTSVoice]:
        """列出可用音色。

        Args:
            lang: 语种过滤;None=列出全部。

        Returns:
            list[TTSVoice]: 可用音色列表。
        """
        ...

    @abstractmethod
    async def close(self) -> None:
        """释放资源。"""
        ...


class TTSError(Exception):
    """TTS 异常。"""
    pass


__all__ = ["BaseTTS", "TTSVoice", "TTSError", "ZH_DEFAULT_VOICE", "ZH_MALE_VOICE"]
