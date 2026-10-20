"""Voice tools — 语音相关 Tool。"""

from smartbutler.capabilities.tools.voice.enroll_speaker import (
    enroll_speaker,
    set_speaker_registry,
    set_voice_adapter,
)

__all__ = [
    "enroll_speaker",
    "set_speaker_registry",
    "set_voice_adapter",
]
