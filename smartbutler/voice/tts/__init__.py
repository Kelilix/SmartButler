"""TTS 模块。"""

from smartbutler.voice.tts.base import (
    ZH_DEFAULT_VOICE,
    ZH_MALE_VOICE,
    BaseTTS,
    TTSError,
    TTSVoice,
)
from smartbutler.voice.tts.edge_tts import EdgeTTSImpl, FakeTTS
from smartbutler.voice.tts.piper_tts import PiperTTSImpl

__all__ = [
    "BaseTTS",
    "TTSError",
    "TTSVoice",
    "ZH_DEFAULT_VOICE",
    "ZH_MALE_VOICE",
    "EdgeTTSImpl",
    "PiperTTSImpl",
    "FakeTTS",
]
