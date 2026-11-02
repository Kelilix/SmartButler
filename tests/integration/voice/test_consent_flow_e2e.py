"""Phase 7.6.1 同意流程 **端到端** 集成测试。

跑通完整链路:

    陌生人说话
      → VoiceEvent(suggest_enrollment=True)
      → LLM 看到提示,决定询问
      → TTS 播报询问
      → CONFIRMING 窗口(5s)捕获用户回答
      → VoiceEvent(CONFIRM_ANSWER)
      → LLM 判定同意 → 调 enroll_speaker(confirm=True)
      → 采集 → 注册 → 播报"记住了"

用 Fake LLM / Fake TTS / Fake Capture,不依赖真实硬件和 API key。
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from typing import Any

import numpy as np
import pytest

from smartbutler.events.core.event import SpeakerInfo
from smartbutler.events.protocol.voice_event import VoiceAction, VoiceEvent
from smartbutler.voice.adapter.config import VoiceAdapterConfig
from smartbutler.voice.adapter.voice_adapter import VoiceAdapter


class FakeBus:
    def __init__(self) -> None:
        self.events: list[VoiceEvent] = []

    async def publish(self, event: VoiceEvent, *, topic: str | None = None) -> None:
        self.events.append(event)

    def of_action(self, action: VoiceAction) -> list[VoiceEvent]:
        return [e for e in self.events if e.action == action]


class FakeChunk:
    def __init__(self, audio: np.ndarray) -> None:
        self.audio = audio


class ScriptedCapture:
    """按脚本产出音频块。

    ``script`` 是一串 RMS 值,每个代表 100ms。
    """

    def __init__(self, script: list[float]) -> None:
        self.script = script
        self.consumed = 0

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def stream(self) -> AsyncIterator[FakeChunk]:
        for rms in self.script:
            self.consumed += 1
            yield FakeChunk(np.full(1600, rms, dtype=np.float32))


class FakeVAD:
    def __init__(self, threshold: float = 0.02) -> None:
        self.threshold = threshold

    def reset(self) -> None:
        pass

    def is_speech(self, audio: np.ndarray) -> bool:
        rms = float(np.sqrt(np.mean(np.asarray(audio, dtype=np.float32) ** 2)))
        return rms > self.threshold


class ScriptedASR:
    """按调用顺序返回不同文本(第一次普通对话,第二次确认回答)。"""

    def __init__(self, texts: list[str]) -> None:
        self.texts = list(texts)
        self.calls = 0

    async def transcribe(self, segment: Any):
        text = self.texts[self.calls] if self.calls < len(self.texts) else ""
        self.calls += 1
        return type("R", (), {"text": text})()


class RecordingPlayer:
    """记录播放内容,并实现门控语义。"""

    def __init__(self, tail: float = 0.05) -> None:
        self._tail = tail
        self._gate_until = 0.0
        self.spoken: list[str] = []

    @property
    def playback_gate_until(self) -> float:
        return self._gate_until

    async def play(self, stream: AsyncIterator[bytes]) -> None:
        async for _ in stream:
            pass

    async def play_gated(self, stream: AsyncIterator[bytes]) -> None:
        self._gate_until = time.monotonic() + self._tail
        try:
            await self.play(stream)
        finally:
            self._gate_until = time.monotonic() + self._tail

    async def close(self) -> None:
        pass


class FakeEmbedder:
    async def embed(self, audio: np.ndarray, sample_rate: int = 16000) -> list[float]:
        return [0.1] * 256


class StrangerRegistry:
    """永远匹配不上 → 一直当陌生人。"""

    async def identify_speaker_info(self, embedding: list[float]) -> SpeakerInfo:
        return SpeakerInfo(
            user_id="unknown:abc",
            confidence=0.0,
            is_known=False,
            embedding=embedding,
        )

    async def identify(self, embedding: list[float]):
        return type("M", (), {"is_known": False, "matched_user_id": None, "candidates": []})()

    def should_trigger_enrollment(self, match, *, audio_rms=None) -> tuple[bool, str]:
        return True, "stranger"


@pytest.mark.asyncio
async def test_full_consent_flow_end_to_end() -> None:
    """⭐ 完整链路:陌生人 → 建议询问 → 确认窗口 → 同意 → 采集。"""
    bus = FakeBus()
    # 脚本:前 3 块是用户说话(触发 COMMAND),后面是确认窗口的回答
    capture = ScriptedCapture([0.3, 0.3, 0.3, 0.3, 0.0, 0.0, 0.3, 0.3, 0.3, 0.3])
    asr = ScriptedASR(["你好呀", "要"])
    player = RecordingPlayer()

    adapter = VoiceAdapter(
        config=VoiceAdapterConfig(device_id="spk", user_id="owner"),
        event_bus=bus,  # type: ignore[arg-type]
    )
    adapter._capture = capture  # type: ignore[assignment]
    adapter._vad = FakeVAD()  # type: ignore[assignment]
    adapter._asr = asr  # type: ignore[assignment]
    adapter._player = player  # type: ignore[assignment]
    adapter._embedder = FakeEmbedder()  # type: ignore[assignment]
    adapter._registry = StrangerRegistry()  # type: ignore[assignment]
    adapter._running = True
    adapter._confirm_window_seconds = 1.0
    adapter._confirm_window_start_delay = 0.0

    # ---- 步骤 1:陌生人说话 → COMMAND 事件带 suggest_enrollment ----
    adapter._buffer = [np.full(1600, 0.3, dtype=np.float32)]
    adapter._buffer_ms = 100
    await adapter._flush_buffer()

    commands = bus.of_action(VoiceAction.COMMAND)
    assert len(commands) == 1
    assert commands[0].suggest_enrollment is True
    assert commands[0].enrollment_reason == "stranger"
    # 关键:此时**没有**启动采集
    assert adapter._enrollment_mode is False

    # LLM 看到的输入里有"可以问"的提示
    llm_input = commands[0].to_user_input()
    assert "可询问登记声纹" in llm_input

    # ---- 步骤 2:LLM 决定询问 → TTS 播报 ----
    await adapter.speak("方便告诉我你的名字,让我记住你的声音吗?")
    assert player.spoken == []  # speak() 用的是 tts,这里没配 tts,不播

    # ---- 步骤 3:开启确认窗口 → 用户说"要" ----
    answer = await adapter.start_confirmation_window(
        user_id=commands[0].user_id,
    )
    assert answer == "要"

    # ---- 步骤 4:CONFIRM_ANSWER 事件 → LLM 判定 → 调工具(confirm=True) ----
    confirms = bus.of_action(VoiceAction.CONFIRM_ANSWER)
    assert len(confirms) == 1
    assert confirms[0].asr_text == "要"
    confirm_input = confirms[0].to_user_input()
    assert "声纹采集确认" in confirm_input
    assert "confirm=true" in confirm_input

    # LLM 据此调工具 → 采集启动
    import importlib

    enroll_mod = importlib.import_module(
        "smartbutler.capabilities.tools.voice.enroll_speaker"
    )

    registry = StrangerRegistry()
    started: list[str] = []

    class SpyAdapter:
        _enrollment_mode = False

        async def start_voice_enrollment(self) -> None:
            started.append("yes")
            self._enrollment_mode = True

    spy = SpyAdapter()
    saved_a, saved_r = enroll_mod._voice_adapter, enroll_mod._speaker_registry
    saved_c = list(enroll_mod._pending_contexts)
    enroll_mod._voice_adapter = spy  # type: ignore[assignment]
    enroll_mod._speaker_registry = registry  # type: ignore[assignment]
    enroll_mod._pending_contexts.clear()
    try:
        # LLM 传的参数:名字 + confirm=true
        result = await enroll_mod.enroll_speaker(display_name="小明", confirm=True)
        assert result["status"] == "collecting"
        assert started == ["yes"]
        # 上下文已入栈,供后台回调取用
        assert enroll_mod.peek_pending_context()["display_name"] == "小明"
    finally:
        enroll_mod._voice_adapter = saved_a
        enroll_mod._speaker_registry = saved_r
        enroll_mod._pending_contexts.clear()
        enroll_mod._pending_contexts.extend(saved_c)


@pytest.mark.asyncio
async def test_rejection_puts_device_in_cooldown() -> None:
    """⭐ 用户拒绝 → 当天不再询问。"""
    bus = FakeBus()
    adapter = VoiceAdapter(
        config=VoiceAdapterConfig(device_id="spk", user_id="owner"),
        event_bus=bus,  # type: ignore[arg-type]
    )
    adapter._running = True

    # 用户拒绝 —— 通过 decline_speaker_enrollment 工具记录
    import importlib

    mod = importlib.import_module("smartbutler.capabilities.tools.voice.enroll_speaker")
    saved_a = mod._voice_adapter
    mod._voice_adapter = adapter  # type: ignore[assignment]
    try:
        result = await mod.decline_speaker_enrollment(
            user_id="stranger_abc", reason="说不用"
        )
        assert result["status"] == "declined"
    finally:
        mod._voice_adapter = saved_a

    assert adapter._is_in_consent_cooldown("stranger_abc")

    # 下一轮陌生人事件 → 不再建议询问
    adapter._embedder = FakeEmbedder()  # type: ignore[assignment]
    adapter._registry = StrangerRegistry()  # type: ignore[assignment]
    adapter._asr = ScriptedASR(["你好"])  # type: ignore[assignment]
    adapter._buffer = [np.full(1600, 0.3, dtype=np.float32)]
    adapter._buffer_ms = 100

    await adapter._flush_buffer()

    commands = bus.of_action(VoiceAction.COMMAND)
    assert commands[0].suggest_enrollment is False, "拒绝后当天还在问"
    assert commands[0].enrollment_reason == "stranger"  # 原因仍记录,便于观测


@pytest.mark.asyncio
async def test_tool_without_confirm_is_rejected_end_to_end() -> None:
    """⭐ LLM 忘记传 confirm=True → 不采集。"""
    import importlib

    mod = importlib.import_module("smartbutler.capabilities.tools.voice.enroll_speaker")

    class SpyAdapter:
        _enrollment_mode = False

        def __init__(self) -> None:
            self.started = 0

        async def start_voice_enrollment(self) -> None:
            self.started += 1

    spy = SpyAdapter()
    saved_a, saved_r = mod._voice_adapter, mod._speaker_registry
    saved_c = list(mod._pending_contexts)
    mod._voice_adapter = spy  # type: ignore[assignment]
    mod._speaker_registry = StrangerRegistry()  # type: ignore[assignment]
    mod._pending_contexts.clear()
    try:
        result = await mod.enroll_speaker(display_name="小明")  # 忘记 confirm
        assert result["status"] == "consent_required"
        assert spy.started == 0
    finally:
        mod._voice_adapter = saved_a
        mod._speaker_registry = saved_r
        mod._pending_contexts.clear()
        mod._pending_contexts.extend(saved_c)


@pytest.mark.asyncio
async def test_playback_gate_protects_enrollment_audio() -> None:
    """⭐ 采集期间管家播报 → 播报声不进声纹。"""
    bus = FakeBus()
    player = RecordingPlayer(tail=0.2)
    adapter = VoiceAdapter(
        config=VoiceAdapterConfig(device_id="spk", user_id="owner"),
        event_bus=bus,  # type: ignore[arg-type]
    )
    adapter._capture = ScriptedCapture([0.3] * 5)  # type: ignore[assignment]
    adapter._vad = FakeVAD()  # type: ignore[assignment]
    adapter._player = player  # type: ignore[assignment]
    adapter._running = True
    adapter._enrollment_mode = True
    adapter._enrollment_timeout_seconds = 0.2

    # 模拟 TTS 正在播报
    player._gate_until = time.monotonic() + 0.5

    await adapter._enrollment_listen()

    assert adapter._enrollment_audio == [], "管家的播报声进了声纹采集"


@pytest.mark.asyncio
async def test_confirm_window_does_not_leak_into_enrollment() -> None:
    """⭐ 确认窗口的"要"不会污染声纹。"""
    bus = FakeBus()
    capture = ScriptedCapture([0.3] * 5)
    adapter = VoiceAdapter(
        config=VoiceAdapterConfig(device_id="spk", user_id="owner"),
        event_bus=bus,  # type: ignore[arg-type]
    )
    adapter._capture = capture  # type: ignore[assignment]
    adapter._vad = FakeVAD()  # type: ignore[assignment]
    adapter._asr = ScriptedASR(["要"])  # type: ignore[assignment]
    adapter._player = RecordingPlayer()  # type: ignore[assignment]
    adapter._running = True
    adapter._confirm_window_start_delay = 0.0

    await adapter.start_confirmation_window(user_id="stranger_x")

    assert adapter._enrollment_audio == []
    assert adapter._enrollment_mode is False
    assert len(bus.of_action(VoiceAction.CONFIRM_ANSWER)) == 1
