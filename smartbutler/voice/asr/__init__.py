"""ASR 模块。"""

from smartbutler.voice.asr.base import ASRError, ASRResult, BaseASR
from smartbutler.voice.asr.funasr import FakeASR, FunASR

__all__ = ["BaseASR", "ASRResult", "ASRError", "FunASR", "FakeASR"]
