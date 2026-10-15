"""Voice 模块根。

Architecture:
```
voice/
├── capture/     # 麦克风采集
│   ├── base.py       BaseAudioCapture (抽象)
│   └── mic_capture.py  MicCapture (sounddevice 实现)
├── vad/         # 语音活动检测
│   ├── base.py       BaseVAD (抽象)
│   └── silero.py     SileroVAD (torch 实现)
├── asr/         # 自动语音识别
│   ├── base.py       BaseASR (抽象)
│   └── funasr.py     FunASR (funasr 实现)
├── tts/         # 文本转语音
│   ├── base.py       BaseTTS (抽象)
│   ├── edge_tts.py   EdgeTTSImpl (edge-tts 实现, Phase 7.4 首选)
│   └── piper.py      PiperTTSImpl (piper 实现, Phase 7.4+ 离线备选)
├── speaker/     # 声纹识别
│   ├── base.py       BaseSpeakerEmbedder (抽象)
│   ├── resemblyzer.py ResemblyzerEmbedder (resemblyzer 实现, Phase 7.6 首选)
│   └── ecapa.py      EcapaTdnnEmbedder (ecapa-tdnn 实现, Phase 7.6+ 备选)
├── wake_word/   # 唤醒词检测
│   ├── base.py       BaseWakeWordDetector (抽象)
│   └── pv_porcupine.py  PvPorcupineImpl (Picovoice 实现)
├── player/      # 音频播放
│   ├── base.py       BaseAudioPlayer (抽象)
│   └── pygame_player.py PygameAudioPlayer (pygame 实现)
└── adapter/     # VoiceAdapter (集成入口)
    ├── voice_adapter.py   VoiceAdapter (主循环)
    ├── config.py          VoiceAdapterConfig (配置)
    └── tts_output_adapter.py TTSOutputAdapter (OutputAdapter 接口实现)
```
    30|
设计原则:
1. Base 归一化接口:每个模块都有抽象 Base 类,暴露稳定顶级 API 给业务方。
2. 实现可插拔:内部可替换为不同技术方案(resemblyzer ↔ ecapa-tdnn, edge-tts ↔ piper),   不影响业务方调用。
3. 单测友好:所有 Base 类可 mock,无需物理硬件即可验证核心逻辑。
4. 默认实现:每个子模块提供开箱即用的简化实现,快速跑通最小原型。

Author: SmartButler Team
Date: 2026-10-15
"""

from smartbutler.voice.capture.base import BaseAudioCapture
from smartbutler.voice.vad.base import BaseVAD
from smartbutler.voice.asr.base import BaseASR
from smartbutler.voice.tts.base import BaseTTS
from smartbutler.voice.speaker.base import BaseSpeakerEmbedder
from smartbutler.voice.wake_word.base import BaseWakeWordDetector
from smartbutler.voice.player.base import BaseAudioPlayer

__all__ = [
    "BaseAudioCapture",
    "BaseVAD",
    "BaseASR",
    "BaseTTS",
    "BaseSpeakerEmbedder",
    "BaseWakeWordDetector",
    "BaseAudioPlayer",
]
