"""VoiceAdapter — 语音交互集成入口。

Architecture:
```
[麦克风] → [VAD] → [Buffer] → [ASR] → [声纹] → [VoiceEvent] → [EventBus]
                                        ↓
[唤醒词] ← (唤醒状态) ← (唤醒词检测)
                                        ↓
                            [TTS] ← (管家回复) ← [Orchestrator]
                                        ↓
                                    [播放器]
```

设计原则:
1. 集成所有 voice 子模块(MicCapture / VAD / ASR / TTS / 声纹识别 / 播放器)。
2. 暴露简洁生命周期 API:start() / run() / stop()。
3. 依赖注入:所有子模块通过 VoiceAdapterConfig 注入,单测可 mock。
4. 声纹识别:Phase 7.6 启用,7.5 阶段 embedder=None,VoiceEvent.speakers=[]。
5. 唤醒词:可选,也可由外部事件驱动(如物理按钮)。
6. TTS 播放:通过 AudioPlayer 输出到音箱,支持打断。

Events Published:
- VoiceEvent.make(action=WAKE_WORD, ...) → EventBus (唤醒词触发)
- VoiceEvent.make(action=COMMAND, asr_text=..., speakers=[...]) → EventBus (命令)

Events Consumed:
- 无(纯事件生产者,回复通过 Orchestrator → AnswerRouter → TTSOutputAdapter)

Author: SmartButler Team
Date: 2026-10-15
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from typing import TYPE_CHECKING, Any

import numpy as np

from smartbutler.events.core.event import SpeakerInfo
from smartbutler.events.core.event_bus import EventBus
from smartbutler.events.protocol.voice_event import VoiceAction, VoiceEvent
from smartbutler.voice.adapter.config import VoiceAdapterConfig
from smartbutler.voice.capture.base import AudioSegment
from smartbutler.voice.player.base import BaseAudioPlayer
from smartbutler.voice.speaker.base import BaseSpeakerEmbedder
from smartbutler.voice.tts.base import TTSVoice

if TYPE_CHECKING:
    from smartbutler.events.routing.speaker_registry import SpeakerRegistry

logger = logging.getLogger(__name__)


def _rms(audio: np.ndarray) -> float:
    """计算音频 RMS(用于诊断片段里有没有实际声音)。"""
    if audio is None or len(audio) == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.asarray(audio, dtype=np.float32) ** 2)))


class VoiceAdapter:
    """语音交互集成入口。

    Usage:
        # 生产:真实硬件
        config = VoiceAdapterConfig(
            device_id="smart_speaker_01",
            user_id="gaotianyu",
            capture=MicCapture(),
            vad=SileroVAD(),
            asr=FunASR(),
            tts=EdgeTTSImpl(),
            embedder=ResemblyzerEmbedder(),  # Phase 7.6
        )
        adapter = VoiceAdapter(config, event_bus, speaker_registry=None)
        await adapter.start()
        await adapter.run()  # 阻塞,直到 stop()

        # 单测:Fake 组件
        adapter = VoiceAdapter(config_with_fakes, event_bus, speaker_registry=fake_registry)

    State Machine:
        IDLE → LISTENING → SPEAKING → IDLE
        IDLE:等待唤醒词(若配置了 wake_word_detector)
        LISTENING:等待用户说话(VAD 检测中)
        SPEAKING:处理命令 + 等待回复播放完成
    """

    class State:
        IDLE = "idle"
        LISTENING = "listening"
        SPEAKING = "speaking"
        # 🆕 2026-11-01 (Phase 7.6.1): 声纹采集同意窗口。
        # 等用户回答"要不要录声纹",收到回答后发 CONFIRM_ANSWER 事件交给 LLM 判定。
        CONFIRMING = "confirming"
        # 🆕 2026-11-01 (Phase 7.6.1): 声纹采集中(用户已明确同意)。
        ENROLLING = "enrolling"

    def __init__(
        self,
        config: VoiceAdapterConfig,
        event_bus: EventBus,
        speaker_registry: "SpeakerRegistry | None" = None,
    ) -> None:
        self._cfg = config
        self._bus = event_bus
        self._registry = speaker_registry
        self._state = self.State.IDLE
        self._running = False
        self._buffer: list[np.ndarray] = []
        self._buffer_ms = 0
        # 🐛 单段是否已超上限(软切标记):超限时等下一个自然停顿再切,
        # 不在说话中途硬切。见 _listen_and_respond。
        self._segment_overflow = False

        # 依赖(从 config 注入)
        self._capture = config.capture
        self._vad = config.vad
        self._asr = config.asr
        self._tts = config.tts
        self._embedder: BaseSpeakerEmbedder | None = config.embedder
        self._wake_word = config.wake_word_detector
        self._player: BaseAudioPlayer | None = config.player

        # TTS 音色配置
        self._tts_voice: TTSVoice | None = config.tts_voice

        # 打断标志
        self._interrupt_event = asyncio.Event()

        # 播放锁
        self._speaking_lock = asyncio.Lock()

        # 🆕 Phase 7.6: 声纹采集模式
        self._enrollment_mode = False
        self._enrollment_audio: list[np.ndarray] = []  # 采集的音频片段
        self._enrollment_complete = asyncio.Event()
        # 🐛 2026-11-01: 采集完成后自动注册的钩子(由 voice_service 注入)。
        # 签名: async (segments: list[tuple[np.ndarray, int]]) -> None
        # VoiceAdapter 只负责"采到音频";"要不要注册/更新还是新建"由上层决定。
        self._on_enrollment_complete: Any = None
        # 🐛 2026-11-01: 采集启动时间戳,用于防抖(短时间内不重复触发采集)。
        self._enrollment_started_at: float = 0.0
        # 🐛 2026-11-01: 采集去抖窗口(秒)。同一设备在这个窗口内不重复触发。
        self._enrollment_debounce_seconds: float = 30.0
        # 🆕 2026-11-01: 采集超时(秒)。
        #
        # 必须有上限:否则用户不说话 / 不说满 3 句时,_enrollment_listen 的
        # async for 永不退出 → 主循环被永久卡死 → 整个 VoiceAdapter 失联。
        self._enrollment_timeout_seconds: float = 60.0

        # ══════════════════════════════════════════════════════════════════
        # 🆕 2026-11-01 (Phase 7.6.1): 同意机制(consent flow)
        # ══════════════════════════════════════════════════════════════════
        #
        # 改造前:陌生人 → 直接后台静默采集(用户完全不知情)。
        # 改造后:陌生人 → Agent 主动询问 → 用户明确同意 → 才采集。
        #
        # 本段负责**音频层**的事:
        #   - CONFIRMING 状态:开一个短窗口,听用户一句回答
        #   - 回声抑制:管家自己 TTS 播报期间不录音
        #   - 冷却:用户拒绝后当天不再询问
        #
        # 业务语义(同意与否、叫什么名字)由 Agent(LLM)判定,见 §3 工具侧。

        # 确认窗口时长(秒)。用户决策(2026-11-01):5 秒可行。
        self._confirm_window_seconds: float = 5.0

        # 🆕 拒绝冷却:用户明确拒绝后,**当天**不再询问。
        #
        # 为什么按"当天"而不是"1 小时":用户说"不用"通常意味着
        # "我现在不想被这个打扰",而不是"5 分钟后再问我就愿意了"。
        # 用户决策(2026-11-01):当天不询问。
        self._consent_cooldown_seconds: float = 24 * 3600.0

        # user_id → 下次可询问的时间戳(用户拒绝后写入)。
        # 空 dict = 从没拒绝过任何人。
        self._consent_cooldown: dict[str, float] = {}

        # 🔇 回声抑制:TTS 播放期间(及播放结束后一小段)不录音。
        #
        # 为什么必须有:mic_capture 是裸采集,没有 AEC。
        # 管家 TTS 播"请说 3 句话"时,麦克风会把**管家自己的声音**录进去,
        # 污染声纹 embedding,导致识别率下降。
        #
        # ⚠️ 门控的**实现在 player 层**(BaseAudioPlayer.play_gated),
        # 因为 Agent 的正常回复走 TTSOutputAdapter → player,和本类的
        # speak() 共用同一个 player。这里只是读它的状态。
        #
        # 播放结束后额外等待(秒),让扬声器尾音 + 房间反射散掉。
        self._playback_tail_seconds: float = 0.3
        # 播放结束后,过多久才开确认窗口(避免用户抢答被截断)。
        self._confirm_window_start_delay: float = 0.5

    # -------------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------------

    async def start(self) -> None:
        """启动 VoiceAdapter。

        启动音频采集和唤醒词检测。
        """
        logger.info("VoiceAdapter starting", extra={"device_id": self._cfg.device_id})
        # 🆕 预加载 VAD 模型(在后台线程,避免阻塞事件循环)
        if self._vad is not None:
            try:
                logger.info("VoiceAdapter 预加载 VAD 模型...")
                await self._vad.ensure_ready()
                logger.info("VAD 模型预加载完成")
            except Exception as e:
                logger.warning("VAD 模型预加载失败,将在第一次 is_speech() 时重试: %s", e)
        # 🆕 预加载 ASR 模型(在后台线程,避免阻塞事件循环)
        if self._asr is not None:
            try:
                logger.info("VoiceAdapter 预加载 ASR 模型...")
                await self._asr.ensure_ready()
                logger.info("ASR 模型预加载完成")
            except Exception as e:
                logger.warning("ASR 模型预加载失败,将在第一次 transcribe() 时重试: %s", e)
        if self._capture:
            await self._capture.start()
        logger.info("VoiceAdapter started")

    async def run(self) -> None:
        """主循环:持续监听麦克风,处理语音交互。

        阻塞直到 stop() 被调用。
        """
        self._running = True
        logger.info("VoiceAdapter run loop started")

        try:
            while self._running:
                await self._idle_loop()
        except asyncio.CancelledError:
            logger.info("VoiceAdapter run loop cancelled")
        finally:
            self._running = False

    async def stop(self) -> None:
        """停止 VoiceAdapter,释放资源。"""
        logger.info("VoiceAdapter stopping")
        self._running = False
        self._interrupt_event.set()  # 打断任何播放

        if self._capture:
            await self._capture.stop()
        if self._asr:
            await self._asr.close()
        if self._tts:
            await self._tts.close()
        if self._player:
            await self._player.close()
        if self._embedder:
            await self._embedder.close()
        if self._wake_word:
            await self._wake_word.close()

        logger.info("VoiceAdapter stopped")

    @property
    def state(self) -> str:
        """当前状态。"""
        return self._state

    # -------------------------------------------------------------------------
    # Internal:State Machine
    # -------------------------------------------------------------------------

    async def _idle_loop(self) -> None:
        """IDLE 状态:等待唤醒词或主动开始监听。"""
        # 🆕 2026-11-01 (Phase 7.6.1): 采集相关状态优先处理。
        #
        # 顺序很重要:采集/确认必须在"是否需要唤醒词"之前判断,
        # 因为这两个状态下我们**已经在对话中**,不该再要求用户说唤醒词。
        if self._enrollment_mode:
            self._state = self.State.ENROLLING
            await self._enrollment_listen()
            return

        if self._wake_word and not self._wake_word.is_awake():
            # 唤醒词模式:等待唤醒
            await self._wait_for_wake_word()
        else:
            # 无唤醒词模式:直接进入 LISTENING
            self._state = self.State.LISTENING
            await self._listen_and_respond()

    async def _wait_for_wake_word(self) -> None:
        """等待唤醒词触发。"""
        logger.info("👂 进入 _wait_for_wake_word, 调用 self._capture.stream()...")
        if not self._capture:
            logger.error("_capture 为空,无法等待唤醒词")
            return

        async for chunk in self._capture.stream():
            if not self._running:
                break

            # 检查唤醒词
            if self._wake_word:
                result = await self._wake_word.process_audio(chunk.audio)
                if result:
                    logger.info("Wake word detected", extra={"keyword": result.keyword})
                    await self._bus.publish(
                        VoiceEvent.make(
                            action=VoiceAction.WAKE_WORD,
                            device_id=self._cfg.device_id,
                            user_id=self._cfg.user_id,
                            wake_word=result.keyword,
                        )
                    )
                    # 唤醒后退出外层 stream,让主循环下次调 _listen_and_respond
                    self._state = self.State.LISTENING
                    return  # ← 关键:不要 break,避免嵌套 stream() 抢 queue

    async def _listen_and_respond(self) -> None:
        """LISTENING 状态:聆听用户说话,处理命令。

        🆕 Phase 7.6:若处于声纹采集模式(_enrollment_mode=True)，
        则静默收集音频片段，不走 ASR，不发布 VoiceEvent。
        """
        # 🆕 声纹采集模式:静默收集音频
        if self._enrollment_mode:
            await self._enrollment_listen()
            return

        logger.info("🎙️  LISTENING: 开始监听用户说话... (说点什么,1.5 秒静音后自动结束)")
        self._buffer.clear()
        self._buffer_ms = 0
        self._vad.reset()
        self._buffer.clear()
        self._buffer_ms = 0
        self._vad.reset()
        # 🆕 累计静默时长(用于判定"用户说完了")
        self._silence_ms = 0
        # 🐛 重置软切标记(新一轮监听从零开始)
        self._segment_overflow = False

        if not self._capture:
            return

        chunk_count = 0
        speech_chunks = 0
        last_status_log = 0
        logger.info("🎙️ LISTENING 准备 async for chunk in self._capture.stream()...")
        async for chunk in self._capture.stream():
            if not self._running:
                break

            # 🆕 2026-11-01 关键修复:采集模式可能在**本次监听期间**被开启。
            #
            # 场景:陌生人说话 → _flush_buffer 里判定"要采集" → 调用
            # start_voice_enrollment() → _enrollment_mode=True。
            #
            # 但 _enrollment_mode 的检查只在 _listen_and_respond 的**入口**,
            # 而入口检查发生在"进入函数时"。采集是在 async for 循环**内部**
            # 触发的 → 入口检查永远不会再跑 → _enrollment_listen 从没被调用
            # → 采集永远是空的(日志表现:开启采集后仍在打 "LISTENING 收到第 N 块")。
            #
            # 修法:检测到采集模式就**立刻退出**当前监听循环,让主循环回到
            # _idle_loop → _listen_and_respond → 这次走入口分支 → _enrollment_listen。
            if self._enrollment_mode:
                logger.info(
                    "🎙️ 采集模式已开启,退出当前监听循环,切换到声纹采集"
                )
                break

            is_speech = self._vad.is_speech(chunk.audio)
            chunk_count += 1
            if is_speech:
                speech_chunks += 1

            if chunk_count <= 3 or chunk_count % 50 == 0:
                rms = float(np.sqrt(np.mean(chunk.audio**2)))
                logger.info(
                    f"🎙️ LISTENING 收到第 {chunk_count} 块, is_speech={is_speech}, RMS={rms:.4f}"
                )

            # 每 50 块(~5秒)打一次状态,确认 loop 在跑
            if chunk_count - last_status_log >= 50:
                rms = float(np.sqrt(np.mean(chunk.audio**2)))
                logger.info(
                    f"📊 状态: 收 {chunk_count} 块, VAD 命中 {speech_chunks}/{chunk_count}, "
                    f"本块 RMS={rms:.4f}, buffer={self._buffer_ms:.0f}ms"
                )
                last_status_log = chunk_count

            if is_speech:
                self._buffer.append(chunk.audio)
                self._buffer_ms += len(chunk.audio) / self._cfg.sample_rate * 1000
                self._silence_ms = 0  # 🆕 重置静默计时
                # 🐛 2026-10-26 软上限:超过 max_segment 不再**立刻**硬切。
                #
                # 原来的逻辑是"buffer 超时就 flush",无条件切 —— 即使用户还在
                # 滔滔不绝地说,也会在阈值处把一句话劈成两半,产生两个碎片事件,
                # LLM 对每片各回一次 → 用户听到"同一句话被拆成两段分别回答"。
                #
                # 现在改成:超限时打个标记,等到**真正出现静默**时再在自然
                # 停顿处切一刀。这样长句会被完整保留,切点落在句末。
                #
                # 只有在"用户完全不再说话"(静默计时也走完)时才会落到
                # 下面的 else 分支正常 flush,不会无限增长。
                if self._buffer_ms >= self._cfg.max_segment_duration_ms:
                    if not self._segment_overflow:
                        logger.info(
                            "⏱️  达到单段上限 %.0fms, 将在下一个自然停顿处切分 "
                            "(不再硬切,避免把长句劈成两半)",
                            self._cfg.max_segment_duration_ms,
                        )
                    self._segment_overflow = True
            else:
                # 静音:buffer 有内容则累计静默时长
                if self._buffer:
                    # 累加静默时长(chunk 时长 = 100ms)
                    chunk_ms = len(chunk.audio) / self._cfg.sample_rate * 1000
                    self._silence_ms += chunk_ms
                    # 检查是否超时
                    if self._silence_ms >= self._cfg.silence_timeout_ms:
                        # 🐛 超长段:用户说太久还没停 → 在这里硬切(极端保护)
                        if self._segment_overflow:
                            logger.info(
                                "⏱️  超长段 %.0fms 且持续静音, 强制 flush (极端保护)",
                                self._buffer_ms,
                            )
                        else:
                            logger.info(
                                "🤫 静默 %.0fms 超过阈值, flush buffer (buffer=%.0fms)",
                                self._silence_ms,
                                self._buffer_ms,
                            )
                        await self._flush_buffer()
                        self._silence_ms = 0

            # 退出条件
            if chunk.is_final:
                if self._buffer:
                    await self._flush_buffer()
                break

        # 返回 IDLE
        self._state = self.State.IDLE

    async def _flush_buffer(self) -> None:
        """buffer 有内容时,送 ASR + 声纹识别,发布 VoiceEvent。"""
        if not self._buffer:
            return

        audio = np.concatenate(self._buffer)
        segment = AudioSegment(audio=audio, sample_rate=self._cfg.sample_rate)

        # ASR
        asr_result = ""
        if self._asr:
            try:
                result = await self._asr.transcribe(segment)
                asr_result = result.text
            except Exception as e:
                logger.error("ASR failed", exc_info=e)

        # 声纹识别(Phase 7.6+)
        speakers: list[SpeakerInfo] = []
        # 🆕 2026-11-01:本次发言对应的 user_id(声纹识别出的说话人)。
        # 默认用 config 的 user_id 作为兜底(声纹不可用时的降级路径)。
        effective_user_id = self._cfg.user_id
        # 🆕 2026-11-01:是否需要触发声纹采集(双阈值判定)。
        should_enroll = False
        enroll_reason = ""
        if self._embedder and asr_result:
            try:
                embedding = await self._embedder.embed(audio, self._cfg.sample_rate)
                if self._registry:
                    sp_info = await self._registry.identify_speaker_info(embedding)
                    speakers = [sp_info]
                    # 🐛 2026-11-01 加日志:识别结果是否成功挂到事件上。
                    # 这一层能确认"识别成功了,但事件里没带上 speaker"这类
                    # 中间环节丢失的问题。
                    logger.info(
                        "🗣️  声纹结果已挂载到事件: user_id=%s confidence=%.4f is_known=%s",
                        sp_info.user_id,
                        sp_info.confidence,
                        sp_info.is_known,
                    )

                    # ⭐ 2026-11-01 核心改动:event.user_id 改用**声纹识别出的说话人**。
                    #
                    # 之前是写死的 config.user_id(设备归属人),导致:
                    #   - 记忆里所有人的话都记成 "[user=gaotianyu]"
                    #   - prompt 里"你正在和【gaotianyu】对话"永远是错的
                    #   - 管家无法区分"谁在说话",也就无法做"爸爸说…妈妈说…以哪个为准"
                    #
                    # 现在:
                    #   - is_known=True  → 用注册的真实 user_id(speaker_xxx)
                    #   - is_known=False → 用陌生人临时 id(stranger_xxx),
                    #     同时后台触发采集,采集完成后就能认出真身
                    if sp_info.is_known:
                        effective_user_id = sp_info.user_id
                    else:
                        effective_user_id = self._build_stranger_id(embedding)
                        # 双阈值判定:该不该为这个人启动采集?
                        match = await self._registry.identify(embedding)
                        should_enroll, enroll_reason = (
                            self._registry.should_trigger_enrollment(
                                match, audio_rms=_rms(audio)
                            )
                        )
                else:
                    speakers = [
                        SpeakerInfo(
                            user_id="unknown",
                            confidence=0.0,
                            is_known=False,
                            embedding=embedding,
                        )
                    ]
                    # ⚠️ registry 缺失 → 永远识别不出身份
                    logger.warning(
                        "⚠️ 无声纹注册表(SpeakerRegistry 未注入)，跳过身份识别"
                    )
                    effective_user_id = self._build_stranger_id(embedding)
                    should_enroll = True
                    enroll_reason = "no_registry"
            except Exception as e:
                logger.warning("Speaker embedding failed", exc_info=e)
        elif not self._embedder:
            logger.warning("⚠️ 无声纹 embedder，跳过身份识别")
        elif not asr_result:
            logger.info("🔇 ASR 结果为空，跳过声纹识别")

        # 发布 VoiceEvent
        # 🆕 2026-11-01: user_id 传声纹识别出的说话人(不再是写死的设备归属人)
        #
        # 🆕 2026-11-01 (Phase 7.6.1): 传 suggest_enrollment / enrollment_reason,
        # 告诉 Agent"这个人是陌生人,可以问要不要录声纹"。
        #
        # ⚠️ 关键语义:这里**只标记,不采集**。
        # 改造前是直接 start_voice_enrollment()(后台静默,用户不知情)。
        # 改造后采集必须经过:用户明确同意 → LLM 调 enroll_speaker(confirm=True)。
        event = VoiceEvent.make(
            action=VoiceAction.COMMAND,
            device_id=self._cfg.device_id,
            user_id=effective_user_id,
            asr_text=asr_result,
            speakers=speakers,
            suggest_enrollment=should_enroll and not self._is_in_consent_cooldown(
                effective_user_id
            ),
            enrollment_reason=enroll_reason,
        )
        await self._bus.publish(event)

        # 清空 buffer
        self._buffer.clear()
        self._buffer_ms = 0
        # 🐛 重置软切标记:下一次超限重新计
        self._segment_overflow = False

        # 🆕 2026-11-01 (Phase 7.6.1): 陌生人 → **只标记建议**,不自动采集。
        #
        # 改造前(已废弃):
        #     if should_enroll:
        #         await self.start_voice_enrollment()   ← 后台静默采集,用户不知情
        #
        # 改造后:由 Agent(LLM)看到 suggest_enrollment=True 后决定要不要
        # 开口询问,用户明确同意才调 enroll_speaker(confirm=True) 启动采集。
        #
        # 这样既拿到"陌生人"的价值(能被记住),又拿到了知情同意。
        if should_enroll:
            if self._is_in_consent_cooldown(effective_user_id):
                logger.info(
                    "🎙️ 陌生人但处于冷却期,本次不询问登记声纹: user_id=%s",
                    effective_user_id,
                )
            else:
                logger.info(
                    "🎙️ 建议询问登记声纹: reason=%s user_id=%s（等待 Agent 询问 + 用户同意）",
                    enroll_reason,
                    effective_user_id,
                )

    @staticmethod
    def _build_stranger_id(embedding: list[float]) -> str:
        """构造陌生人临时 id(2026-11-01)。

        设计要点:
          - 前缀 ``stranger_`` 让 LLM / 日志一眼看出"这人还没登记"。
          - 后缀用声纹前 8 维的哈希 —— **只用于本轮会话内的临时指代**,
            不保证同一个人跨轮一致(声纹随内容变化,实测同一人不同句子
            会得到不同哈希)。真正的身份统一要等采集完成注册后。

        Args:
            embedding: 声纹向量。

        Returns:
            形如 ``stranger_a3f2b1c`` 的临时 id。
        """
        embed_key = bytes(str(list(embedding[:8])).encode())
        short_hash = hashlib.sha256(embed_key).hexdigest()[:7]
        return f"stranger_{short_hash}"

    # -------------------------------------------------------------------------
    # 🆕 Phase 7.6: 声纹采集模式实现
    # -------------------------------------------------------------------------

    async def _enrollment_listen(self) -> None:
        """声纹采集模式:静默收集音频片段，每句话独立存储。

        - VAD 检测到语音 → 放入 buffer
        - 静默超时(3秒) → 把当前 buffer 存为一个片段，清空 buffer
        - 采集到 3+ 片段后自动结束（stop_voice_enrollment 被调用时立即结束）

        🆕 2026-11-01:加了**超时兜底**。原来这里没有超时 —— 用户如果
        不说话或不说满 3 句,这个 async for 永远不退出,主循环被**永久卡死**
        (整个 VoiceAdapter 再也不响应任何东西)。

        超时后:用已采到的片段走注册流程(哪怕只有 1 段也比没有强),
        然后回到正常监听。
        """
        if not self._capture:
            return

        self._buffer.clear()
        self._buffer_ms = 0
        self._vad.reset()
        self._silence_ms = 0
        self._enrollment_audio.clear()
        chunk_count = 0

        MIN_SAMPLES_FOR_VALID_SEGMENT = 10  # 至少收到 10 个 chunk 才算有效片段

        # 🆕 采集超时:最多等 60 秒(用户读提示 + 说 3 句绰绰有余)
        deadline = time.monotonic() + self._enrollment_timeout_seconds

        async for chunk in self._capture.stream():
            if not self._enrollment_mode:
                # 被外部 stop_voice_enrollment() 中断，丢弃未完成的 buffer
                break

            # 🔇 2026-11-01 (Phase 7.6.1) 回声抑制。
            #
            # 采集开始前 Agent 会 TTS 播报"请说 3 句话"(用户决策:
            # 采集前播报一次、采集后播报一次,不做逐句播报)。
            # 那段播报声**绝不能**进声纹,所以播放期间/刚结束的音频一律丢弃。
            if self._is_playback_gated():
                continue

            # 🆕 超时兜底:不能无限等下去,否则主循环永久卡死
            if time.monotonic() > deadline:
                logger.warning(
                    "⏰ 声纹采集超时(%.0fs),用已采集的 %d 个片段结束",
                    self._enrollment_timeout_seconds,
                    len(self._enrollment_audio),
                )
                break

            chunk_count += 1
            is_speech = self._vad.is_speech(chunk.audio)

            if is_speech:
                self._buffer.append(chunk.audio)
                self._buffer_ms += len(chunk.audio) / self._cfg.sample_rate * 1000
                self._silence_ms = 0  # 重置静默计时
            else:
                # 静音
                if self._buffer:
                    chunk_ms = len(chunk.audio) / self._cfg.sample_rate * 1000
                    self._silence_ms += chunk_ms
                    # 3秒静默 → 保存当前片段
                    if self._silence_ms >= self._cfg.silence_timeout_ms:
                        # 🐛 2026-11-01 加日志:片段被丢弃时也要打日志。
                        # 之前只有"保存片段"有日志,被 MIN_SAMPLES_FOR_VALID_SEGMENT
                        # 过滤掉的片段**完全静默** —— 用户明明说了话,却收到
                        # "没有采集到有效音频",却不知道短在哪。
                        if len(self._buffer) >= MIN_SAMPLES_FOR_VALID_SEGMENT:
                            segment_audio = np.concatenate(self._buffer)
                            self._enrollment_audio.append(segment_audio)
                            logger.info(
                                "🎙️ 声纹采集: 保存第 %d 个片段, 时长=%.2fs, RMS=%.4f",
                                len(self._enrollment_audio),
                                self._buffer_ms / 1000,
                                _rms(segment_audio),
                            )
                        else:
                            logger.info(
                                "🎙️ 声纹采集: 丢弃过短片段 (时长=%.2fs < 最短 %d 个 chunk)",
                                self._buffer_ms / 1000,
                                MIN_SAMPLES_FOR_VALID_SEGMENT,
                            )
                        self._buffer.clear()
                        self._buffer_ms = 0
                        self._silence_ms = 0
                        # 采集到足够片段自动退出（3句）
                        if len(self._enrollment_audio) >= 3:
                            logger.info(
                                "🎙️ 声纹采集: 已采集 3 个片段，退出采集模式"
                            )
                            self._enrollment_mode = False
                            break

            # chunk.is_final 退出条件
            if chunk.is_final:
                # 保存未完成的 buffer（如果有）
                if self._buffer and len(self._buffer) >= MIN_SAMPLES_FOR_VALID_SEGMENT:
                    segment_audio = np.concatenate(self._buffer)
                    self._enrollment_audio.append(segment_audio)
                break

        # 退出时把剩余 buffer 也保存
        if self._buffer and len(self._buffer) >= MIN_SAMPLES_FOR_VALID_SEGMENT:
            segment_audio = np.concatenate(self._buffer)
            self._enrollment_audio.append(segment_audio)

        segments = [
            (audio, self._cfg.sample_rate) for audio in self._enrollment_audio
        ]
        logger.info(
            "🎙️ 声纹采集完成: 共采集 %d 个有效片段（总时长 %.2fs）",
            len(segments),
            sum(len(a) / self._cfg.sample_rate for a, _ in segments),
        )

        # 🐛 2026-11-01: 采集完成后自动触发注册回调(用户决策:"按A,自动注册")。
        #
        # 之前采集完就结束了 —— 音频丢在 _enrollment_audio 里没人用,
        # 必须靠 LLM 二次调用 confirm=True 才有人捡起。这是"自动注册"的关键一环。
        self._enrollment_audio.clear()
        self._enrollment_mode = False
        self._state = self.State.IDLE
        await self._fire_enrollment_complete(segments)

    # -------------------------------------------------------------------------
    # TTS Speak (由外部调用,如 AnswerRouter 响应时)
    # -------------------------------------------------------------------------

    async def speak(self, text: str, voice: TTSVoice | None = None) -> None:
        """合成并播放 TTS 音频(外部调用)。

        Args:
            text: 要播放的文本。
            voice: 音色配置;None=用默认音色。
        """
        if not self._tts:
            logger.warning("No TTS configured, skipping speak")
            return

        if not self._player:
            logger.warning("No audio player configured, skipping speak")
            return

        # 检查打断标志
        if self._interrupt_event.is_set():
            logger.debug("Interrupted before speaking, skipping")
            return

        effective_voice = voice or self._tts_voice

        self._state = self.State.SPEAKING
        logger.debug("TTS speaking", extra={"text": text[:50]})

        try:
            # 使用播放锁,确保同一时间只播放一段音频
            async with self._speaking_lock:
                # 🔇 2026-11-01 (Phase 7.6.1) 回声抑制:委托给 player 门控。
                #
                # 门控实现在 player 层(BaseAudioPlayer.play_gated),因为
                # Agent 的正常回复走 TTSOutputAdapter → player.play(),
                # 和这里的 VoiceAdapter.speak() **共用同一个 player 实例**。
                # 放在 player 才能覆盖两条路径。
                #
                # 生成 TTS 音频流
                audio_stream = self._tts.speak(text, voice=effective_voice)

                # 收集音频块用于播放
                # 注意:这里可以改为流式播放以减少延迟
                chunks: list[bytes] = []
                async for chunk in audio_stream:
                    # 检查打断
                    if self._interrupt_event.is_set():
                        logger.debug("TTS interrupted")
                        return
                    chunks.append(chunk)

                # 播放收集到的音频
                if chunks and not self._interrupt_event.is_set():
                    if self._player:
                        # 使用注入的 player
                        async def audio_iter():
                            for c in chunks:
                                yield c

                        await self._player.play_gated(audio_iter())
                    else:
                        # 无 player:只生成音频流(测试用)
                        logger.debug("No audio player configured, TTS audio discarded")

        except Exception as e:
            logger.error("TTS speak failed", exc_info=e)
        finally:
            self._state = self.State.IDLE

    async def interrupt(self) -> None:
        """打断当前 TTS 播放。

        由 AnswerRouter 或 voice.interrupt 事件调用。
        """
        logger.debug("Interrupting TTS")
        self._interrupt_event.set()
        # 短暂等待播放停止
        await asyncio.sleep(0.1)
        self._interrupt_event.clear()

    # -------------------------------------------------------------------------
    # 🆕 Phase 7.6.1: 声纹采集同意机制(2026-11-01)
    # -------------------------------------------------------------------------

    def _consent_cooldown_key(self, user_id: str) -> str:
        """计算冷却键(决定"这一天不再问谁")。

        ⚠️ 这里有个很容易踩的坑:**陌生人 id 是临时哈希**。

        ``_build_stranger_id()`` 用声纹前 8 维的哈希生成 ``stranger_xxx``,
        文档里写明"不保证同一个人跨轮一致" —— 同一个人说不同的话,
        声纹不同 → 哈希不同 → id 不同。

        如果直接拿 ``stranger_xxx`` 做冷却键,结果是:
            第 1 轮:用户拒绝,记下 ``stranger_a1b2c3d``
            第 2 轮:同一个用户说话,id 变成 ``stranger_e4f5g6h``
                    → 查不到冷却 → **又去问**
        也就是"拒绝后当天不询问"这条规则**完全失效**,而且失效得很隐蔽
        (日志里能看到冷却记录,但询问照旧)。

        所以:
        - **陌生人** → 按**设备**冷却。这台设备今天已经问过并被拒,
          就不再问任何人。
          (宁可少问一次,不可多问 —— 隐私优先。)
        - **已注册 user_id** → 按 user_id 冷却(重采集场景,精确到人)。
        """
        if not user_id:
            # 未指定 → 按设备冷却(这台设备今天不再问任何人)
            return f"device:{self._cfg.device_id}"
        if user_id.startswith("stranger_"):
            return f"device:{self._cfg.device_id}"
        return f"user:{user_id}"

    def _is_in_consent_cooldown(self, user_id: str) -> bool:
        """判断该 user_id 是否处于"拒绝冷却期"(当天不再询问)。

        Args:
            user_id: 说话人 id(陌生人临时 id 或已注册 user_id)。

        Returns:
            True = 冷却中,不应该再问。
        """
        key = self._consent_cooldown_key(user_id)
        if not key:
            return False
        until = self._consent_cooldown.get(key)
        if until is None:
            return False
        if time.monotonic() >= until:
            # 冷却已过期,清掉
            self._consent_cooldown.pop(key, None)
            return False
        return True

    def mark_consent_rejected(self, user_id: str) -> None:
        """标记用户**拒绝**录入声纹 → 当天不再询问。

        Args:
            user_id: 说话人 id。**留空**表示"按设备冷却"
                (这台设备今天不再问任何人)。
        """
        key = self._consent_cooldown_key(user_id)
        if not key:
            return
        self._consent_cooldown[key] = time.monotonic() + self._consent_cooldown_seconds
        logger.info(
            "🚫 用户拒绝录入声纹,%.0f 秒(当天)内不再询问: user_id=%s cooldown_key=%s",
            self._consent_cooldown_seconds,
            user_id or "(未指定)",
            key,
        )

    def clear_consent_cooldown(self, user_id: str) -> None:
        """清除冷却(用户后来同意了 / 重新采集时用)。"""
        self._consent_cooldown.pop(self._consent_cooldown_key(user_id), None)

    def _is_playback_gated(self) -> bool:
        """当前是否处于回声抑制门控期(正在播报 / 刚播完)。

        门控实现在 player 层(``BaseAudioPlayer.play_gated``)——
        因为 Agent 正常回复走 ``TTSOutputAdapter`` → player,
        和 ``VoiceAdapter.speak()`` 共用同一个 player 实例。

        没有 player 时返回 False(没在播报)。
        """
        player = self._player
        if player is None:
            return False
        gate_until = getattr(player, "playback_gate_until", None)
        if gate_until is None:
            return False
        return time.monotonic() < gate_until

    async def start_confirmation_window(
        self,
        *,
        user_id: str,
        timeout: float | None = None,
    ) -> str | None:
        """开启声纹采集**同意确认窗口**(Phase 7.6.1)。

        流程:

        1. 等 TTS 播报结束 + 一点延迟(避免用户抢答被截断)
        2. 监听一个短窗口(默认 5s),用 VAD 判断用户是否说话
        3. 把这一段音频送 ASR → 发 ``VoiceEvent(CONFIRM_ANSWER)`` 给 Agent 判定
        4. 返回识别到的文本(供同步调用方参考);超时/无语音返回 None

        ⚠️ 这一段音频**不会**进入声纹采集 —— 它只是"要"/"不要"这种
        单音节确认词,没有声纹价值(见设计文档 §3.3)。

        Args:
            user_id: 本次询问对应的说话人 id(用于事件关联 + 冷却)。
            timeout: 窗口时长(秒),None = 用默认 5s。

        Returns:
            ASR 识别到的文本;没听到 / 超时返回 None。
        """
        if not self._capture:
            logger.error("_capture 为空,无法开启确认窗口")
            return None

        window = timeout if timeout is not None else self._confirm_window_seconds
        self._state = self.State.CONFIRMING

        # 等 TTS 播完 + 防抢答延迟
        #
        # 门控在 player 层,所以这里读 player 的截止时间:
        #   还要等的时间 = max(门控剩余, 防抢答延迟)
        delay = self._confirm_window_start_delay
        gate_remaining = 0.0
        player = self._player
        if player is not None:
            gate_remaining = max(
                0.0, getattr(player, "playback_gate_until", 0.0) - time.monotonic()
            )
        remaining = max(gate_remaining, delay)
        if remaining > 0:
            logger.info("⏳ 等待 TTS 结束 + %.1fs 防抢答延迟...", remaining)
            await asyncio.sleep(remaining)

        logger.info(
            "🎤 确认窗口开启(%.1fs),等待用户回答是否录入声纹...",
            window,
        )

        deadline = time.monotonic() + window
        self._buffer.clear()
        self._buffer_ms = 0
        self._silence_ms = 0
        self._vad.reset()

        min_samples = 3  # 至少 3 块(300ms)才算有效语音

        async for chunk in self._capture.stream():
            if not self._running:
                break

            # 🔇 回声抑制:TTS 播放期间/刚结束的音频一律丢弃
            if self._is_playback_gated():
                continue

            if time.monotonic() > deadline:
                logger.info("⏰ 确认窗口超时(%.1fs),视为用户未同意", window)
                break

            if not self._vad.is_speech(chunk.audio):
                continue

            self._buffer.append(chunk.audio)
            self._buffer_ms += len(chunk.audio) / self._cfg.sample_rate * 1000

            if len(self._buffer) < min_samples:
                continue

            # 收集到一段语音 → 送 ASR
            audio = np.concatenate(self._buffer)
            self._buffer.clear()
            self._buffer_ms = 0

            asr_text = ""
            if self._asr:
                try:
                    result = await self._asr.transcribe(audio)
                    asr_text = (result.text or "").strip()
                except Exception as e:  # noqa: BLE001
                    logger.warning("确认窗口 ASR 失败: %s", e)

            if not asr_text:
                logger.info("🔇 确认窗口未识别到有效文本,继续等待")
                continue

            logger.info("🎤 CONFIRM_ANSWER: asr_text=%r user_id=%s", asr_text, user_id)

            # 发事件给 Agent 判定同意/拒绝
            await self._bus.publish(
                VoiceEvent.make(
                    action=VoiceAction.CONFIRM_ANSWER,
                    device_id=self._cfg.device_id,
                    user_id=user_id,
                    asr_text=asr_text,
                    audio_duration_ms=int(len(audio) / self._cfg.sample_rate * 1000),
                )
            )

            self._state = self.State.IDLE
            return asr_text

        self._state = self.State.IDLE
        return None

    # -------------------------------------------------------------------------
    # 🆕 Phase 7.6: 声纹采集模式
    # -------------------------------------------------------------------------

    async def start_voice_enrollment(self) -> None:
        """进入声纹采集模式。

        暂停正常监听，进入专注采集状态。
        采集过程中音频会缓存到 _enrollment_audio，不会发布 VoiceEvent。

        🐛 2026-11-01 修复:原来这里会 ``self._running = False``,
        而 ``_running`` 是**主循环的开关**:

            while self._running:
                await self._idle_loop()

        置 False 后主循环立刻退出 → ``_idle_loop`` 不再被调 →
        ``_listen_and_respond`` 不再被调 → ``_enrollment_listen``
        **永远执行不到**,采集永远是空的。

        原意应该是"停掉当前这轮监听",但实际把整个 adapter 干掉了。
        现在只切 ``_enrollment_mode``,主循环下一轮自然走进采集分支。
        """
        # 防抖:短时间内不重复触发采集(避免反复打断用户)
        now = time.monotonic()
        if (
            self._enrollment_started_at
            and now - self._enrollment_started_at < self._enrollment_debounce_seconds
        ):
            logger.info(
                "🎙️ 声纹采集被去抖跳过 (距上次 %.1fs < %.1fs)",
                now - self._enrollment_started_at,
                self._enrollment_debounce_seconds,
            )
            return

        logger.info(
            "🎙️ VoiceAdapter 进入声纹采集模式（静默采集中，不发布 VoiceEvent）"
        )
        self._enrollment_mode = True
        self._enrollment_audio.clear()
        self._enrollment_complete.clear()
        self._enrollment_started_at = now
        # ⚠️ 不再置 _running=False —— 那会杀掉主循环,导致采集永不执行。
        # 主循环下一轮 _idle_loop → _listen_and_respond 会看到
        # _enrollment_mode=True 而走 _enrollment_listen 分支。

    def set_enrollment_callback(
        self, callback: Any | None
    ) -> None:
        """注入"采集完成"回调(2026-11-01)。

        Args:
            callback: ``async (segments) -> None``。在采集完成后被调用,
                由上层负责注册/更新声纹(VoiceAdapter 不碰 registry 业务逻辑)。
        """
        self._on_enrollment_complete = callback

    async def stop_voice_enrollment(self) -> list[tuple[np.ndarray, int]]:
        """退出声纹采集模式，返回采集到的音频片段。

        Returns:
            list of (audio, sample_rate) tuples。每句独立采集为一个片段。
        """
        logger.info(
            "🎙️ VoiceAdapter 退出声纹采集模式，采集到 %d 个音频片段（总时长 %.2fs）",
            len(self._enrollment_audio),
            sum(len(a) / self._cfg.sample_rate for a in self._enrollment_audio),
        )
        self._enrollment_mode = False
        # 返回 (audio_array, sample_rate) 元组列表
        result = [(audio, self._cfg.sample_rate) for audio in self._enrollment_audio]
        self._enrollment_audio.clear()
        return result

    async def _fire_enrollment_complete(
        self, segments: list[tuple[np.ndarray, int]]
    ) -> None:
        """采集完成后触发回调(2026-11-01)。

        由 ``_enrollment_listen`` 在采集到足够片段后调用。
        回调异常**不能影响主循环** —— 采集失败只是这次没记住声音,
        不能让整个 VoiceAdapter 挂掉。
        """
        self._enrollment_complete.set()
        if self._on_enrollment_complete is None:
            logger.info("🎙️ 采集完成（未注册回调，跳过自动注册）")
            return
        try:
            await self._on_enrollment_complete(segments)
        except Exception as e:  # noqa: BLE001
            logger.error("❌ 采集完成回调执行失败: %s", e, exc_info=True)

    def __repr__(self) -> str:
        return f"VoiceAdapter(device_id={self._cfg.device_id}, state={self._state})"


__all__ = ["VoiceAdapter", "VoiceAdapterConfig"]
