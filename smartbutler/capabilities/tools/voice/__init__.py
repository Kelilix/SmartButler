"""Voice tools — 语音相关 Tool。"""

from smartbutler.capabilities.tools.voice.enroll_speaker import (
    decline_speaker_enrollment,
    enroll_speaker,
    set_speaker_registry,
    set_voice_adapter,
)

__all__ = [
    "enroll_speaker",
    "decline_speaker_enrollment",
    "set_speaker_registry",
    "set_voice_adapter",
]
