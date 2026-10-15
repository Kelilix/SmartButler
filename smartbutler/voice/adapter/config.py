"""VoiceAdapterConfig — VoiceAdapter 配置。

Author: SmartButler Team
Date: 2026-10-15
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from smartbutler.voice.asr.base import BaseASR
    from smartbutler.voice.capture.base import BaseAudioCapture
    from smartbutler.voice.player.base import BaseAudioPlayer
    from smartbutler.voice.speaker.base import BaseSpeakerEmbedder
    from smartbutler.voice.tts.base import BaseTTS, TTSVoice
    from smartbutler.voice.vad.base import BaseVAD
    from smartbutler.voice.wake_word.base import BaseWakeWordDetector


@dataclass
class VoiceAdapterConfig:
    """VoiceAdapter 配置。

    设计:所有硬件依赖通过依赖注入传入,不在这里硬编码。
    这样可以在单测中注入 FakeMicCapture / FakeVAD / FakeASR 等 mock 对象。

    Attributes:
        device_id: 设备 ID(音箱/麦克风设备唯一标识)。
        user_id: 设备归属用户 ID(家主)。
        capture: 音频采集器(BaseAudioCapture 实现)。
        vad: 语音活动检测器(BaseVAD 实现)。
        asr: 自动语音识别器(BaseASR 实现)。
        tts: 文本转语音器(BaseTTS 实现)。
        embedder: 声纹识别器(BaseSpeakerEmbedder 实现,Phase 7.6 启用)。
        wake_word_detector: 唤醒词检测器(BaseWakeWordDetector 实现,可选)。
        player: 音频播放器(BaseAudioPlayer 实现),用于播放 TTS 生成的音频。
        tts_voice: TTS 默认音色配置。
        sample_rate: 采样率(Hz),默认 16000。
        silence_timeout_ms: 静音超时(毫秒),超过此时间无语音自动结束一段话。
        max_segment_duration_ms: 单段最大时长(毫秒),超过强制截断。
        buffer_max_ms: 缓冲最大时长(毫秒),超过强制送 ASR。
    """

    device_id: str
    user_id: str

    # 硬件依赖(依赖注入)
    capture: "BaseAudioCapture | None" = None
    vad: "BaseVAD | None" = None
    asr: "BaseASR | None" = None
    tts: "BaseTTS | None" = None
    embedder: "BaseSpeakerEmbedder | None" = None
    wake_word_detector: "BaseWakeWordDetector | None" = None
    player: "BaseAudioPlayer | None" = None
    tts_voice: "TTSVoice | None" = None

    # 音频参数
    sample_rate: int = 16000
    channels: int = 1

    # 时序参数
    silence_timeout_ms: int = 3000
    max_segment_duration_ms: int = 30000
    buffer_max_ms: int = 5000

    # 调试
    debug_audio_path: str | None = None  # 调试用:写原始音频到文件

    def validate(self) -> None:
        """校验配置合法性。"""
        if not self.device_id:
            raise ValueError("device_id must be non-empty")
        if not self.user_id:
            raise ValueError("user_id must be non-empty")
        if self.sample_rate < 8000 or self.sample_rate > 48000:
            raise ValueError(f"sample_rate must be 8-48kHz, got {self.sample_rate}")


__all__ = ["VoiceAdapterConfig"]
