"""声纹识别模块。"""

from smartbutler.voice.speaker.base import (
    BaseSpeakerEmbedder,
    SpeakerEmbedderError,
    normalize_embedding,
)
from smartbutler.voice.speaker.resemblyzer import FakeSpeakerEmbedder, ResemblyzerEmbedder
from smartbutler.voice.speaker.ecapa import EcapaTdnnEmbedder

__all__ = [
    "BaseSpeakerEmbedder",
    "SpeakerEmbedderError",
    "normalize_embedding",
    "ResemblyzerEmbedder",
    "EcapaTdnnEmbedder",
    "FakeSpeakerEmbedder",
]
