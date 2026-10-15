"""VAD 模块。"""

from smartbutler.voice.vad.base import BaseVAD, VADError
from smartbutler.voice.vad.silero import FakeVAD, SileroVAD

__all__ = ["BaseVAD", "VADError", "SileroVAD", "FakeVAD"]
