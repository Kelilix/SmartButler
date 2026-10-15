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
import logging
from typing import TYPE_CHECKING, AsyncIterator

import numpy as np

from smartbutler.events.core.event import SpeakerInfo
from smartbutler.events.core.event_bus import EventBus
from smartbutler.events.protocol.voice_event import VoiceAction, VoiceEvent
from smartbutler.voice.adapter.config import VoiceAdapterConfig
from smartbutler.voice.capture.base import AudioChunk, AudioSegment
from smartbutler.voice.player.base import BaseAudioPlayer
from smartbutler.voice.speaker.base import BaseSpeakerEmbedder
from smartbutler.voice.tts.base import BaseTTS, TTSVoice

if TYPE_CHECKING:
    from smartbutler.events.routing.speaker_registry import SpeakerRegistry

logger = logging.getLogger(__name__)


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

    # -------------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------------

    async def start(self) -> None:
        """启动 VoiceAdapter。

        启动音频采集和唤醒词检测。
        """
        logger.info("VoiceAdapter starting", extra={"device_id": self._cfg.device_id})
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
        if self._wake_word and not self._wake_word.is_awake():
            # 唤醒词模式:等待唤醒
            await self._wait_for_wake_word()
        else:
            # 无唤醒词模式:直接进入 LISTENING
            self._state = self.State.LISTENING
            await self._listen_and_respond()

    async def _wait_for_wake_word(self) -> None:
        """等待唤醒词触发。"""
        logger.debug("Waiting for wake word...")
        if not self._capture:
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
                    # 唤醒后进入聆听
                    self._state = self.State.LISTENING
                    await self._listen_and_respond()
                    break

    async def _listen_and_respond(self) -> None:
        """LISTENING 状态:聆听用户说话,处理命令。"""
        logger.debug("Listening for command...")
        self._buffer.clear()
        self._buffer_ms = 0
        self._vad.reset()

        if not self._capture:
            return

        async for chunk in self._capture.stream():
            if not self._running:
                break

            is_speech = self._vad.is_speech(chunk.audio)

            if is_speech:
                self._buffer.append(chunk.audio)
                self._buffer_ms += len(chunk.audio) / self._cfg.sample_rate * 1000

                # 超时强制送 ASR
                if self._buffer_ms >= self._cfg.max_segment_duration_ms:
                    logger.debug("Max segment duration reached, flushing")
                    await self._flush_buffer()
            else:
                # 静音:buffer 有内容则等待说完
                if self._buffer:
                    # 等待继续说话或超时
                    await asyncio.sleep(0.1)
                    # 检查是否超时
                    if self._buffer_ms >= self._cfg.silence_timeout_ms:
                        logger.debug("Silence timeout, flushing")
                        await self._flush_buffer()

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
        if self._embedder and asr_result:
            try:
                embedding = await self._embedder.embed(audio, self._cfg.sample_rate)
                if self._registry:
                    sp_info = await self._registry.identify_speaker_info(embedding)
                    speakers = [sp_info]
                else:
                    speakers = [
                        SpeakerInfo(
                            user_id="unknown",
                            confidence=0.0,
                            is_known=False,
                            embedding=embedding,
                        )
                    ]
            except Exception as e:
                logger.warning("Speaker embedding failed", exc_info=e)

        # 发布 VoiceEvent
        event = VoiceEvent.make(
            action=VoiceAction.COMMAND,
            device_id=self._cfg.device_id,
            user_id=self._cfg.user_id,
            asr_text=asr_result,
            speakers=speakers,
        )
        await self._bus.publish(event)

        # 清空 buffer
        self._buffer.clear()
        self._buffer_ms = 0

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

                        await self._player.play(audio_iter())
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

    def __repr__(self) -> str:
        return f"VoiceAdapter(device_id={self._cfg.device_id}, state={self._state})"


__all__ = ["VoiceAdapter", "VoiceAdapterConfig"]
