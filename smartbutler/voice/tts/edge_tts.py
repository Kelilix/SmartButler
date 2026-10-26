"""EdgeTTSImpl — Microsoft Edge TTS 实现。

Dependencies:
    edge-tts (pip install edge-tts)

懒加载 edge_tts,避免未安装时无法导入。

Author: SmartButler Team
Date: 2026-10-15
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

from smartbutler.voice.tts.base import (
    ZH_DEFAULT_VOICE,
    BaseTTS,
    TTSError,
    TTSVoice,
)


class EdgeTTSImpl(BaseTTS):
    """Microsoft Edge TTS 实现。

    Usage:
        tts = EdgeTTSImpl()
        async for chunk in tts.speak("你好", voice=ZH_DEFAULT_VOICE):
            write_to_audio(chunk)

    Attributes:
        default_voice: 默认音色。
    """

    def __init__(self, default_voice: TTSVoice | None = None) -> None:
        self._default_voice = default_voice or ZH_DEFAULT_VOICE

    async def speak(
        self,
        text: str,
        voice: TTSVoice | None = None,
    ) -> AsyncGenerator[bytes, None]:
        """Edge TTS 文本转语音流。

        Yields:
            bytes: mp3 音频分片。
        """
        import edge_tts

        voice = voice or self._default_voice

        try:
            communicate = edge_tts.Communicate(text, voice.id)
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    yield chunk["data"]
        except edge_tts.NoAudioReceived:
            raise TTSError("No audio received from Edge TTS") from None
        except Exception as e:
            raise TTSError(f"Edge TTS failed: {e}") from e

    async def list_voices(self, lang: str | None = None) -> list[TTSVoice]:
        """列出 Edge TTS 可用音色。"""
        import edge_tts

        try:
            voices = await edge_tts.list_voices(language=lang)
            return [
                TTSVoice(
                    id=v["Name"],
                    lang=v["Locale"],
                    gender=v["Gender"],
                )
                for v in voices
            ]
        except Exception as e:
            raise TTSError(f"Failed to list voices: {e}") from e

    async def close(self) -> None:
        """Edge TTS 无显式连接,close 为空操作。"""
        pass

    def __repr__(self) -> str:
        return f"EdgeTTSImpl(default_voice={self._default_voice.id})"


# ---------------------------------------------------------------------------
# Fake 实现(单测用)
# ---------------------------------------------------------------------------

class FakeTTS(BaseTTS):
    """单测用假 TTS,每次 speak() 返回固定 bytes。

    Usage:
        fake = FakeTTS(b"FAKE_AUDIO_CHUNK")
        async for chunk in fake.speak("hello"):
            assert chunk == b"FAKE_AUDIO_CHUNK"
    """

    def __init__(self, chunk: bytes = b"FAKE_AUDIO") -> None:
        self._chunk = chunk

    async def speak(
        self,
        text: str,
        voice: TTSVoice | None = None,
    ) -> AsyncGenerator[bytes, None]:
        yield self._chunk

    async def list_voices(self, lang: str | None = None) -> list[TTSVoice]:
        return [ZH_DEFAULT_VOICE]

    async def close(self) -> None:
        pass


__all__ = ["EdgeTTSImpl", "FakeTTS", "BaseTTS", "TTSVoice", "TTSError", "ZH_DEFAULT_VOICE"]
