"""Phase 7.6.1 声纹采集同意机制 单元测试。

覆盖设计文档 `docs/PHASE_7_6_1_CONSENT_FLOW_DESIGN.md` 的核心不变量:

1. `enroll_speaker` 的 **confirm 硬门槛** —— 未明确同意就不采集
2. `VoiceAdapter` 的 **CONFIRMING 窗口** —— 5s 内收到回答就发 CONFIRM_ANSWER
3. 确认窗口的音频 **不进** 声纹采集
4. **回声抑制** —— 播放期间麦克风音频被丢弃
5. **拒绝冷却** —— 当天不再询问
6. **陌生人只标记不采集** —— `_flush_buffer` 不再自动启动采集
7. `low_confidence` 标记 —— 单句样本建档时打标
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator

import numpy as np
import pytest

from smartbutler.events.core.event import SpeakerInfo
from smartbutler.events.protocol.voice_event import VoiceAction, VoiceEvent
from smartbutler.voice.adapter.config import VoiceAdapterConfig
from smartbutler.voice.adapter.voice_adapter import VoiceAdapter

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeBus:
    """收集发布的事件。"""

    def __init__(self) -> None:
        self.events: list[VoiceEvent] = []

    async def publish(self, event: VoiceEvent, *, topic: str | None = None) -> None:
        self.events.append(event)

    def of_action(self, action: VoiceAction) -> list[VoiceEvent]:
        return [e for e in self.events if e.action == action]


class FakeChunk:
    def __init__(self, audio: np.ndarray, is_final: bool = False) -> None:
        self.audio = audio
        self.is_final = is_final


class FakeCapture:
    """假麦克风:每次 stream() 产出有限块后结束。"""

    def __init__(self, chunks: list[np.ndarray] | None = None) -> None:
        self._chunks = chunks or []
        self.gen_count = 0

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def stream(self) -> AsyncIterator[FakeChunk]:
        self.gen_count += 1
        for c in self._chunks:
            yield FakeChunk(c)


class FakeVAD:
    """按 RMS 阈值判语音。"""

    def __init__(self, threshold: float = 0.02) -> None:
        self.threshold = threshold

    def reset(self) -> None:
        pass

    def is_speech(self, audio: np.ndarray) -> bool:
        rms = float(np.sqrt(np.mean(np.asarray(audio, dtype=np.float32) ** 2)))
        return rms > self.threshold


class FakeASR:
    """假 ASR:接口是 asr.transcribe(audio) → 结果对象.text。"""

    def __init__(self, text: str = "要") -> None:
        self.text = text

    async def transcribe(self, audio: np.ndarray):
        return type("R", (), {"text": self.text})()

    async def close(self) -> None:
        pass


class FakePlayer:
    """假播放器:实现 play_gated 门控语义(与真 player 一致)。"""

    def __init__(self, tail: float = 0.3) -> None:
        self._tail = tail
        self._gate_until = 0.0
        self.played: list[str] = []

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


def _audio(rms: float, samples: int = 1600) -> np.ndarray:
    return np.full(samples, rms, dtype=np.float32)


def _make_adapter(
    *,
    capture: FakeCapture | None = None,
    player: FakePlayer | None = None,
    asr: FakeASR | None = None,
) -> tuple[VoiceAdapter, FakeBus]:
    bus = FakeBus()
    adapter = VoiceAdapter(
        config=VoiceAdapterConfig(device_id="spk", user_id="owner"),
        event_bus=bus,  # type: ignore[arg-type]
    )
    adapter._capture = capture or FakeCapture()  # type: ignore[assignment]
    adapter._vad = FakeVAD()  # type: ignore[assignment]
    adapter._asr = asr or FakeASR()  # type: ignore[assignment]
    adapter._player = player or FakePlayer()  # type: ignore[assignment]
    adapter._running = True
    return adapter, bus


# ---------------------------------------------------------------------------
# 1. enroll_speaker 的 confirm 硬门槛
# ---------------------------------------------------------------------------


class TestEnrollSpeakerConsentGate:
    """Tool 层:没有明确同意就不许采集。"""

    @pytest.fixture(autouse=True)
    def _setup(self):
        # ⚠️ `from ... import enroll_speaker as mod` 拿到的是**函数**,
        # 不是模块(包 __init__ 把它导出成了函数)。
        # 要拿模块必须用 importlib。
        import importlib

        self.mod = importlib.import_module(
            "smartbutler.capabilities.tools.voice.enroll_speaker"
        )
        self._saved_adapter = self.mod._voice_adapter
        self._saved_registry = self.mod._speaker_registry
        self._saved_contexts = list(self.mod._pending_contexts)

        class DummyRegistry:
            pass

        class DummyAdapter:
            _enrollment_mode = False

            def __init__(self) -> None:
                self.started = 0

            async def start_voice_enrollment(self) -> None:
                self.started += 1
                self._enrollment_mode = True

        self.adapter = DummyAdapter()
        self.mod._voice_adapter = self.adapter  # type: ignore[assignment]
        self.mod._speaker_registry = DummyRegistry()  # type: ignore[assignment]
        self.mod._pending_contexts.clear()
        yield
        self.mod._voice_adapter = self._saved_adapter
        self.mod._speaker_registry = self._saved_registry
        self.mod._pending_contexts.clear()
        self.mod._pending_contexts.extend(self._saved_contexts)

    @pytest.mark.asyncio
    async def test_without_confirm_does_not_start_capture(self) -> None:
        """⭐ confirm=False → 不启动采集。"""
        result = await self.mod.enroll_speaker(display_name="小明")
        assert result["status"] == "consent_required"
        assert self.adapter.started == 0, "未同意却启动了采集"

    @pytest.mark.asyncio
    async def test_with_confirm_starts_capture(self) -> None:
        """confirm=True → 启动采集。"""
        result = await self.mod.enroll_speaker(display_name="小明", confirm=True)
        assert result["status"] == "collecting"
        assert self.adapter.started == 1

    @pytest.mark.asyncio
    async def test_busy_when_enrollment_in_progress(self) -> None:
        """上一次采集没结束 → 拒绝新请求。"""
        self.adapter._enrollment_mode = True
        result = await self.mod.enroll_speaker(display_name="小明", confirm=True)
        assert result["status"] == "busy"
        assert self.adapter.started == 0

    @pytest.mark.asyncio
    async def test_empty_display_name_rejected(self) -> None:
        result = await self.mod.enroll_speaker(display_name="  ", confirm=True)
        assert result["status"] == "error"

    @pytest.mark.asyncio
    async def test_pending_context_is_stack(self) -> None:
        """上下文是栈 —— 连续两次采集不会互相冲掉。"""
        self.mod.set_pending_context(display_name="甲")
        self.mod.set_pending_context(display_name="乙")
        first = self.mod.take_pending_context()
        second = self.mod.take_pending_context()
        assert first["display_name"] == "甲"
        assert second["display_name"] == "乙"
        assert self.mod.take_pending_context() == {}


# ---------------------------------------------------------------------------
# 2. CONFIRMING 窗口
# ---------------------------------------------------------------------------


class TestConfirmationWindow:
    """VoiceAdapter 层:确认窗口的行为。"""

    @pytest.mark.asyncio
    async def test_speech_triggers_confirm_answer_event(self) -> None:
        """⭐ 窗口内听到语音 → 发 CONFIRM_ANSWER 事件。"""
        chunks = [_audio(0.3) for _ in range(5)]
        adapter, bus = _make_adapter(capture=FakeCapture(chunks), asr=FakeASR("要"))

        result = await adapter.start_confirmation_window(user_id="stranger_x")

        assert result == "要"
        events = bus.of_action(VoiceAction.CONFIRM_ANSWER)
        assert len(events) == 1
        assert events[0].asr_text == "要"
        assert events[0].user_id == "stranger_x"

    @pytest.mark.asyncio
    async def test_timeout_returns_none(self) -> None:
        """窗口内没说话 → 返回 None(视为未同意)。"""
        adapter, bus = _make_adapter(capture=FakeCapture([_audio(0.001) for _ in range(5)]))
        adapter._confirm_window_seconds = 0.2

        result = await adapter.start_confirmation_window(user_id="stranger_x")

        assert result is None
        assert bus.of_action(VoiceAction.CONFIRM_ANSWER) == []

    @pytest.mark.asyncio
    async def test_confirm_audio_not_recorded_as_enrollment(self) -> None:
        """⭐ 确认窗口的音频不进声纹采集。"""
        chunks = [_audio(0.3) for _ in range(5)]
        adapter, _ = _make_adapter(capture=FakeCapture(chunks))

        await adapter.start_confirmation_window(user_id="stranger_x")

        assert adapter._enrollment_audio == [], "确认音频混进了声纹采集"

    @pytest.mark.asyncio
    async def test_state_returns_to_idle(self) -> None:
        adapter, _ = _make_adapter(capture=FakeCapture([_audio(0.3) for _ in range(5)]))
        await adapter.start_confirmation_window(user_id="stranger_x")
        assert adapter.state == adapter.State.IDLE

    @pytest.mark.asyncio
    async def test_empty_asr_keeps_waiting(self) -> None:
        """识别不出文本 → 继续等,不误判为同意。"""
        adapter, bus = _make_adapter(
            capture=FakeCapture([_audio(0.3) for _ in range(5)]),
            asr=FakeASR(""),
        )
        result = await adapter.start_confirmation_window(user_id="stranger_x")
        assert result is None
        assert bus.of_action(VoiceAction.CONFIRM_ANSWER) == []


# ---------------------------------------------------------------------------
# 3. 回声抑制
# ---------------------------------------------------------------------------


class TestEchoSuppression:
    """播放期间的麦克风音频必须被丢弃。"""

    @pytest.mark.asyncio
    async def test_confirmation_window_waits_for_gate_to_clear(self) -> None:
        """⭐ 窗口会先等门控结束(TTS 播完)再开始听 —— 不会把管家的话当用户回答。"""
        player = FakePlayer(tail=0.5)
        chunks = [_audio(0.3) for _ in range(5)]
        adapter, _ = _make_adapter(capture=FakeCapture(chunks), player=player)

        # 模拟 TTS 正在播报,还剩 0.3s
        player._gate_until = time.monotonic() + 0.3

        start = time.monotonic()
        result = await adapter.start_confirmation_window(user_id="stranger_x")
        elapsed = time.monotonic() - start

        assert result == "要", "门控结束后应该能采到用户回答"
        assert elapsed >= 0.3, f"没等门控结束就开始听了({elapsed:.2f}s)"

    @pytest.mark.asyncio
    async def test_gate_opened_mid_window_drops_audio(self) -> None:
        """窗口进行中又开始播报 → 期间音频被丢弃。"""
        player = FakePlayer(tail=10.0)
        chunks = [_audio(0.3) for _ in range(5)]
        adapter, bus = _make_adapter(capture=FakeCapture(chunks), player=player)

        # 门控一开始就是开的(长时间)
        player._gate_until = time.monotonic() + 10.0

        # 但窗口会等门控… 所以这里直接测采集循环的门控语义:
        # 换成"门控在窗口中途打开"的等价场景 —— 用 enrollment 循环验证。
        adapter._enrollment_mode = True
        adapter._enrollment_timeout_seconds = 0.2
        await adapter._enrollment_listen()
        assert adapter._enrollment_audio == []
        assert bus.of_action(VoiceAction.CONFIRM_ANSWER) == []

    @pytest.mark.asyncio
    async def test_gate_expires_after_tail(self) -> None:
        """门控过期后恢复正常采集。"""
        player = FakePlayer(tail=0.1)
        chunks = [_audio(0.3) for _ in range(5)]
        adapter, _ = _make_adapter(capture=FakeCapture(chunks), player=player)

        player._gate_until = time.monotonic() - 1.0  # 已过期

        result = await adapter.start_confirmation_window(user_id="stranger_x")
        assert result == "要"

    @pytest.mark.asyncio
    async def test_enrollment_skips_gated_audio(self) -> None:
        """采集循环同样受门控保护。"""
        player = FakePlayer(tail=0.5)
        adapter, _ = _make_adapter(capture=FakeCapture([_audio(0.3)]), player=player)
        adapter._enrollment_mode = True
        adapter._enrollment_timeout_seconds = 0.2

        player._gate_until = time.monotonic() + 0.5

        await adapter._enrollment_listen()

        assert adapter._enrollment_audio == [], "门控期音频进了声纹"

    def test_real_player_exposes_gate(self) -> None:
        """真 player 提供门控状态(供采集侧读取)。"""
        from smartbutler.voice.player.pygame_player import FakeAudioPlayer

        player = FakeAudioPlayer()
        assert hasattr(player, "playback_gate_until")
        assert hasattr(player, "play_gated")
        assert not player.is_playback_gated()


# ---------------------------------------------------------------------------
# 4. 拒绝冷却(当天不再询问)
# ---------------------------------------------------------------------------


class TestConsentCooldown:
    """用户拒绝后当天不再询问。"""

    def test_reject_blocks_further_asking(self) -> None:
        adapter, _ = _make_adapter()
        assert not adapter._is_in_consent_cooldown("stranger_x")

        adapter.mark_consent_rejected("stranger_x")
        assert adapter._is_in_consent_cooldown("stranger_x")

    def test_cooldown_expires(self) -> None:
        adapter, _ = _make_adapter()
        adapter._consent_cooldown_seconds = 0.01
        adapter.mark_consent_rejected("stranger_x")
        time.sleep(0.02)
        assert not adapter._is_in_consent_cooldown("stranger_x")

    def test_cooldown_is_per_user(self) -> None:
        adapter, _ = _make_adapter()
        adapter.mark_consent_rejected("speaker_mom")
        assert not adapter._is_in_consent_cooldown("speaker_dad")

    def test_stranger_cooldown_is_device_scoped(self) -> None:
        """⭐ 陌生人 id 是临时哈希,同一人的 id 会变 → 必须按设备冷却。

        否则"拒绝后当天不询问"会静默失效:
        第 1 轮记 ``stranger_a1b2``,第 2 轮 id 变 ``stranger_c3d4``
        → 查不到 → 又去问。
        """
        adapter, _ = _make_adapter()
        adapter.mark_consent_rejected("stranger_a1b2c3d")
        # 同一个人,下一轮哈希不同
        assert adapter._is_in_consent_cooldown("stranger_e4f5g6h")
        # 同设备的任何陌生人都不再问
        assert adapter._is_in_consent_cooldown("stranger_zzz")

    def test_known_user_cooldown_is_user_scoped(self) -> None:
        """已注册 user_id → 精确到人(重采集场景)。"""
        adapter, _ = _make_adapter()
        adapter.mark_consent_rejected("speaker_mom")
        assert adapter._is_in_consent_cooldown("speaker_mom")
        assert not adapter._is_in_consent_cooldown("speaker_dad")

    def test_clear_cooldown(self) -> None:
        adapter, _ = _make_adapter()
        adapter.mark_consent_rejected("stranger_x")
        adapter.clear_consent_cooldown("stranger_x")
        assert not adapter._is_in_consent_cooldown("stranger_x")

    def test_cooldown_default_is_one_day(self) -> None:
        """用户决策:当天不询问。"""
        adapter, _ = _make_adapter()
        assert adapter._consent_cooldown_seconds == 24 * 3600


# ---------------------------------------------------------------------------
# 5. 陌生人只标记不采集
# ---------------------------------------------------------------------------


class TestSuggestEnrollmentOnly:
    """_flush_buffer 不再自动启动采集,只标记建议。"""

    @pytest.mark.asyncio
    async def test_stranger_event_suggests_enrollment(self) -> None:
        """⭐ 陌生人事件带 suggest_enrollment=True,但不启动采集。"""
        chunks = [_audio(0.3) for _ in range(4)] + [_audio(0.001) for _ in range(40)]
        adapter, bus = _make_adapter(capture=FakeCapture(chunks))
        adapter._asr = FakeASR("你好")  # type: ignore[assignment]

        class FakeEmbedder:
            async def embed(self, audio, sample_rate=16000):
                return [0.1] * 256

        adapter._embedder = FakeEmbedder()  # type: ignore[assignment]

        class FakeRegistry:
            async def identify_speaker_info(self, embedding):
                # 必须返回真正的 SpeakerInfo(pydantic 会校验)
                return SpeakerInfo(
                    user_id="unknown:x",
                    confidence=0.0,
                    is_known=False,
                    embedding=embedding,
                )

            async def identify(self, embedding):
                return type(
                    "M",
                    (),
                    {
                        "is_known": False,
                        "matched_user_id": None,
                        "candidates": [],
                    },
                )()

            def should_trigger_enrollment(self, match, *, audio_rms=None):
                return True, "stranger"

        adapter._registry = FakeRegistry()  # type: ignore[assignment]
        adapter._buffer = [_audio(0.3)]
        adapter._buffer_ms = 100

        await adapter._flush_buffer()

        commands = bus.of_action(VoiceAction.COMMAND)
        assert len(commands) == 1
        assert commands[0].suggest_enrollment is True
        assert commands[0].enrollment_reason == "stranger"
        # 关键:没有启动采集
        assert adapter._enrollment_mode is False, "又自动启动采集了"

    @pytest.mark.asyncio
    async def test_known_speaker_does_not_suggest(self) -> None:
        """熟人 → 不建议询问。"""

        class FakeEmbedder:
            async def embed(self, audio, sample_rate=16000):
                return [0.1] * 256

        adapter, bus = _make_adapter()
        adapter._embedder = FakeEmbedder()  # type: ignore[assignment]

        class FakeRegistry:
            async def identify_speaker_info(self, embedding):
                return SpeakerInfo(
                    user_id="speaker_mom",
                    confidence=0.95,
                    is_known=True,
                    embedding=embedding,
                    display_name="妈妈",
                )

        adapter._registry = FakeRegistry()  # type: ignore[assignment]
        adapter._buffer = [_audio(0.3)]
        adapter._buffer_ms = 100
        adapter._asr = FakeASR("你好")  # type: ignore[assignment]

        await adapter._flush_buffer()

        commands = bus.of_action(VoiceAction.COMMAND)
        assert commands[0].suggest_enrollment is False


# ---------------------------------------------------------------------------
# 6. 协议层
# ---------------------------------------------------------------------------


class TestVoiceEventConsentProtocol:
    """事件协议:suggest_enrollment / CONFIRM_ANSWER。"""

    def test_suggest_enrollment_defaults_false(self) -> None:
        e = VoiceEvent.make(
            action=VoiceAction.COMMAND,
            device_id="d",
            user_id="u",
            asr_text="你好",
        )
        assert e.suggest_enrollment is False
        assert e.enrollment_reason == ""

    def test_command_input_contains_enrollment_hint(self) -> None:
        e = VoiceEvent.make(
            action=VoiceAction.COMMAND,
            device_id="d",
            user_id="stranger_x",
            asr_text="你好",
            suggest_enrollment=True,
            enrollment_reason="stranger",
        )
        text = e.to_user_input()
        assert "可询问登记声纹" in text
        assert "同意" in text

    def test_confirm_answer_input_is_explicit(self) -> None:
        e = VoiceEvent.make(
            action=VoiceAction.CONFIRM_ANSWER,
            device_id="d",
            user_id="stranger_x",
            asr_text="要",
        )
        text = e.to_user_input()
        assert "声纹采集确认" in text
        assert "要" in text
        assert "confirm=true" in text

    def test_known_speaker_input_has_no_enrollment_hint(self) -> None:
        e = VoiceEvent.make(
            action=VoiceAction.COMMAND,
            device_id="d",
            user_id="speaker_mom",
            asr_text="你好",
        )
        assert "可询问登记声纹" not in e.to_user_input()


# ---------------------------------------------------------------------------
# 7. low_confidence 标记
# ---------------------------------------------------------------------------


class TestLowConfidenceMarker:
    """单句样本建档 → 标记 low_confidence。"""

    @pytest.mark.asyncio
    async def test_single_sample_marked_low_confidence(self) -> None:
        from smartbutler.events.routing.voiceprint_enrollment import (
            LOW_CONFIDENCE_SAMPLE_THRESHOLD,
            VoiceprintEnrollmentService,
        )

        assert LOW_CONFIDENCE_SAMPLE_THRESHOLD == 3

        registered: list = []

        class FakeRegistry:
            async def get(self, user_id):
                return None

            async def find_best_match(self, embedding):
                return type("B", (), {"matched_user_id": None, "confidence": 0.0})()

            async def register(self, profile):
                registered.append(profile)

        class FakeEmbedder:
            async def embed(self, audio, sample_rate=16000):
                return [0.1] * 256

        service = VoiceprintEnrollmentService(
            registry=FakeRegistry(),  # type: ignore[arg-type]
            embedder=FakeEmbedder(),
        )

        # 只有 1 段,时长 2s,RMS 0.1
        audio = np.full(16000 * 2, 0.1, dtype=np.float32)
        result = await service.enroll_from_segments(
            [(audio, 16000)], display_name="小明"
        )

        assert result.status == "registered"
        assert len(registered) == 1
        assert registered[0].metadata.get("low_confidence") is True

    @pytest.mark.asyncio
    async def test_three_samples_not_low_confidence(self) -> None:
        from smartbutler.events.routing.voiceprint_enrollment import (
            VoiceprintEnrollmentService,
        )

        registered: list = []

        class FakeRegistry:
            async def get(self, user_id):
                return None

            async def find_best_match(self, embedding):
                return type("B", (), {"matched_user_id": None, "confidence": 0.0})()

            async def register(self, profile):
                registered.append(profile)

        class FakeEmbedder:
            async def embed(self, audio, sample_rate=16000):
                return [0.1] * 256

        service = VoiceprintEnrollmentService(
            registry=FakeRegistry(),  # type: ignore[arg-type]
            embedder=FakeEmbedder(),
        )

        audio = np.full(16000 * 2, 0.1, dtype=np.float32)
        await service.enroll_from_segments(
            [(audio, 16000), (audio, 16000), (audio, 16000)], display_name="小明"
        )

        assert registered[0].metadata.get("low_confidence") is False


# ---------------------------------------------------------------------------
# 8. prompt 指引
# ---------------------------------------------------------------------------


class TestPromptGuide:
    """system prompt 里有声纹登记指引(且只在工具可用时)。"""

    def test_guide_injected_when_tool_available(self) -> None:
        from smartbutler.thinking.prompt.builder import ButlerPromptBuilder

        class FakeTool:
            name = "enroll_speaker"
            description = "建立声纹档案"

        prompt = ButlerPromptBuilder().build(tool_specs=[FakeTool()])
        assert "声纹登记" in prompt
        assert "confirm=true" in prompt

    def test_guide_not_injected_without_tool(self) -> None:
        from smartbutler.thinking.prompt.builder import ButlerPromptBuilder

        class OtherTool:
            name = "web_fetch"
            description = "抓网页"

        prompt = ButlerPromptBuilder().build(tool_specs=[OtherTool()])
        assert "confirm=true" not in prompt
