"""VoiceAdapter 工厂函数 — 快速组装完整语音交互链路。

## 使用方式

```python
from smartbutler.voice.adapter.factory import create_voice_adapter

# 方式 1: 使用 Fake 组件(开发/测试)
adapter = create_voice_adapter(
    device_id="smart_speaker_01",
    user_id="gaotianyu",
    use_fakes=True,  # 开发模式,无需麦克风/TTS
)

# 方式 2: 使用真实组件
adapter = create_voice_adapter(
    device_id="smart_speaker_01",
    user_id="gaotianyu",
    use_fakes=False,
    # 可选:指定具体实现
    # tts_impl="edge",  # edge-tts
    # embedder_impl="resemblyzer",  # resemblyzer
)

# 启动
await adapter.start()
await adapter.run()
```

## 组件矩阵

| 组件 | Fake 实现 | 真实实现 | 依赖 |
|------|-----------|----------|------|
| Capture | FakeMicCapture | MicCapture | sounddevice |
| VAD | FakeVAD | SileroVAD | torch |
| ASR | FakeASR | FunASR | funasr |
| TTS | FakeTTS | EdgeTTSImpl | edge-tts |
| Speaker | FakeSpeakerEmbedder | ResemblyzerEmbedder | resemblyzer |
| WakeWord | SimpleWakeWordDetector | PvPorcupineImpl | pvporcupine |
| Player | FakeAudioPlayer | WinsoundAudioPlayer / PygameAudioPlayer | soundfile(转码) / pygame |

Author: SmartButler Team
Date: 2026-10-15
"""

from __future__ import annotations

import sys
from typing import Literal

from smartbutler.events.core.event_bus import EventBus
from smartbutler.voice.adapter.config import VoiceAdapterConfig
from smartbutler.voice.adapter.voice_adapter import VoiceAdapter
from smartbutler.voice.asr.base import BaseASR
from smartbutler.voice.asr.funasr import FakeASR, FunASR
from smartbutler.voice.capture.base import BaseAudioCapture
from smartbutler.voice.capture.mic_capture import FakeMicCapture, MicCapture
from smartbutler.voice.player.base import BaseAudioPlayer
from smartbutler.voice.player.pygame_player import FakeAudioPlayer, PygameAudioPlayer
from smartbutler.voice.speaker.base import BaseSpeakerEmbedder
from smartbutler.voice.speaker.ecapa import EcapaTdnnEmbedder
from smartbutler.voice.speaker.resemblyzer import FakeSpeakerEmbedder, ResemblyzerEmbedder
from smartbutler.voice.tts.base import BaseTTS
from smartbutler.voice.tts.edge_tts import EdgeTTSImpl, FakeTTS
from smartbutler.voice.vad.base import BaseVAD
from smartbutler.voice.vad.silero import FakeVAD, SileroVAD
from smartbutler.voice.wake_word.base import BaseWakeWordDetector
from smartbutler.voice.wake_word.simple import SimpleWakeWordDetector


def create_voice_adapter(
    device_id: str,
    user_id: str,
    *,
    use_fakes: bool = True,
    tts_impl: Literal["edge", "piper"] = "edge",
    embedder_impl: Literal["resemblyzer", "ecapa"] = "resemblyzer",
    event_bus: EventBus | None = None,
    speaker_registry=None,
    **config_kwargs,
) -> VoiceAdapter:
    """创建 VoiceAdapter 实例。

    Args:
        device_id: 设备 ID。
        user_id: 用户 ID。
        use_fakes: 是否使用 Fake 组件(开发/测试模式)。
        tts_impl: TTS 实现,edge 或 piper。
        embedder_impl: 声纹实现,resemblyzer 或 ecapa。
        event_bus: EventBus 实例,None=创建新的 InMemoryEventBus。
        speaker_registry: SpeakerRegistry 实例,None=不进行声纹识别。
        **config_kwargs: 额外配置参数(silence_timeout_ms 等)。

    Returns:
        VoiceAdapter 实例。
    """
    # 解析组件
    capture = _create_capture(use_fakes)
    vad = _create_vad(use_fakes)
    asr = _create_asr(use_fakes)
    tts = _create_tts(use_fakes, tts_impl)
    player = _create_player(use_fakes)
    embedder = _create_embedder(use_fakes, embedder_impl)
    wake_word = _create_wake_word(use_fakes)

    # 构造配置
    config = VoiceAdapterConfig(
        device_id=device_id,
        user_id=user_id,
        capture=capture,
        vad=vad,
        asr=asr,
        tts=tts,
        player=player,
        embedder=embedder,
        wake_word_detector=wake_word,
        **config_kwargs,
    )

    # 创建 EventBus
    if event_bus is None:
        from smartbutler.server.bus import InMemoryEventBus
        event_bus = InMemoryEventBus()

    # 创建 VoiceAdapter
    return VoiceAdapter(
        config=config,
        event_bus=event_bus,
        speaker_registry=speaker_registry,
    )


def _create_capture(use_fakes: bool) -> BaseAudioCapture | None:
    """创建音频采集器。"""
    if use_fakes:
        import numpy as np
        from smartbutler.voice.capture.base import AudioChunk
        chunks = [
            AudioChunk(audio=np.random.rand(16000).astype(np.float32) * 0.1),
            AudioChunk(audio=np.random.rand(16000).astype(np.float32) * 0.1, is_final=True),
        ]
        return FakeMicCapture(chunks)
    # 真实:MicCapture 需要硬件,这里返回 None
    # 用户可以手动传入 MicCapture()
    return None


def _create_vad(use_fakes: bool) -> BaseVAD | None:
    """创建 VAD。"""
    if use_fakes:
        return FakeVAD(is_speech_values=[True, True, False, False])
    # 真实:SileroVAD 需要 torch
    # 用户可以手动传入 SileroVAD()
    return None


def _create_asr(use_fakes: bool) -> BaseASR | None:
    """创建 ASR。"""
    if use_fakes:
        return FakeASR(text="测试文本", confidence=0.95)
    # 真实:FunASR 需要 funasr
    # 用户可以手动传入 FunASR()
    return None


def _create_tts(use_fakes: bool, impl: str) -> BaseTTS | None:
    """创建 TTS。"""
    if use_fakes:
        return FakeTTS(chunk=b"FAKE_AUDIO_DATA")
    if impl == "edge":
        try:
            return EdgeTTSImpl()
        except Exception:
            return None
    elif impl == "piper":
        try:
            from smartbutler.voice.tts.piper_tts import PiperTTSImpl
            # 需要指定模型路径
            return None  # Piper 需要模型文件,这里返回 None
        except Exception:
            return None
    return None


def _create_player(use_fakes: bool) -> BaseAudioPlayer | None:
    """创建播放器。

    Windows 优先 winsound(标准库,零依赖,且能自动转码 MP3);
    非 Windows 只能用 pygame。
    """
    if use_fakes:
        return FakeAudioPlayer()

    if sys.platform == "win32":
        try:
            from smartbutler.voice.player.winsound_player import WinsoundAudioPlayer

            return WinsoundAudioPlayer()
        except Exception:
            pass

    # 真实:pygame 需要 pygame
    try:
        return PygameAudioPlayer()
    except Exception:
        return None


def _create_embedder(use_fakes: bool, impl: str) -> BaseSpeakerEmbedder | None:
    """创建声纹识别器。"""
    if use_fakes:
        return FakeSpeakerEmbedder()
    if impl == "resemblyzer":
        try:
            return ResemblyzerEmbedder()
        except Exception:
            return None
    elif impl == "ecapa":
        try:
            return EcapaTdnnEmbedder()
        except Exception:
            return None
    return None


def _create_wake_word(use_fakes: bool) -> BaseWakeWordDetector | None:
    """创建唤醒词检测器。"""
    if use_fakes:
        # 使用简单的能量阈值唤醒词检测器
        # energy_threshold=0.0 意味着任何音频都会触发(适合开发测试)
        return SimpleWakeWordDetector(
            keyword="hey_butler",
            energy_threshold=0.0,
        )
    # 真实:PvPorcupineImpl 需要 pvporcupine 和 access_key
    # 用户可以手动传入 PvPorcupineImpl(access_key="...")
    return None


__all__ = ["create_voice_adapter"]
