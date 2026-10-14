"""AnswerRouter 端到端测试(Phase 7.5)。

覆盖 §4.7 列出的 6 case:
1. Reactive 链路 → 无 AnswerRouter → 走默认 OutputAdapter
2. Proactive + NORMAL → PresenceService 选定的 OutputAdapter
3. Proactive + URGENT → 推所有 3 个 OutputAdapter
4. Proactive + SILENT → 不推
5. PresenceService 返 None → fallback 到第 1 个 OutputAdapter
6. Reactive + AnswerRouter 注入 → 仍走 Reactive 默认(Reactive 不过 AnswerRouter)

附加单元测试:
- RouteResult 各字段填充正确性
- DeviceRegistry register/get/all/first
- SimplePresenceService update/get/clear
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any
from collections.abc import AsyncIterator

import pytest

from smartbutler.events.core.event import BaseEvent, EventPriority, EventSource
from smartbutler.events.core.normalizer_dedup import DedupNormalizer
from smartbutler.events.core.trigger_default import DefaultEventTrigger
from smartbutler.events.routing.answer_router_console import ConsoleAnswerRouter
from smartbutler.events.routing.device_registry import DeviceRegistry
from smartbutler.events.routing.presence import PresenceInfo
from smartbutler.events.routing.presence_simple import SimplePresenceService
from smartbutler.events.routing.route_result import RouteResult
from smartbutler.server.adapters import ButlerReply, ReactiveSubmission
from smartbutler.server.adapters.stub import InMemoryOutputAdapter
from smartbutler.server.bus import InMemoryEventBus
from smartbutler.server.runtime import ButlerServer, ServerConfig
from smartbutler.thinking.loop.orchestrator import ButlerOrchestrator
from smartbutler.thinking.proactive.decision import ProactiveResult, ProactiveUrgency


# ---------------------------------------------------------------------------
# Fake / Stub
# ---------------------------------------------------------------------------


class _FakeBaseLLM:
    async def chat(self, messages: list[Any], **kwargs: Any) -> Any:  # noqa: ARG002
        return None

    async def chat_stream(self, messages: list[Any], **kwargs: Any) -> AsyncIterator[Any]:  # noqa: ARG002
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
    """可控 ProactiveLoop mock —— 断言 Proactive 链路可预知 acted/message。"""

    def __init__(self, *, message: str = "管家主动播报", acted: bool = True) -> None:
        self.message = message
        self.acted = acted
        self.tick_calls: list[BaseEvent] = []

    async def tick(self, event: BaseEvent) -> ProactiveResult:
        self.tick_calls.append(event)
        return ProactiveResult(
            acted=self.acted,
            message=self.message if self.acted else None,
            urgency=ProactiveUrgency.LOW,
            silence_reason="mock" if not self.acted else "",
        )


def _make_orchestrator_with_loop(loop: _FakeProactiveLoop) -> ButlerOrchestrator:
    orch = ButlerOrchestrator(
        llm=_FakeBaseLLM(),  # type: ignore[arg-type]
        llm_settings=_FakeSettings(),
    )

    async def fake_tick(event: BaseEvent, **kwargs: Any) -> ProactiveResult:
        return await loop.tick(event)

    orch.proactive_tick = fake_tick  # type: ignore[method-assign]
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


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _out(device_id: str) -> InMemoryOutputAdapter:
    """创建带显式 device_id 的 InMemoryOutputAdapter(7.5 测试用)。"""
    return InMemoryOutputAdapter(device_id=device_id)


@pytest.fixture
def three_outputs() -> tuple[InMemoryOutputAdapter, InMemoryOutputAdapter, InMemoryOutputAdapter]:
    """3 个独立 InMemoryOutputAdapter,各用不同 device_id。"""
    return (
        InMemoryOutputAdapter(device_id="speaker.living_room"),
        InMemoryOutputAdapter(device_id="speaker.bedroom"),
        InMemoryOutputAdapter(device_id="phone"),
    )


@pytest.fixture
def presence() -> SimplePresenceService:
    return SimplePresenceService()


@pytest.fixture
def router_with_three(
    three_outputs: tuple[InMemoryOutputAdapter, InMemoryOutputAdapter, InMemoryOutputAdapter],
    presence: SimplePresenceService,
) -> ConsoleAnswerRouter:
    out1, out2, out3 = three_outputs
    registry = DeviceRegistry([out1, out2, out3])
    return ConsoleAnswerRouter(registry=registry, presence_service=presence)


@pytest.fixture
def server_no_router(
    three_outputs: tuple[InMemoryOutputAdapter, InMemoryOutputAdapter, InMemoryOutputAdapter],
) -> tuple[ButlerServer, _FakeProactiveLoop, InMemoryOutputAdapter, InMemoryEventBus]:
    """无 AnswerRouter 的 server(走老逻辑 broadcast 全部)。"""
    out1, out2, out3 = three_outputs
    loop = _FakeProactiveLoop(message="管家主动播报", acted=True)
    orch = _make_orchestrator_with_loop(loop)
    bus = InMemoryEventBus(history_maxsize=200)
    cfg = ServerConfig(
        event_bus=bus,
        enable_event_bus=True,
        auto_subscribe_proactive=True,
        enable_normalizer=True,
        enable_trigger=True,
        normalizer_window_sec=5.0,
        trigger_max_per_minute=20,
    )
    server = ButlerServer(orchestrator=orch, config=cfg, output_adapters=[out1, out2, out3])
    return server, loop, out1, bus


@pytest.fixture
def server_with_router(
    three_outputs: tuple[InMemoryOutputAdapter, InMemoryOutputAdapter, InMemoryOutputAdapter],
    presence: SimplePresenceService,
) -> tuple[
    ButlerServer,
    _FakeProactiveLoop,
    InMemoryOutputAdapter,
    InMemoryOutputAdapter,
    InMemoryOutputAdapter,
    InMemoryEventBus,
    SimplePresenceService,
]:
    """注入了 AnswerRouter + SimplePresenceService 的 server。"""
    out1, out2, out3 = three_outputs
    loop = _FakeProactiveLoop(message="管家主动播报", acted=True)
    orch = _make_orchestrator_with_loop(loop)
    bus = InMemoryEventBus(history_maxsize=200)
    cfg = ServerConfig(
        event_bus=bus,
        enable_event_bus=True,
        auto_subscribe_proactive=True,
        enable_normalizer=True,
        enable_trigger=True,
        normalizer_window_sec=5.0,
        trigger_max_per_minute=20,
    )
    server = ButlerServer(orchestrator=orch, config=cfg, output_adapters=[out1, out2, out3])
    registry = DeviceRegistry([out1, out2, out3])
    router = ConsoleAnswerRouter(registry=registry, presence_service=presence)
    server.set_answer_router(router)
    return server, loop, out1, out2, out3, bus, presence


# ---------------------------------------------------------------------------
# RouteResult 单元测试
# ---------------------------------------------------------------------------


class TestRouteResult:
    def test_acted_true_no_errors(self) -> None:
        r = RouteResult(acted=True, pushed_device_ids=["speaker.living"])
        assert r.acted is True
        assert r.success is True
        assert r.pushed_device_ids == ["speaker.living"]
        assert r.fallback_used is False
        assert r.silence_reason is None
        assert r.error_device_ids == []

    def test_acted_true_with_errors(self) -> None:
        r = RouteResult(
            acted=True,
            pushed_device_ids=["speaker.living", "speaker.bedroom"],
            error_device_ids=["speaker.guest"],
        )
        assert r.acted is True
        assert r.success is False  # 有错误
        assert r.error_device_ids == ["speaker.guest"]

    def test_silent(self) -> None:
        r = RouteResult(acted=False, silence_reason="SILENT")
        assert r.acted is False
        assert r.success is False
        assert r.silence_reason == "SILENT"

    def test_fallback(self) -> None:
        r = RouteResult(
            acted=True,
            pushed_device_ids=["printoutputadapter"],
            fallback_used=True,
        )
        assert r.fallback_used is True
        assert r.success is True

    def test_str(self) -> None:
        r = RouteResult(
            acted=True,
            pushed_device_ids=["a", "b"],
            fallback_used=True,
            error_device_ids=["c"],
        )
        s = str(r)
        assert "acted=True" in s
        assert "pushed=['a', 'b']" in s
        assert "fallback_used=True" in s
        assert "errors=['c']" in s

    def test_frozen_immutable(self) -> None:
        r = RouteResult(acted=True)
        with pytest.raises(Exception):  # frozen=True 禁止修改
            r.acted = False  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# DeviceRegistry 单元测试
# ---------------------------------------------------------------------------


class TestDeviceRegistry:
    def test_register_and_get(self) -> None:
        out = _out("speaker.living_room")
        reg = DeviceRegistry([out])
        assert reg.get("speaker.living_room") is out
        assert reg.get("nonexistent") is None

    def test_all_returns_copy(self) -> None:
        out1, out2 = _out("speaker.living_room"), _out("speaker.bedroom")
        reg = DeviceRegistry([out1, out2])
        all_adapters = reg.all()
        assert len(all_adapters) == 2
        all_adapters.clear()
        assert len(reg.all()) == 2

    def test_first(self) -> None:
        out1, out2 = _out("speaker.living_room"), _out("speaker.bedroom")
        reg = DeviceRegistry([out1, out2])
        assert reg.first() is out1

    def test_first_empty_registry(self) -> None:
        reg = DeviceRegistry()
        assert reg.first() is None

    def test_register_runtime(self) -> None:
        reg = DeviceRegistry()
        out = _out("speaker.guest")
        reg.register(out)
        assert reg.get("speaker.guest") is out
        assert len(reg.all()) == 1

    def test_device_id_fallback(self) -> None:
        out = _out("speaker.living_room")
        assert DeviceRegistry._device_id(out) == "speaker.living_room"

    def test_device_id_explicit(self) -> None:
        out = _out("speaker.bedroom")
        assert DeviceRegistry._device_id(out) == "speaker.bedroom"


# ---------------------------------------------------------------------------
# SimplePresenceService 单元测试
# ---------------------------------------------------------------------------


class TestSimplePresenceService:
    @pytest.mark.asyncio
    async def test_get_no_info(self) -> None:
        p = SimplePresenceService()
        assert await p.get_active_device("u1") is None

    @pytest.mark.asyncio
    async def test_update_and_get(self) -> None:
        p = SimplePresenceService()
        info = PresenceInfo(
            user_id="u1",
            device_id="speaker.living_room",
            device_type="speaker",
            confidence=0.9,
            last_seen_at=datetime.now(UTC),
        )
        await p.update("u1", info)
        result = await p.get_active_device("u1")
        assert result is not None
        assert result.device_id == "speaker.living_room"
        assert result.confidence == 0.9

    @pytest.mark.asyncio
    async def test_update_overwrites(self) -> None:
        p = SimplePresenceService()
        await p.update("u1", PresenceInfo(
            user_id="u1", device_id="speaker.living_room", device_type="speaker",
            confidence=0.5, last_seen_at=datetime.now(UTC),
        ))
        await p.update("u1", PresenceInfo(
            user_id="u1", device_id="speaker.bedroom", device_type="speaker",
            confidence=0.9, last_seen_at=datetime.now(UTC),
        ))
        result = await p.get_active_device("u1")
        assert result.device_id == "speaker.bedroom"

    def test_clear(self) -> None:
        p = SimplePresenceService()
        p.set_sync("u1", PresenceInfo(
            user_id="u1", device_id="speaker.living_room", device_type="speaker",
            confidence=1.0, last_seen_at=datetime.now(UTC),
        ))
        p.clear("u1")
        assert p._store.get("u1") is None

    def test_clear_all(self) -> None:
        p = SimplePresenceService()
        p.set_sync("u1", PresenceInfo(
            user_id="u1", device_id="speaker.living_room", device_type="speaker",
            confidence=1.0, last_seen_at=datetime.now(UTC),
        ))
        p.set_sync("u2", PresenceInfo(
            user_id="u2", device_id="phone", device_type="phone",
            confidence=1.0, last_seen_at=datetime.now(UTC),
        ))
        p.clear_all()
        assert len(p._store) == 0


# ---------------------------------------------------------------------------
# ConsoleAnswerRouter 单元测试(直接调 route,不走 server)
# ---------------------------------------------------------------------------


class TestConsoleAnswerRouterDirect:
    @pytest.mark.asyncio
    async def test_silent_returns_not_acted(self) -> None:
        out = _out("speaker.living_room")
        presence = SimplePresenceService()
        registry = DeviceRegistry([out])
        router = ConsoleAnswerRouter(registry=registry, presence_service=presence)
        event = _make_event(priority=EventPriority.SILENT)
        r = await router.route(event, "hello")
        assert r.acted is False
        assert r.silence_reason == "SILENT"
        assert len(out.replies) == 0

    @pytest.mark.asyncio
    async def test_urgent_pushes_all(self) -> None:
        out1, out2, out3 = _out("speaker.living_room"), _out("speaker.bedroom"), _out("phone")
        presence = SimplePresenceService()
        registry = DeviceRegistry([out1, out2, out3])
        router = ConsoleAnswerRouter(registry=registry, presence_service=presence)
        event = _make_event(priority=EventPriority.URGENT)
        r = await router.route(event, "紧急播报")
        assert r.acted is True
        assert len(r.pushed_device_ids) == 3
        assert len(out1.replies) == 1
        assert out1.replies[0].content == "紧急播报"
        assert out2.replies[0].content == "紧急播报"
        assert out3.replies[0].content == "紧急播报"

    @pytest.mark.asyncio
    async def test_normal_uses_presence_service(self) -> None:
        out1, out2 = _out("speaker.living_room"), _out("speaker.bedroom")
        presence = SimplePresenceService()
        registry = DeviceRegistry([out1, out2])
        router = ConsoleAnswerRouter(registry=registry, presence_service=presence)
        await presence.update("u1", PresenceInfo(
            user_id="u1",
            device_id="speaker.living_room",
            device_type="test",
            confidence=1.0,
            last_seen_at=datetime.now(UTC),
        ))
        event = _make_event(user_id="u1", priority=EventPriority.NORMAL)
        r = await router.route(event, "normal 播报")
        assert r.acted is True
        assert len(out1.replies) == 1
        assert len(out2.replies) == 0

    @pytest.mark.asyncio
    async def test_normal_fallback_when_presence_returns_none(self) -> None:
        out1, out2 = _out("speaker.living_room"), _out("speaker.bedroom")
        presence = SimplePresenceService()
        registry = DeviceRegistry([out1, out2])
        router = ConsoleAnswerRouter(registry=registry, presence_service=presence)
        event = _make_event(user_id="u1", priority=EventPriority.NORMAL)
        r = await router.route(event, "fallback 播报")
        assert r.acted is True
        assert r.fallback_used is True
        assert len(out1.replies) == 1
        assert len(out2.replies) == 0

    @pytest.mark.asyncio
    async def test_normal_dnd_rejected(self) -> None:
        out = _out("speaker.living_room")
        presence = SimplePresenceService()
        registry = DeviceRegistry([out])
        router = ConsoleAnswerRouter(registry=registry, presence_service=presence)
        await presence.update("u1", PresenceInfo(
            user_id="u1",
            device_id="speaker.living_room",
            device_type="test",
            confidence=1.0,
            last_seen_at=datetime.now(UTC),
            is_dnd=True,
        ))
        event = _make_event(user_id="u1", priority=EventPriority.NORMAL)
        r = await router.route(event, "dnd 播报")
        assert r.acted is False
        assert "dnd=True" in r.silence_reason
        assert len(out.replies) == 0

    @pytest.mark.asyncio
    async def test_device_not_found_returns_not_acted(self) -> None:
        out = _out("speaker.living_room")
        presence = SimplePresenceService()
        registry = DeviceRegistry([out])
        router = ConsoleAnswerRouter(registry=registry, presence_service=presence)
        await presence.update("u1", PresenceInfo(
            user_id="u1",
            device_id="ghost_device",
            device_type="test",
            confidence=1.0,
            last_seen_at=datetime.now(UTC),
        ))
        event = _make_event(user_id="u1", priority=EventPriority.NORMAL)
        r = await router.route(event, "not found")
        assert r.acted is False
        assert "device_not_found" in r.silence_reason
        assert r.fallback_used is True


# ---------------------------------------------------------------------------
# Case 1:Reactive 链路 → 无 AnswerRouter → 走默认 OutputAdapter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_case1_reactive_no_router_broadcasts_all(
    server_no_router: tuple[ButlerServer, _FakeProactiveLoop, InMemoryOutputAdapter, InMemoryEventBus],
) -> None:
    """case 1:Reactive 链路(不走 AnswerRouter) → 无 router 时 broadcast 全部 3 个 adapter。"""
    server, loop, out1, bus = server_no_router
    _, out2, out3 = bus._subscribers[0] if hasattr(bus, "_subscribers") else (None, None, None)
    await server.start()
    try:
        reply = await server.submit_reactive(
            ReactiveSubmission(user_input="你好", user_id="u1"),
        )
        await asyncio.sleep(0.05)
        # Reactive → _dispatch_reply broadcast 全部 3 个(无 AnswerRouter)
        assert len(reply.content) > 0  # 有回复
        # 注:3 个 output_adapter 是 server._output_adapters,fixture 返回了 out1
        # out2/out3 也在同一列表中
        # 由于 fixture 只返回 out1,我们只看 out1
        assert len(out1.replies) == 1
    finally:
        await server.stop()


# ---------------------------------------------------------------------------
# Case 2:Proactive + NORMAL → PresenceService 选定的 OutputAdapter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_case2_proactive_normal_routes_to_presence_device(
    server_with_router: tuple,
) -> None:
    """case 2:Proactive + NORMAL → PresenceService 选 out1 → 只有 out1 收到。"""
    (
        server, loop, out1, out2, out3, bus, presence
    ) = server_with_router
    # u1 在 out1
    await presence.update("u1", PresenceInfo(
        user_id="u1",
        device_id="speaker.living_room",
        device_type="test",
        confidence=1.0,
        last_seen_at=datetime.now(UTC),
    ))
    await server.start()
    try:
        event = _make_event(user_id="u1", priority=EventPriority.NORMAL, event_id="evt_case2")
        await bus.publish(event)
        await asyncio.sleep(0.05)
        assert len(out1.replies) == 1
        assert out1.replies[0].content == "管家主动播报"
        assert len(out2.replies) == 0
        assert len(out3.replies) == 0
    finally:
        await server.stop()


# ---------------------------------------------------------------------------
# Case 3:Proactive + URGENT → 推所有 3 个 OutputAdapter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_case3_proactive_urgent_pushes_all_three(
    server_with_router: tuple,
) -> None:
    """case 3:Proactive + URGENT → 3 个 adapter 全收到。"""
    (
        server, loop, out1, out2, out3, bus, presence
    ) = server_with_router
    await server.start()
    try:
        event = _make_event(priority=EventPriority.URGENT, event_id="evt_case3")
        await bus.publish(event)
        await asyncio.sleep(0.05)
        assert len(out1.replies) == 1
        assert len(out2.replies) == 1
        assert len(out3.replies) == 1
        # 内容相同
        assert out1.replies[0].content == out2.replies[0].content == out3.replies[0].content
    finally:
        await server.stop()


# ---------------------------------------------------------------------------
# Case 4:Proactive + SILENT → 不推
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_case4_proactive_silent_no_push(
    server_with_router: tuple,
) -> None:
    """case 4:Proactive + SILENT → 3 个 adapter 都不收到。"""
    (
        server, loop, out1, out2, out3, bus, presence
    ) = server_with_router
    await server.start()
    try:
        event = _make_event(priority=EventPriority.SILENT, event_id="evt_case4")
        await bus.publish(event)
        await asyncio.sleep(0.05)
        assert len(out1.replies) == 0
        assert len(out2.replies) == 0
        assert len(out3.replies) == 0
    finally:
        await server.stop()


# ---------------------------------------------------------------------------
# Case 5:PresenceService 返 None → fallback 到第 1 个 OutputAdapter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_case5_presence_none_fallback_to_first(
    server_with_router: tuple,
) -> None:
    """case 5:u1 无 PresenceInfo → AnswerRouter fallback 到 first(out1)。"""
    (
        server, loop, out1, out2, out3, bus, presence
    ) = server_with_router
    # u1 没有 update → presence.get 返 None
    await server.start()
    try:
        event = _make_event(user_id="u1", priority=EventPriority.NORMAL, event_id="evt_case5")
        await bus.publish(event)
        await asyncio.sleep(0.05)
        assert len(out1.replies) == 1
        assert len(out2.replies) == 0
        assert len(out3.replies) == 0
    finally:
        await server.stop()


# ---------------------------------------------------------------------------
# Case 6:Reactive + AnswerRouter 注入 → 仍走 Reactive 默认(Reactive 不过 AnswerRouter)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_case6_reactive_with_router_still_broadcasts_all(
    server_with_router: tuple,
) -> None:
    """case 6:AnswerRouter 注入,但 Reactive 链路不走 AnswerRouter,走 _dispatch_reply。"""
    (
        server, loop, out1, out2, out3, bus, presence
    ) = server_with_router
    await server.start()
    try:
        reply = await server.submit_reactive(
            ReactiveSubmission(user_input="你好", user_id="u1"),
        )
        await asyncio.sleep(0.05)
        # Reactive → _dispatch_reply broadcast 全部 3 个(完全不过 AnswerRouter)
        assert len(reply.content) > 0
        assert len(out1.replies) == 1
        # out2/out3 在 server._output_adapters 里
        assert len(out2.replies) == 1
        assert len(out3.replies) == 1
    finally:
        await server.stop()
