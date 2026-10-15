"""VoiceService — 语音服务启动入口。

整合 ButlerServer + VoiceAdapter + EventBus,提供完整的语音交互服务。

Usage:
    # 开发模式(使用 Fake 组件)
    python -m smartbutler.scripts.voice_service

    # 生产模式(真实硬件)
    python -m smartbutler.scripts.voice_service --no-fake

    # 指定设备
    python -m smartbutler.scripts.voice_service --device-id speaker_01

Architecture:
```
VoiceService
├── ButlerServer
│   ├── ButlerOrchestrator
│   ├── InMemoryEventBus
│   ├── DefaultEventTrigger
│   ├── AnswerRouter (策略链)
│   └── TTSOutputAdapter → VoiceAdapter.speak()
│
└── VoiceAdapter
    ├── MicCapture (sounddevice)
    ├── SileroVAD (torch)
    ├── FunASR (funasr)
    ├── ResemblyzerEmbedder (声纹识别)
    └── wake_word_detector

Events Flow:
    [麦克风] → VoiceAdapter → VoiceEvent → EventBus → EventTrigger
                                                            ↓
                                                    ButlerOrchestrator
                                                            ↓
                                                     AnswerRouter
                                                            ↓
                                                    TTSOutputAdapter
                                                            ↓
                                                    VoiceAdapter.speak()
                                                            ↓
                                                        [音箱播放]
```

Author: SmartButler Team
Date: 2026-10-15
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from dataclasses import dataclass
from typing import Literal

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(name)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger(__name__)


@dataclass
class VoiceServiceConfig:
    """语音服务配置。"""

    # 设备信息
    device_id: str = "speaker_01"
    user_id: str = "gaotianyu"

    # 组件模式
    use_fakes: bool = True  # 开发模式

    # TTS 配置
    tts_impl: Literal["edge", "piper"] = "edge"
    tts_voice_id: str = "zh-CN-XiaoxiaoNeural"  # EdgeTTS 中文女声

    # 声纹配置
    embedder_impl: Literal["resemblyzer", "ecapa"] = "resemblyzer"

    # 唤醒词配置
    wake_word: str = "hey_butler"
    wake_energy_threshold: float = 0.0  # 0=任何声音都触发

    # 音频参数
    sample_rate: int = 16000
    silence_timeout_ms: int = 3000
    max_segment_duration_ms: int = 30000


async def create_voice_service(config: VoiceServiceConfig):
    """创建语音服务。"""
    from smartbutler.events.routing import (
        DeviceRegistry,
        SimplePresenceService,
        build_default_chain_answer_router,
    )
    from smartbutler.server.adapters import ButlerReply, OutputAdapter
    from smartbutler.server.bus import InMemoryEventBus
    from smartbutler.server.runtime import ButlerServer
    from smartbutler.thinking.loop.orchestrator import ButlerOrchestrator
    from smartbutler.voice.adapter.config import VoiceAdapterConfig
    from smartbutler.voice.adapter.tts_output_adapter import TTSOutputAdapter
    from smartbutler.voice.adapter.voice_adapter import VoiceAdapter
    from smartbutler.voice.tts.base import TTSVoice

    logger.info("=" * 60)
    logger.info("SmartButler Voice Service 启动中...")
    logger.info(f"  设备ID: {config.device_id}")
    logger.info(f"  用户ID: {config.user_id}")
    logger.info(f"  开发模式: {config.use_fakes}")
    logger.info(f"  TTS: {config.tts_impl}")
    logger.info(f"  声纹: {config.embedder_impl}")
    logger.info("=" * 60)

    # 1. 创建语音组件
    logger.info("[1/5] 初始化语音组件...")

    capture, vad, asr, tts, player, embedder, wake_word = _create_voice_components(config)

    # 2. 创建 TTS 音色配置
    tts_voice = TTSVoice(
        id=config.tts_voice_id,
        lang="zh-CN",
        gender="female",
    )

    # 3. 构造 VoiceAdapterConfig
    voice_config = VoiceAdapterConfig(
        device_id=config.device_id,
        user_id=config.user_id,
        capture=capture,
        vad=vad,
        asr=asr,
        tts=tts,
        player=player,
        embedder=embedder,
        wake_word_detector=wake_word,
        tts_voice=tts_voice,
        sample_rate=config.sample_rate,
        silence_timeout_ms=config.silence_timeout_ms,
        max_segment_duration_ms=config.max_segment_duration_ms,
    )

    # 4. 创建 EventBus
    logger.info("[2/5] 初始化事件总线...")
    event_bus = InMemoryEventBus()

    # 5. 创建 VoiceAdapter
    logger.info("[3/5] 初始化 VoiceAdapter...")
    voice_adapter = VoiceAdapter(
        config=voice_config,
        event_bus=event_bus,
        speaker_registry=None,  # 7.6 阶段暂不启用声纹
    )

    # 6. 创建 TTSOutputAdapter (实现 OutputAdapter 接口)
    logger.info("[4/5] 初始化 TTS 输出适配器...")
    tts_output_adapter = TTSOutputAdapter(
        tts=tts,
        player=player,
        voice=tts_voice,
        device_id=config.device_id,
    )

    # 7. 创建 DeviceRegistry + AnswerRouter
    logger.info("[5/5] 初始化路由...")
    device_registry = DeviceRegistry([tts_output_adapter])
    presence_service = SimplePresenceService()
    answer_router = build_default_chain_answer_router(
        registry=device_registry,
        presence_service=presence_service,
    )

    # 8. 创建 ButlerOrchestrator (需要 LLM)
    logger.info("创建管家大脑...")
    try:
        from smartbutler.capabilities.llm.factory import create_llm
        from smartbutler.config.llm import load_llm_settings

        llm_settings = load_llm_settings()
        llm = create_llm(llm_settings)
        orchestrator = ButlerOrchestrator(llm=llm, llm_settings=llm_settings)
        logger.info("  LLM: %s / %s", llm_settings.provider, llm_settings.model)
    except Exception as e:
        logger.warning("LLM 初始化失败: %s", e)
        logger.warning("  将使用模拟 Orchestrator")
        orchestrator = _create_mock_orchestrator()

    # 9. 创建 ButlerServer
    server = ButlerServer(
        orchestrator=orchestrator,
    )
    server.set_answer_router(answer_router)

    return voice_adapter, server, event_bus


def _create_voice_components(config: VoiceServiceConfig):
    """创建语音组件。"""
    if config.use_fakes:
        return _create_fake_components(config)
    else:
        return _create_real_components(config)


def _create_fake_components(config: VoiceServiceConfig):
    """创建 Fake 组件(开发模式)。"""
    import numpy as np

    from smartbutler.voice.asr.funasr import FakeASR
    from smartbutler.voice.capture.mic_capture import FakeMicCapture
    from smartbutler.voice.player.pygame_player import FakeAudioPlayer
    from smartbutler.voice.speaker.resemblyzer import FakeSpeakerEmbedder
    from smartbutler.voice.tts.edge_tts import FakeTTS
    from smartbutler.voice.vad.silero import FakeVAD
    from smartbutler.voice.wake_word.simple import SimpleWakeWordDetector

    logger.info("  使用 Fake 组件(开发模式)")

    capture = FakeMicCapture([
        # 模拟 1 秒音频
    ])
    vad = FakeVAD(is_speech_values=[True, True, False, False])
    asr = FakeASR(text="测试命令", confidence=0.95)
    tts = FakeTTS(chunk=b"FAKE_AUDIO_DATA")
    player = FakeAudioPlayer()
    embedder = FakeSpeakerEmbedder()
    wake_word = SimpleWakeWordDetector(
        keyword=config.wake_word,
        energy_threshold=config.wake_energy_threshold,
    )

    return capture, vad, asr, tts, player, embedder, wake_word


def _create_real_components(config: VoiceServiceConfig):
    """创建真实组件(生产模式)。"""
    from smartbutler.voice.asr.funasr import FunASR
    from smartbutler.voice.capture.mic_capture import MicCapture
    from smartbutler.voice.speaker.ecapa import EcapaTdnnEmbedder
    from smartbutler.voice.speaker.resemblyzer import ResemblyzerEmbedder
    from smartbutler.voice.tts.edge_tts import EdgeTTSImpl
    from smartbutler.voice.vad.silero import SileroVAD
    from smartbutler.voice.wake_word.simple import SimpleWakeWordDetector

    logger.info("  使用真实组件(生产模式)")

    # 音频采集
    try:
        capture = MicCapture()
        logger.info("  ✓ MicCapture (sounddevice)")
    except Exception as e:
        logger.error("  ✗ MicCapture 失败: %s", e)
        capture = None

    # VAD
    try:
        vad = SileroVAD()
        logger.info("  ✓ SileroVAD (torch)")
    except Exception as e:
        logger.error("  ✗ SileroVAD 失败: %s", e)
        vad = None

    # ASR
    try:
        asr = FunASR()
        logger.info("  ✓ FunASR")
    except Exception as e:
        logger.error("  ✗ FunASR 失败: %s", e)
        asr = None

    # TTS
    if config.tts_impl == "edge":
        try:
            tts = EdgeTTSImpl()
            logger.info("  ✓ EdgeTTS")
        except Exception as e:
            logger.error("  ✗ EdgeTTS 失败: %s", e)
            tts = None
    else:
        logger.warning("  Piper TTS 需要模型文件,请手动配置")
        tts = None

    # 播放器:优先 pygame,失败降级到 winsound(Windows 标准库,零依赖)
    player = _create_player()

    # 声纹识别
    if config.embedder_impl == "resemblyzer":
        try:
            embedder = ResemblyzerEmbedder()
            logger.info("  ✓ ResemblyzerEmbedder")
        except Exception as e:
            logger.error("  ✗ ResemblyzerEmbedder 失败: %s", e)
            embedder = None
    else:
        try:
            embedder = EcapaTdnnEmbedder()
            logger.info("  ✓ EcapaTdnnEmbedder")
        except Exception as e:
            logger.error("  ✗ EcapaTdnnEmbedder 失败: %s", e)
            embedder = None

    # 唤醒词
    wake_word = SimpleWakeWordDetector(
        keyword=config.wake_word,
        energy_threshold=config.wake_energy_threshold,
    )
    logger.info("  ✓ SimpleWakeWordDetector (任何声音都触发)")

    return capture, vad, asr, tts, player, embedder, wake_word


def _create_player():
    """智能创建播放器:pygame → winsound → 失败提示。"""
    # 1) 优先 pygame
    try:
        from smartbutler.voice.player.pygame_player import PygameAudioPlayer

        player = PygameAudioPlayer()
        logger.info("  ✓ PygameAudioPlayer (pygame)")
        return player
    except Exception as e:
        logger.warning("  ⚠ PygameAudioPlayer 不可用: %s", e)

    # 2) 降级:Windows winsound(零依赖)
    if sys.platform == "win32":
        try:
            from smartbutler.voice.player.winsound_player import WinsoundAudioPlayer

            player = WinsoundAudioPlayer()
            logger.info("  ✓ WinsoundAudioPlayer (Windows 标准库,零依赖)")
            return player
        except Exception as e:
            logger.warning("  ⚠ WinsoundAudioPlayer 不可用: %s", e)

    # 3) 都失败了
    logger.error("  ✗ 所有播放器都不可用!")
    logger.error("    建议: pip install pygame")
    return None


def _create_mock_orchestrator():
    """创建模拟 Orchestrator(无 LLM 时使用)。"""

    class MockOrchestrator:
        async def ainvoke(self, text, **kwargs):
            return f"[模拟回复] 你说的是: {text}"

        async def proactive_tick(self, event, **kwargs):
            class Result:
                acted = True
                message = f"[模拟主动] 事件: {event.topic}"
            return Result()

    return MockOrchestrator()


async def run_voice_service(config: VoiceServiceConfig):
    """运行语音服务。"""
    voice_adapter, server, event_bus = await create_voice_service(config)

    # 启动 Server
    await server.start()
    logger.info("ButlerServer 已启动")

    # 启动 VoiceAdapter
    await voice_adapter.start()
    logger.info("VoiceAdapter 已启动")

    logger.info("")
    logger.info("=" * 60)
    logger.info("SmartButler Voice Service 运行中!")
    logger.info("=" * 60)
    logger.info("")
    logger.info("按 Ctrl+C 停止服务")
    logger.info("")

    # 运行主循环
    running = True

    def signal_handler():
        nonlocal running
        running = False

    # 设置信号处理
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, signal_handler)
        except NotImplementedError:
            # Windows 不支持 add_signal_handler
            pass

    try:
        # 启动 VoiceAdapter 主循环
        voice_task = asyncio.create_task(voice_adapter.run())

        # 保持运行
        while running:
            await asyncio.sleep(1)

    except asyncio.CancelledError:
        logger.info("服务被取消")
    finally:
        logger.info("正在停止服务...")

        # 停止 VoiceAdapter
        voice_task.cancel()
        try:
            await voice_task
        except asyncio.CancelledError:
            pass

        await voice_adapter.stop()

        # 停止 Server
        await server.stop()

        logger.info("服务已停止")


def main():
    """命令行入口。"""
    import argparse

    parser = argparse.ArgumentParser(description="SmartButler Voice Service")
    parser.add_argument("--device-id", default="speaker_01", help="设备 ID")
    parser.add_argument("--user-id", default="gaotianyu", help="用户 ID")
    parser.add_argument(
        "--no-fake",
        action="store_true",
        help="使用真实硬件(而非 Fake 组件)",
    )
    parser.add_argument(
        "--tts",
        choices=["edge", "piper"],
        default="edge",
        help="TTS 实现",
    )
    parser.add_argument(
        "--embedder",
        choices=["resemblyzer", "ecapa"],
        default="resemblyzer",
        help="声纹识别实现",
    )
    parser.add_argument("--wake-word", default="hey_butler", help="唤醒词")
    parser.add_argument(
        "--energy-threshold",
        type=float,
        default=0.0,
        help="唤醒能量阈值(0-1, 0=任何声音都触发)",
    )

    args = parser.parse_args()

    config = VoiceServiceConfig(
        device_id=args.device_id,
        user_id=args.user_id,
        use_fakes=not args.no_fake,
        tts_impl=args.tts,
        embedder_impl=args.embedder,
        wake_word=args.wake_word,
        wake_energy_threshold=args.energy_threshold,
    )

    asyncio.run(run_voice_service(config))


if __name__ == "__main__":
    main()
