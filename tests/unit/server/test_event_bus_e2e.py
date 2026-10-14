"""EventBus 端到端测试(Phase 7.4)。

覆盖 §3.4 列出的 5 case:
1. publish 1 个 DeviceEvent → 1 次 OutputAdapter 收到
2. publish 60 个同 event_id DeviceEvent(window=5s) → 只 1 次 OutputAdapter 收到
3. publish 30 个不同 event_id DeviceEvent(同 user) → 限速 20/min,只 20 次收到
4. Trigger 调 proactive_tick 抛异常 → OutputAdapter 收到 fallback 文本,EventBus 不死循环
5. VoiceEvent(source=VOICE) → Trigger 路由到 Proactive(沿用 ADR-009 §5.9.3:EventBus 走 Proactive)
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from datetime import UTC, datetime

import pytest

from smartbutler.events.core.event import BaseEvent, EventPriority, EventSource
from smartbutler.events.core.normalizer_dedup import DedupNormalizer
from smartbutler.events.core.trigger_default import DefaultEventTrigger
from smartbutler.server.adapters import ButlerReply
from smartbutler.server.adapters.stub import InMemoryOutputAdapter
from smartbutler.server.bus import InMemoryEventBus
from smartbutler.server.runtime import ButlerServer, ServerConfig
from smartbutler.thinking.loop.orchestrator import ButlerOrchestrator
from smartbutler.thinking.proactive.decision import ProactiveResult, ProactiveUrgency


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class _FakeBaseLLM:
    async def chat(self, messages: list[Any], **kwargs: Any) -> Any:  # noqa: ARG002
        return None

    async def chat_stream(  # noqa: ARG002
        self, messages: list[Any], **kwargs: Any
    ) -> AsyncIterator[Any]:
        return
        yield  # type: ignore[unreachable]

    async def aclose(self) -> None:
        pass


class _FakeSettings:
    provider = "openai"
    openai_api_key = "sk-fake"
    openai_base_url = "https://api.openai.com/v1"
    model = "gpt-4o-mini"
    temperature = 0.7
    max_tokens = 1024
    timeout = 60.0
    max_retries = 2


class _FakeProactiveLoop:
    """可控的 ProactiveLoop mock —— 单测断言 acted=True/False 切换。"""

    def __init__(self, *, message: str = "管家主动播报", acted: bool = True) -> None:
        self.message = message
        self.acted = acted
        self.tick_calls: list[BaseEvent] = []

    async def tick(self, event: BaseEvent) -> ProactiveResult:  # noqa: ARG002
        self.tick_calls.append(event)
        return ProactiveResult(
            acted=self.acted,
            message=self.message if self.acted else None,
            urgency=ProactiveUrgency.LOW,
            silence_reason="mock" if not self.acted else "",
        )


def _make_orchestrator_with_loop(loop: _FakeProactiveLoop) -> ButlerOrchestrator:
    """构造 ButlerOrchestrator,proactive_tick 走可控的 _FakeProactiveLoop。"""
    orch = ButlerOrchestrator(
        llm=_FakeBaseLLM(),  # type: ignore[arg-type]
        llm_settings=_FakeSettings(),
    )
    # 拦截 proactive_tick:用我们的 mock loop
    async def fake_proactive_tick(event: BaseEvent, **kwargs: Any) -> ProactiveResult:  # noqa: ARG002
        return await loop.tick(event)
    orch.proactive_tick = fake_proactive_tick  # type: ignore[method-assign]
    return orch


def _make_event(
    *,
    event_id: str = "evt_test_001",
    source: EventSource = EventSource.DEVICE,
    topic: str = "device.door.opened",
    user_id: str = "u1",
    priority: EventPriority = EventPriority.NORMAL,
) -> BaseEvent:
    return BaseEvent(
        event_id=event_id,
        source=source,
        topic=topic,
        user_id=user_id,
        timestamp=datetime.now(UTC),
        priority=priority,
        device_id="door_front_01",
    )


@pytest.fixture
def server_with_loop() -> tuple[ButlerServer, _FakeProactiveLoop, InMemoryOutputAdapter, InMemoryEventBus]:
    """构造带 mock loop 的 server(acted=True,默认 message)。"""
    loop = _FakeProactiveLoop(message="管家说:厨房有烟", acted=True)
    orch = _make_orchestrator_with_loop(loop)
    bus = InMemoryEventBus(history_maxsize=200)
    output = InMemoryOutputAdapter()
    cfg = ServerConfig(
        event_bus=bus,
        enable_event_bus=True,
        auto_subscribe_proactive=True,
        enable_normalizer=True,
        enable_trigger=True,
        normalizer_window_sec=5.0,
        trigger_max_per_minute=20,
    )
    server = ButlerServer(orchestrator=orch, config=cfg, output_adapters=[output])
    return server, loop, output, bus


# ---------------------------------------------------------------------------
# Case 1:publish 1 个 DeviceEvent → 1 次 OutputAdapter 收到
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_case1_single_event_passes_through_normalizer_and_trigger(
    server_with_loop: tuple[ButlerServer, _FakeProactiveLoop, InMemoryOutputAdapter, InMemoryEventBus],
) -> None:
    """case 1:单个事件走 Normalizer(pass) + Trigger(pass) → 1 次答复。"""
    server, loop, output, bus = server_with_loop
    await server.start()
    try:
        event = _make_event(event_id="evt_001")
        await bus.publish(event)
        # 等 consumer 拉
        import asyncio
        await asyncio.sleep(0.05)
        # output 收到 1 次
        assert len(output.replies) == 1
        assert output.replies[0].content == "管家说:厨房有烟"
        assert output.replies[0].source == "proactive"
        assert output.replies[0].related_event_id == "evt_001"
        # loop 被调 1 次
        assert len(loop.tick_calls) == 1
        # Normalizer 看到 1 received / 1 passed / 0 deduped
        assert server._normalizer is not None  # type: ignore[attr-defined]
        snap = server._normalizer.snapshot()  # type: ignore[attr-defined]
        assert snap["stats"]["received"] == 1
        assert snap["stats"]["passed"] == 1
        assert snap["stats"]["deduped"] == 0
    finally:
        await server.stop()


# ---------------------------------------------------------------------------
# Case 2:publish 60 个同 event_id → Normalizer dedup → 只 1 次
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_case2_60_duplicate_events_deduped_to_one(
    server_with_loop: tuple[ButlerServer, _FakeProactiveLoop, InMemoryOutputAdapter, InMemoryEventBus],
) -> None:
    """case 2:60 个同 event_id 事件 → window 5s 内只有 1 个通过。"""
    server, loop, output, bus = server_with_loop
    await server.start()
    try:
        # publish 60 个同 event_id
        for _ in range(60):
            await bus.publish(_make_event(event_id="evt_dup"))
        import asyncio
        await asyncio.sleep(0.1)
        # output 只收到 1 次
        assert len(output.replies) == 1
        assert output.replies[0].content == "管家说:厨房有烟"
        # loop 只被调 1 次
        assert len(loop.tick_calls) == 1
        # Normalizer 统计:60 received, 1 passed, 59 deduped
        snap = server._normalizer.snapshot()  # type: ignore[attr-defined]
        assert snap["stats"]["received"] == 60
        assert snap["stats"]["passed"] == 1
        assert snap["stats"]["deduped"] == 59
    finally:
        await server.stop()


# ---------------------------------------------------------------------------
# Case 3:30 个不同 event_id → 限速 20/min → 只 20 次
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_case3_30_unique_events_rate_limited_to_20(
    server_with_loop: tuple[ButlerServer, _FakeProactiveLoop, InMemoryOutputAdapter, InMemoryEventBus],
) -> None:
    """case 3:30 个不同 event_id(同 user)→ 限速 20/min,只 20 次收到。"""
    server, loop, output, bus = server_with_loop
    await server.start()
    try:
        for i in range(30):
            await bus.publish(_make_event(event_id=f"evt_unique_{i:03d}"))
        import asyncio
        await asyncio.sleep(0.15)
        # output 收到 20 次(前 20 过限速,后 10 被限速)
        assert len(output.replies) == 20
        # loop 被调 20 次
        assert len(loop.tick_calls) == 20
        # Trigger 限速统计
        snap = server._trigger.snapshot()  # type: ignore[attr-defined]
        assert snap["stats"]["received"] == 30
        assert snap["stats"]["proactive_called"] == 20
        assert snap["stats"]["rate_limited"] == 10
    finally:
        await server.stop()


# ---------------------------------------------------------------------------
# Case 4:Trigger.handle 抛异常 → OutputAdapter 收到 fallback 文本,EventBus 不死循环
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_case4_proactive_tick_exception_does_not_crash_eventbus() -> None:
    """case 4:proactive_tick 抛异常 → Trigger.handle 内部 try/except → 返 fallback 文本。"""
    # 构造 orchestrator:proactive_tick 直接抛异常
    orch = ButlerOrchestrator(
        llm=_FakeBaseLLM(),  # type: ignore[arg-type]
        llm_settings=_FakeSettings(),
    )
    async def boom(event: BaseEvent, **kwargs: Any) -> ProactiveResult:  # noqa: ARG002
        raise RuntimeError("simulated LLM down")
    orch.proactive_tick = boom  # type: ignore[method-assign]

    bus = InMemoryEventBus()
    output = InMemoryOutputAdapter()
    cfg = ServerConfig(
        event_bus=bus,
        enable_event_bus=True,
        auto_subscribe_proactive=True,
        enable_normalizer=True,
        enable_trigger=True,
    )
    server = ButlerServer(orchestrator=orch, config=cfg, output_adapters=[output])
    await server.start()
    try:
        # publish 1 个事件 → 抛异常 → Trigger.handle 兜底 → 返 fallback 文本
        await bus.publish(_make_event(event_id="evt_boom"))
        import asyncio
        await asyncio.sleep(0.05)
        # output 收到 1 次 fallback
        assert len(output.replies) == 1
        assert "[error]" in output.replies[0].content
        assert "simulated LLM down" not in output.replies[0].content  # 兜底文案不带原始异常细节
        # EventBus consumer 没死(再 publish 一条仍能消费)
        # 用同一个 event_id,window 内 dedup → 应被丢弃
        await bus.publish(_make_event(event_id="evt_boom"))
        await asyncio.sleep(0.05)
        # 仍只有 1 次 output(Normalizer dedup 第二次)
        assert len(output.replies) == 1
    finally:
        await server.stop()


# ---------------------------------------------------------------------------
# Case 5:VoiceEvent(source=VOICE) → 仍走 Proactive(ADR-009 §5.9.3)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_case5_voice_event_routes_to_proactive(
    server_with_loop: tuple[ButlerServer, _FakeProactiveLoop, InMemoryOutputAdapter, InMemoryEventBus],
) -> None:
    """case 5:VoiceEvent(source=VOICE) → Trigger 路由到 Proactive(不是 Reactive)。

    ADR-009 §5.9.3:EventBus 推送的所有事件 → Proactive。
    Reactive 入口是 submit_reactive,不经 EventBus。
    """
    server, loop, output, bus = server_with_loop
    await server.start()
    try:
        # VoiceEvent 走 EventBus(通过 publish) → 应被 dedup+trigger → 1 次答复
        voice_event = BaseEvent(
            event_id="evt_voice_001",
            source=EventSource.VOICE,
            topic="voice.command",
            user_id="u1",
            timestamp=datetime.now(UTC),
            priority=EventPriority.NORMAL,
            payload={"asr_text": "管家,关灯"},
        )
        await bus.publish(voice_event)
        import asyncio
        await asyncio.sleep(0.05)
        # output 收到 1 次(走 Proactive,不是 Reactive)
        assert len(output.replies) == 1
        # ProactiveLoop 被调
        assert len(loop.tick_calls) == 1
        # Trigger 统计:1 proactive,0 reactive
        snap = server._trigger.snapshot()  # type: ignore[attr-defined]
        assert snap["stats"]["proactive_called"] == 1
        assert snap["stats"]["reactive_called"] == 0
    finally:
        await server.stop()


# ---------------------------------------------------------------------------
# 补充:Trigger 异常兜底 + 老逻辑(无 Normalizer/Trigger) 兼容
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_legacy_path_without_normalizer_trigger() -> None:
    """兼容:enable_normalizer=False 且 enable_trigger=False → 走老 _on_bus_event 逻辑。"""
    orch = _make_orchestrator_with_loop(_FakeProactiveLoop(message="老逻辑 OK", acted=True))
    bus = InMemoryEventBus()
    output = InMemoryOutputAdapter()
    cfg = ServerConfig(
        event_bus=bus,
        enable_event_bus=True,
        auto_subscribe_proactive=True,
        enable_normalizer=False,  # 关掉
        enable_trigger=False,     # 关掉
    )
    server = ButlerServer(orchestrator=orch, config=cfg, output_adapters=[output])
    await server.start()
    try:
        assert server._normalizer is None  # type: ignore[attr-defined]
        assert server._trigger is None  # type: ignore[attr-defined]
        await bus.publish(_make_event(event_id="evt_legacy"))
        import asyncio
        await asyncio.sleep(0.05)
        assert len(output.replies) == 1
        assert output.replies[0].content == "老逻辑 OK"
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_silent_proactive_returns_no_reply(
    server_with_loop: tuple[ButlerServer, _FakeProactiveLoop, InMemoryOutputAdapter, InMemoryEventBus],
) -> None:
    """ProactiveLoop silent(acted=False)→ Trigger.handle 返 None → 不下发到 OutputAdapter。"""
    server, loop, output, bus = server_with_loop
    # 改 loop 为 silent
    loop.acted = False
    loop.message = None
    await server.start()
    try:
        await bus.publish(_make_event(event_id="evt_silent"))
        import asyncio
        await asyncio.sleep(0.05)
        # silent → 不下发
        assert len(output.replies) == 0
        snap = server._trigger.snapshot()  # type: ignore[attr-defined]
        assert snap["stats"]["silent"] == 1
    finally:
        await server.stop()


# ---------------------------------------------------------------------------
# DedupNormalizer / DefaultEventTrigger 单元测试
# ---------------------------------------------------------------------------


class TestDedupNormalizer:
    @pytest.mark.asyncio
    async def test_first_event_passes(self) -> None:
        n = DedupNormalizer(window_sec=5.0)
        e = _make_event(event_id="e1")
        result = await n.normalize(e)
        assert result is e

    @pytest.mark.asyncio
    async def test_duplicate_within_window_deduped(self) -> None:
        n = DedupNormalizer(window_sec=5.0)
        e = _make_event(event_id="e1")
        await n.normalize(e)
        result = await n.normalize(e)
        assert result is None

    @pytest.mark.asyncio
    async def test_duplicate_outside_window_passes(self) -> None:
        n = DedupNormalizer(window_sec=0.1)
        e = _make_event(event_id="e1")
        await n.normalize(e)
        import asyncio
        await asyncio.sleep(0.15)  # 超过窗口
        result = await n.normalize(e)
        assert result is e

    @pytest.mark.asyncio
    async def test_lru_eviction(self) -> None:
        n = DedupNormalizer(window_sec=5.0, max_size=3)
        for i in range(5):
            await n.normalize(_make_event(event_id=f"e{i}"))
        snap = n.snapshot()
        assert snap["seen_count"] == 3
        assert snap["stats"]["evicted"] == 2

    def test_invalid_window(self) -> None:
        with pytest.raises(ValueError):
            DedupNormalizer(window_sec=0)

    def test_invalid_max_size(self) -> None:
        with pytest.raises(ValueError):
            DedupNormalizer(max_size=0)


class TestDefaultEventTrigger:
    @pytest.mark.asyncio
    async def test_should_handle_pass(self) -> None:
        loop = _FakeProactiveLoop()
        orch = _make_orchestrator_with_loop(loop)
        t = DefaultEventTrigger(orchestrator=orch, max_per_minute=5)
        assert await t.should_handle(_make_event(event_id="e1")) is True

    @pytest.mark.asyncio
    async def test_rate_limit(self) -> None:
        loop = _FakeProactiveLoop()
        orch = _make_orchestrator_with_loop(loop)
        t = DefaultEventTrigger(orchestrator=orch, max_per_minute=3)
        for i in range(5):
            r = await t.should_handle(_make_event(event_id=f"e{i}"))
            assert r is (i < 3)  # 前 3 通过,后 2 限速
        snap = t.snapshot()
        assert snap["stats"]["rate_limited"] == 2

    @pytest.mark.asyncio
    async def test_handle_proactive_acted(self) -> None:
        loop = _FakeProactiveLoop(message="播报", acted=True)
        orch = _make_orchestrator_with_loop(loop)
        t = DefaultEventTrigger(orchestrator=orch)
        answer = await t.handle(_make_event(event_id="e1"))
        assert answer == "播报"

    @pytest.mark.asyncio
    async def test_handle_proactive_silent(self) -> None:
        loop = _FakeProactiveLoop(acted=False, message=None)
        orch = _make_orchestrator_with_loop(loop)
        t = DefaultEventTrigger(orchestrator=orch)
        answer = await t.handle(_make_event(event_id="e1"))
        assert answer is None

    @pytest.mark.asyncio
    async def test_handle_exception_returns_fallback(self) -> None:
        orch = ButlerOrchestrator(
            llm=_FakeBaseLLM(),  # type: ignore[arg-type]
            llm_settings=_FakeSettings(),
        )
        async def boom(event: BaseEvent, **kwargs: Any) -> ProactiveResult:  # noqa: ARG002
            raise RuntimeError("boom")
        orch.proactive_tick = boom  # type: ignore[method-assign]
        t = DefaultEventTrigger(orchestrator=orch)
        answer = await t.handle(_make_event(event_id="e1"))
        # 兜底:返 fallback 文本
        assert answer is not None
        assert "[error]" in answer

    def test_invalid_max_per_minute(self) -> None:
        with pytest.raises(ValueError):
            DefaultEventTrigger(orchestrator=None, max_per_minute=0)  # type: ignore[arg-type]
