# SmartButler Voice Service 启动指南

> 完整的麦克风 → 声纹 → ASR → 管家大脑 → TTS → 音箱播放 链路
> 作者: SmartButler Team
> 更新: 2026-10-15

## 1. 快速启动

### 1.1 开发模式（无需硬件）

```bash
# 直接运行（使用 Fake 组件，无需麦克风/TTS 依赖）
python -m smartbutler.scripts.voice_service

# 自定义设备 ID 和用户 ID
python -m smartbutler.scripts.voice_service --device-id speaker_01 --user-id gaotianyu
```

### 1.2 生产模式（真实硬件）

```bash
# 使用真实麦克风 + 声卡
python -m smartbutler.scripts.voice_service --no-fake

# 指定 TTS 实现
python -m smartbutler.scripts.voice_service --no-fake --tts edge

# 指定声纹模型
python -m smartbutler.scripts.voice_service --no-fake --embedder resemblyzer
```

### 1.3 依赖安装

```bash
# 核心依赖
pip install pydantic structlog

# 语音依赖（生产模式必需）
pip install sounddevice    # 麦克风采集
pip install torch          # Silero VAD（可选，silero-vad 也会自动装）
pip install funasr         # 中文 ASR
pip install edge-tts       # 在线 TTS（免费）
pip install pygame         # 音频播放
pip install resemblyzer    # 声纹识别

# 唤醒词（可选，需要 Picovoice AccessKey）
pip install pvporcupine
```

## 2. 真实音响接入方案

### 2.1 方案对比

| 方案 | 成本 | 复杂度 | 延迟 | 音质 | 推荐场景 |
|------|------|--------|------|------|----------|
| **本地音箱 (3.5mm/USB)** | ¥100-500 | ⭐ | 最低 | 依赖音箱 | 树莓派 + 小音箱 |
| **HomePod / 小爱 / 天猫精灵** | ¥500-2000 | ⭐⭐ | 低 | 好 | 普通家庭 |
| **专业音响 + DAC** | ¥2000+ | ⭐⭐⭐ | 最低 | 优秀 | 极客玩家 |
| **树莓派 Zero + 蓝牙音箱** | ¥100-300 | ⭐ | 中 | 依赖音箱 | 极简方案 |

### 2.2 推荐方案：本地 USB 音箱

```
[麦克风] → [树莓派/PC] → [EdgeTTS] → [Pygame] → [USB 音箱] → 🔊
```

**步骤**：
1. 接入 USB 麦克风 + USB 音箱
2. 安装 `sounddevice` 和 `pygame`
3. 启动服务（生产模式）

```bash
python -m smartbutler.scripts.voice_service --no-fake
```

### 2.3 HomePod / 小爱 / 天猫精灵接入

这些智能音箱**通常不支持第三方应用**直接驱动音响播放，
但可以走以下两条路：

#### 方案 A: AirPlay (HomePod)
```python
# 1. 安装依赖
# pip install pyatv

# 2. 自定义 AudioPlayer
class AirPlayAudioPlayer(BaseAudioPlayer):
    def __init__(self, atv_identifier: str):
        self.atv = await pyatv.connect(atv_identifier)
    
    async def play(self, audio_stream):
        # 把 TTS 音频流式推送到 HomePod
        await self.atv.stream.stream_file(audio_file)
```

#### 方案 B: HomeAssistant + Media Player
```python
# 1. 通过 HomeAssistant 统一控制
# pip install homeassistant-api

class HAAudioPlayer(BaseAudioPlayer):
    def __init__(self, entity_id: str):
        self.entity_id = entity_id  # "media_player.homepod"
    
    async def play(self, audio_stream):
        # 1. 把音频流保存为临时文件
        path = save_temp_file(audio_stream)
        # 2. 推送到 HA
        await ha.call_service("media_player.play_media", {
            "entity_id": self.entity_id,
            "media_content_id": f"http://butler.local/{path}",
            "media_content_type": "music",
        })
```

#### 方案 C: MQTT + ESP32 音响
```
[ButlerServer] → [MQTT broker] → [ESP32] → [MAX98357A DAC] → [音箱]
                  192.168.1.x    wifi      音频流           PCM5102A
```

**硬件清单**（约 ¥80）：
- ESP32-S3 DevKit（¥30）
- MAX98357A I2S DAC（¥25）
- 4Ω 3W 喇叭（¥20）
- 杜邦线（¥5）

**软件**（ESP32 端）：
```cpp
// ESP32 接收 TTS 音频流
#include <WiFi.h>
#include <PubSubClient.h>

void onTTSAudioCallback(char* topic, byte* payload, unsigned int length) {
    if (strcmp(topic, "butler/tts") == 0) {
        // 直接喂给 I2S DAC
        i2s_write(I2S_NUM_0, payload, length, &bytes_written, portMAX_DELAY);
    }
}
```

### 2.4 专业音响方案（高端用户）

```
[Butler] → [DAC (Topping D50s)] → [功放] → [书架箱]
                                    SMSL    KEF LS50
                                    ¥1500   ¥5000
```

适用：客厅 HiFi 系统集成。

### 2.5 直接通过 PC 声卡播放

最简单的方案（开发测试用）：
```
[Butler] → [Pygame] → [PC 声卡] → [3.5mm 耳机/音箱]
```

启动：
```bash
# 1. 确保 PC 有麦克风（笔记本自带/USB 麦）
# 2. 确保有音箱/耳机
python -m smartbutler.scripts.voice_service --no-fake
```

## 3. 编程方式启动

### 3.1 最简方式

```python
import asyncio
from smartbutler.scripts.voice_service import VoiceServiceConfig, run_voice_service

config = VoiceServiceConfig(
    device_id="speaker_living_room",
    user_id="gaotianyu",
    use_fakes=False,  # 生产模式
    tts_impl="edge",  # edge-tts
    embedder_impl="resemblyzer",
)

asyncio.run(run_voice_service(config))
```

### 3.2 完全自定义

```python
import asyncio
from smartbutler.voice.adapter.voice_adapter import VoiceAdapter
from smartbutler.voice.adapter.config import VoiceAdapterConfig
from smartbutler.voice.capture.mic_capture import MicCapture
from smartbutler.voice.vad.silero import SileroVAD
from smartbutler.voice.asr.funasr import FunASR
from smartbutler.voice.tts.edge_tts import EdgeTTSImpl
from smartbutler.voice.player.pygame_player import PygameAudioPlayer
from smartbutler.voice.speaker.resemblyzer import ResemblyzerEmbedder
from smartbutler.voice.wake_word.pv_porcupine import PvPorcupineImpl
from smartbutler.server.bus import InMemoryEventBus
from smartbutler.voice.tts.base import TTSVoice

async def main():
    # 1. 创建组件
    config = VoiceAdapterConfig(
        device_id="speaker_01",
        user_id="gaotianyu",
        capture=MicCapture(device_index=0),    # 麦克风
        vad=SileroVAD(),                         # 语音活动检测
        asr=FunASR(),                            # 中文 ASR
        tts=EdgeTTSImpl(),                       # 在线 TTS
        player=PygameAudioPlayer(),              # pygame 播放
        embedder=ResemblyzerEmbedder(),          # 声纹识别
        wake_word_detector=PvPorcupineImpl(      # Porcupine 唤醒
            keywords=["hey butler"],
            access_key="YOUR_PICOVOICE_KEY",
        ),
        tts_voice=TTSVoice(id="zh-CN-XiaoxiaoNeural", lang="zh-CN"),
    )
    
    bus = InMemoryEventBus()
    adapter = VoiceAdapter(config=config, event_bus=bus)
    
    # 2. 启动
    await adapter.start()
    
    # 3. 运行主循环
    try:
        await adapter.run()
    except KeyboardInterrupt:
        pass
    finally:
        await adapter.stop()

asyncio.run(main())
```

## 4. 端到端测试

### 4.1 开发模式测试

```bash
# 1. 启动服务（开发模式）
python -m smartbutler.scripts.voice_service

# 应该看到:
# [info] SmartButler Voice Service 启动中...
# [info]   设备ID: speaker_01
# [info]   用户ID: gaotianyu
# [info]   开发模式: True
# [info] SmartButler Voice Service 运行中!
# [info] 按 Ctrl+C 停止服务
```

### 4.2 生产模式测试

```bash
# 1. 安装依赖
pip install sounddevice edge-tts pygame funasr torch

# 2. 测试麦克风
python -c "
import sounddevice as sd
print('Devices:', sd.query_devices())
"

# 3. 测试 TTS
python -c "
import asyncio
from smartbutler.voice.tts.edge_tts import EdgeTTSImpl
async def test():
    tts = EdgeTTSImpl()
    async for chunk in tts.speak('你好'):
        print(f'Got {len(chunk)} bytes')
asyncio.run(test())
"

# 4. 启动服务
python -m smartbutler.scripts.voice_service --no-fake

# 5. 说话测试
# 说 "hey butler" → 应该听到 TTS 回复
```

## 5. 故障排查

### 5.1 麦克风问题

```bash
# 检查可用麦克风
python -c "import sounddevice as sd; print(sd.query_devices())"

# 指定设备
python -m smartbutler.scripts.voice_service --no-fake  # 修改代码指定 device_index
```

### 5.2 TTS 没有声音

```python
# 1. 检查 pygame mixer 是否初始化
from smartbutler.voice.player.pygame_player import PygameAudioPlayer
player = PygameAudioPlayer()  # 应该不抛异常

# 2. 检查默认输出设备
import pygame
pygame.mixer.init()
print(pygame.mixer.get_init())  # 应有输出
```

### 5.3 Edge TTS 慢/超时

```python
# 1. 网络问题 → 改用 Piper（本地 TTS）
python -m smartbutler.scripts.voice_service --no-fake --tts piper

# 2. Edge TTS API 限速 → 加缓存或换 Piper
```

## 6. 部署到生产

### 6.1 用 systemd 守护（Linux）

```ini
# /etc/systemd/system/smartbutler-voice.service
[Unit]
Description=SmartButler Voice Service
After=network.target

[Service]
Type=simple
User=butler
WorkingDirectory=/opt/smartbutler
ExecStart=/opt/smartbutler/.venv/bin/python -m smartbutler.scripts.voice_service --no-fake
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable smartbutler-voice
sudo systemctl start smartbutler-voice
```

### 6.2 用 Docker

```dockerfile
FROM python:3.11-slim

# 系统依赖（ALSA for sounddevice, pulseaudio for pygame）
RUN apt-get update && apt-get install -y \
    libasound2-dev \
    portaudio19-dev \
    pulseaudio \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . .

RUN pip install --no-cache-dir -e .[all]

# 麦克风设备
# docker run --device /dev/snd ...

CMD ["python", "-m", "smartbutler.scripts.voice_service", "--no-fake"]
```

### 6.3 用 Windows Service

```bash
# 用 NSSM (Non-Sucking Service Manager)
nssm install SmartButlerVoice "C:\path\to\python.exe" "-m smartbutler.scripts.voice_service --no-fake"
nssm start SmartButlerVoice
```

## 7. 进阶：分布式部署

```
[Pi 1: 麦克风]  →  [EventBus]  ←  [Pi 2: LLM 大脑]
                       ↓
                  [Pi 3: TTS]  →  [HomePod]
```

EventBus 支持 Redis/Kafka 分布式版本，可部署多节点。

## 8. 总结

### 推荐起步方案
1. **开发**: `python -m smartbutler.scripts.voice_service`（开发模式）
2. **测试**: PC + USB 麦 + 耳机/小音箱
3. **家庭**: 树莓派 + USB 麦 + 蓝牙音箱
4. **生产**: 智能音箱 + HomeAssistant

### 关键点
- 链路已打通: Mic → VAD → ASR → 声纹 → VoiceEvent → EventBus → Orchestrator → AnswerRouter → TTS → 音箱
- Base 抽象: 可随时换实现（edge-tts ↔ piper, resemblyzer ↔ ecapa-tdnn）
- 懒加载: heavy 依赖（torch/funasr/pygame）按需加载
- 单测友好: 所有组件都有 Fake* 实现

## 9. 完整命令参考

```bash
# 查看所有选项
python -m smartbutler.scripts.voice_service --help

# 开发模式（默认）
python -m smartbutler.scripts.voice_service

# 生产模式 + 真实硬件
python -m smartbutler.scripts.voice_service --no-fake

# 完整生产模式
python -m smartbutler.scripts.voice_service \
    --no-fake \
    --device-id speaker_living_room \
    --user-id gaotianyu \
    --tts edge \
    --embedder resemblyzer \
    --wake-word "hey butler" \
    --energy-threshold 0.0
```
