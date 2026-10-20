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
from typing import TYPE_CHECKING, Literal

from smartbutler.config.voice import load_voice_settings

if TYPE_CHECKING:
    pass

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(name)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger(__name__)


@dataclass
class VoiceServiceConfig:
    """语音服务配置。

    所有阈值字段的默认值都从 ``.env`` / 环境变量读取(前缀 ``SMARTBUTLER_VOICE_``),
    命令行参数会覆盖 env 值(优先级: CLI > env > dataclass 默认)。

    例如 ``.env`` 里写::

        SMARTBUTLER_VOICE_VAD_THRESHOLD=0.005
        SMARTBUTLER_VOICE_SILENCE_TIMEOUT_MS=2000
        SMARTBUTLER_VOICE_MAX_SEGMENT_DURATION_MS=15000

    启动时直接生效, 不需要改代码。
    """

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

    # 音频参数(默认值由 env 注入, 见 __post_init__)
    sample_rate: int = 16000
    silence_timeout_ms: int = 3000
    max_segment_duration_ms: int = 30000
    vad_threshold: float = 0.3

    def __post_init__(self) -> None:
        """从 .env / 环境变量读取默认值(只在 dataclass 默认值时生效)。

        命令行参数解析后会显式覆盖这些字段, 所以 CLI 优先级最高。
        """
        settings = load_voice_settings()
        # 只在 dataclass 默认值时填入, 避免覆盖 CLI/显式传入的值
        _DEFAULT_VAD = 0.3
        _DEFAULT_SILENCE = 3000
        _DEFAULT_MAX_SEGMENT = 30000
        if self.vad_threshold == _DEFAULT_VAD:
            self.vad_threshold = settings.vad_threshold
        if self.silence_timeout_ms == _DEFAULT_SILENCE:
            self.silence_timeout_ms = settings.silence_timeout_ms
        if self.max_segment_duration_ms == _DEFAULT_MAX_SEGMENT:
            self.max_segment_duration_ms = settings.max_segment_duration_ms


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

    # 🆕 Phase 7.6: 初始化 SQLite 声纹注册表（持久化）
    from pathlib import Path

    from smartbutler.events.routing.speaker_registry_sqlite import (
        SQLiteSpeakerRegistry,
    )

    speaker_registry: SQLiteSpeakerRegistry | None = None
    if embedder is not None:
        try:
            speaker_registry = SQLiteSpeakerRegistry(
                embedder=embedder,
                db_path=Path("data/speakers.db"),
            )
            await speaker_registry.start()
            registered_count = len(await speaker_registry.list_members())
            logger.info(
                "  ✓ SQLiteSpeakerRegistry (已加载 %d 个家庭成员声纹)", registered_count
            )
        except Exception as e:
            logger.error("  ✗ SQLiteSpeakerRegistry 初始化失败: %s", e)
            speaker_registry = None

    voice_adapter = VoiceAdapter(
        config=voice_config,
        event_bus=event_bus,
        speaker_registry=speaker_registry,  # 🆕 Phase 7.6: 接入声纹识别
    )

    # 🆕 Phase 7.6: 把 voice_adapter 和 speaker_registry 注入到 enroll_speaker Tool
    # 让 LLM 调 Tool 时能触发现实麦克风采集和声纹持久化
    try:
        from smartbutler.capabilities.tools.voice import (
            set_speaker_registry,
            set_voice_adapter,
        )

        set_voice_adapter(voice_adapter)
        if speaker_registry is not None:
            set_speaker_registry(speaker_registry)
        logger.info("  ✓ enroll_speaker Tool 已就绪")
    except Exception as e:
        logger.warning("  ⚠ enroll_speaker Tool 注入失败: %s", e)

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
    # 🐛 修复:必须把同一个 event_bus 传给 server,否则 voice_adapter 发布的事件
    #    server 收不到(各自有不同的 InMemoryEventBus 实例)
    from smartbutler.server.runtime import ServerConfig

    server_config = ServerConfig(
        event_bus=event_bus,  # 关键:用上面 voice_adapter 用的同一个 bus
        enable_event_bus=True,
        auto_subscribe_proactive=True,
    )
    server = ButlerServer(
        orchestrator=orchestrator,
        config=server_config,
    )
    server.set_answer_router(answer_router)

    return voice_adapter, server, event_bus, speaker_registry


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
        # 透传 --device 参数(让用户能指定 KPods SE 等)
        mic_device = getattr(config, "mic_device", None)
        # 默认开启自动增益(对耳机麦克风/小音量麦克风很有用)
        capture = MicCapture(device_index=mic_device, auto_gain=True, gain_target_rms=0.1)
        if mic_device is not None:
            logger.info("  ✓ MicCapture (sounddevice, device=%d, auto_gain=ON)", mic_device)
        else:
            logger.info("  ✓ MicCapture (sounddevice, default, auto_gain=ON)")
    except Exception as e:
        logger.error("  ✗ MicCapture 失败: %s", e)
        capture = None

    # VAD(用 --vad-threshold 调灵敏度;默认 0.3 对增益后的音频更友好)
    try:
        vad_threshold = getattr(config, "vad_threshold", 0.3)
        vad = SileroVAD(threshold=vad_threshold)
        logger.info("  ✓ SileroVAD (torch, threshold=%.2f)", vad_threshold)
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
    voice_adapter, server, event_bus, speaker_registry = await create_voice_service(config)

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

        # 🆕 Phase 7.6: 关闭 SQLite 声纹注册表
        if speaker_registry is not None:
            try:
                await speaker_registry.stop()
                logger.info("  ✓ SQLiteSpeakerRegistry 已关闭")
            except Exception as e:
                logger.warning("  ⚠ SQLiteSpeakerRegistry 关闭失败: %s", e)

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
    parser.add_argument(
        "--vad-threshold",
        type=float,
        default=None,  # None → 用 .env / dataclass 默认值
        help=(
            "VAD 语音概率阈值(0-1, 默认从 .env SMARTBUTLER_VOICE_VAD_THRESHOLD 读取, "
            "越低越敏感。麦克风音量小时调到 0.005-0.1)"
        ),
    )
    parser.add_argument(
        "--silence-timeout-ms",
        type=int,
        default=None,
        help=(
            "静音超时(毫秒)。连续静音超过此时长 → flush buffer 送 ASR。"
            "默认从 .env SMARTBUTLER_VOICE_SILENCE_TIMEOUT_MS 读取(3000)。"
        ),
    )
    parser.add_argument(
        "--max-segment-ms",
        type=int,
        default=None,
        help=(
            "单段最大时长(毫秒)。即使用户不停说话, 超过此时长也强制 flush。"
            "默认从 .env SMARTBUTLER_VOICE_MAX_SEGMENT_DURATION_MS 读取(30000, 生产建议 15000)。"
        ),
    )
    parser.add_argument(
        "--device",
        type=int,
        default=None,
        help="音频输入设备 ID(默认=系统默认。用 --list-devices 查看可用设备)",
    )
    parser.add_argument(
        "--list-devices",
        action="store_true",
        help="列出所有音频输入/输出设备后退出",
    )

    args = parser.parse_args()

    # 列出设备后退出
    if args.list_devices:
        from smartbutler.scripts.mic_test import list_devices as _list_devices
        asyncio.run(_list_devices())
        return

    config = VoiceServiceConfig(
        device_id=args.device_id,
        user_id=args.user_id,
        use_fakes=not args.no_fake,
        tts_impl=args.tts,
        embedder_impl=args.embedder,
        wake_word=args.wake_word,
        wake_energy_threshold=args.energy_threshold,
    )

    # CLI 参数优先级最高: 用 None 判断"用户没传"才覆盖
    if args.vad_threshold is not None:
        config.vad_threshold = args.vad_threshold  # type: ignore[attr-defined]
    if args.silence_timeout_ms is not None:
        config.silence_timeout_ms = args.silence_timeout_ms  # type: ignore[attr-defined]
    if args.max_segment_ms is not None:
        config.max_segment_duration_ms = args.max_segment_ms  # type: ignore[attr-defined]

    config.mic_device = args.device  # type: ignore[attr-defined]

    # 打印生效配置(让用户看到 .env 是否真的起作用了)
    logger.info(
        "  VAD threshold:    %s",
        f"{config.vad_threshold:.4f} (env=SMARTBUTLER_VOICE_VAD_THRESHOLD)",
    )
    logger.info(
        "  Silence timeout:  %sms (env=SMARTBUTLER_VOICE_SILENCE_TIMEOUT_MS)",
        config.silence_timeout_ms,
    )
    logger.info(
        "  Max segment:      %sms (env=SMARTBUTLER_VOICE_MAX_SEGMENT_DURATION_MS)",
        config.max_segment_duration_ms,
    )

    asyncio.run(run_voice_service(config))


if __name__ == "__main__":
    main()
