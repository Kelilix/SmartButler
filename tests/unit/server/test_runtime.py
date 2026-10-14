"""server 子包单元测试 (Phase 7.1)。

覆盖:
1. InputAdapter / OutputAdapter 抽象数据类
2. InMemoryInputAdapter / InMemoryOutputAdapter 行为
3. InMemoryEventBus topic 匹配 / publish / subscribe / replay
4. ButlerServer.submit_reactive / submit_proactive / start / stop
5. EventBus 异常不挂 server
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from datetime import UTC, datetime
from langchain_core.messages import AIMessage

from smartbutler.events.core.event import BaseEvent, EventPriority, EventSource
from smartbutler.server.adapters import (
    ButlerReply,
    InputAdapter,
    OutputAdapter,
    ProactiveSubmission,
    ReactiveSubmission,
)
from smartbutler.server.adapters.stub import (
    InMemoryInputAdapter,
    InMemoryOutputAdapter,
    PrintOutputAdapter,
)
from smartbutler.server.bus import InMemoryEventBus, _topic_matches
from smartbutler.server.runtime import ButlerServer, ServerConfig, build_default_server
from smartbutler.thinking.loop.orchestrator import ButlerOrchestrator


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class _FakeBaseLLM:
    def __init__(self) -> None:
        self.closed = False

    async def chat(self, messages: list[Any], **kwargs: Any) -> Any:  # noqa: ARG002
        return None

    async def chat_stream(  # noqa: ARG002
        self, messages: list[Any], **kwargs: Any
    ) -> AsyncIterator[Any]:
        return
        yield  # type: ignore[unreachable]

    async def aclose(self) -> None:
        self.closed = True


class _FakeSettings:
    provider = "openai"
    openai_api_key = "sk-fake"
    openai_base_url = "https://api.openai.com/v1"
    model = "gpt-4o-mini"
    temperature = 0.7
    max_tokens = 1024
    timeout = 60.0
    max_retries = 2


class _FakeCompiledGraph:
    """inject into ButlerOrchestrator 拦截 ainvoke/proactive_tick 的图。"""

    def __init__(self, *, ai_content: str = "管家回复") -> None:
        self.ai_content = ai_content
        self.invocations: list[dict[str, Any]] = []

    async def ainvoke(
        self, state: dict[str, Any], config: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        self.invocations.append({"state": state, "config": config})
        new_messages = list(state["messages"])
        new_messages.append(AIMessage(content=self.ai_content))
        return {**state, "messages": new_messages, "iteration_count": 1}


def _make_orchestrator(ai_content: str = "管家回复") -> tuple[ButlerOrchestrator, _FakeCompiledGraph]:
    """构造 ButlerOrchestrator + 拦截 compiled。

    不走 LangGraph 真实编译,直接 _compiled = fake。
    """
    compiled = _FakeCompiledGraph(ai_content=ai_content)
    orch = ButlerOrchestrator(
        llm=_FakeBaseLLM(),  # type: ignore[arg-type]
        llm_settings=_FakeSettings(),
    )
    orch._compiled = compiled  # type: ignore[attr-defined]
    return orch, compiled


# ---------------------------------------------------------------------------
# 1. 数据类
# ---------------------------------------------------------------------------


class TestSubmissionDataclasses:
    def test_reactive_submission_defaults(self) -> None:
        sub = ReactiveSubmission(user_input="hi")
        assert sub.user_input == "hi"
        assert sub.user_id == "user"
        assert sub.session_id == "default"
        assert sub.parent_agent == "user"
        assert sub.spoken_at is None
        assert sub.metadata == {}

    def test_reactive_submission_frozen(self) -> None:
        sub = ReactiveSubmission(user_input="hi")
        with pytest.raises((AttributeError, Exception)):
            sub.user_input = "other"  # type: ignore[misc]

    def test_proactive_submission_defaults(self) -> None:
        sub = ProactiveSubmission(topic="device.door.opened")
        assert sub.topic == "device.door.opened"
        assert sub.user_id == "user"
        assert sub.source == "device"
        assert sub.priority == "normal"
        assert sub.device_id is None
        assert sub.payload == {}

    def test_butler_reply_defaults(self) -> None:
        reply = ButlerReply(content="ok")
        assert reply.content == "ok"
        assert reply.source == "reactive"
        assert reply.related_event_id is None
        assert reply.metadata == {}


# ---------------------------------------------------------------------------
# 2. InMemoryInputAdapter / InMemoryOutputAdapter
# ---------------------------------------------------------------------------


class TestInMemoryAdapters:
    @pytest.mark.asyncio
    async def test_enqueue_reactive_consumed(self) -> None:
        orch, compiled = _make_orchestrator(ai_content="hi back")
        server = build_default_server(orchestrator=orch)
        await server.start()
        try:
            input_adapter = InMemoryInputAdapter()
            input_adapter.set_sink(
                submit_reactive=server.submit_reactive,
                submit_proactive=server._on_proactive_from_adapter,  # type: ignore[arg-type]
            )
            await input_adapter.start()
            try:
                await input_adapter.enqueue_reactive(
                    ReactiveSubmission(user_input="hello", user_id="u1")
                )
                # 给 consumer 一点时间跑
                import asyncio

                await asyncio.sleep(0.05)
            finally:
                await input_adapter.stop()
            # consumer 跑了一次 ainvoke
            assert len(compiled.invocations) == 1
            state = compiled.invocations[0]["state"]
            assert state["user_id"] == "u1"
        finally:
            await server.stop()

    @pytest.mark.asyncio
    async def test_send_now_returns_reply(self) -> None:
        orch, _ = _make_orchestrator(ai_content="立即答复")
        server = build_default_server(orchestrator=orch)
        await server.start()
        try:
            input_adapter = InMemoryInputAdapter()
            input_adapter.set_sink(
                submit_reactive=server.submit_reactive,
                submit_proactive=server._on_proactive_from_adapter,  # type: ignore[arg-type]
            )
            reply = await input_adapter.send_now(
                ReactiveSubmission(user_input="hi", user_id="u1")
            )
            assert reply.content == "立即答复"
            assert reply.user_id == "u1"
            assert reply.source == "reactive"
        finally:
            await server.stop()

    @pytest.mark.asyncio
    async def test_output_adapter_records_replies(self) -> None:
        orch, _ = _make_orchestrator(ai_content="答复1")
        output = InMemoryOutputAdapter()
        server = build_default_server(orchestrator=orch, output_adapters=[output])
        await server.start()
        try:
            await server.submit_reactive(
                ReactiveSubmission(user_input="hi", user_id="u1")
            )
            assert len(output.replies) == 1
            assert output.replies[0].content == "答复1"
        finally:
            await server.stop()


# ---------------------------------------------------------------------------
# 3. InMemoryEventBus topic 匹配
# ---------------------------------------------------------------------------


class TestTopicMatches:
    def test_exact_match(self) -> None:
        assert _topic_matches("device.door", "device.door") is True

    def test_prefix_match_two_segments(self) -> None:
        # "device.door" → 匹配 "device.door.opened"
        assert _topic_matches("device.door", "device.door.opened") is True

    def test_prefix_no_match(self) -> None:
        assert _topic_matches("device.door", "device.window.opened") is False

    def test_wildcard_match(self) -> None:
        assert _topic_matches("device.*", "device.door") is True
        assert _topic_matches("device.*", "device.door.opened") is True
        assert _topic_matches("device.*", "timer.tick") is False

    def test_no_wildcard_strict(self) -> None:
        assert _topic_matches("device.door", "device.doorbell") is False


class TestInMemoryEventBus:
    @pytest.mark.asyncio
    async def test_publish_requires_start(self) -> None:
        bus = InMemoryEventBus()
        event = BaseEvent(
            event_id="e1",
            source=EventSource.DEVICE,
            topic="device.door",
            user_id="u1",
            timestamp=datetime.now(UTC),
            payload={},
        )
        with pytest.raises(Exception):
            await bus.publish(event)

    @pytest.mark.asyncio
    async def test_publish_consume(self) -> None:
        bus = InMemoryEventBus()
        await bus.start()
        try:
            received: list[BaseEvent] = []

            async def handler(evt: BaseEvent) -> None:
                received.append(evt)

            await bus.subscribe("device.*", handler)
            event = BaseEvent(
                event_id="e1",
                source=EventSource.DEVICE,
                topic="device.door.opened",
                user_id="u1",
                timestamp=datetime.now(UTC),
                payload={},
            )
            await bus.publish(event)
            # 等 consumer 拉
            import asyncio

            await asyncio.sleep(0.05)
            assert len(received) == 1
            assert received[0].event_id == "e1"
        finally:
            await bus.stop()

    @pytest.mark.asyncio
    async def test_handler_error_does_not_kill_consumer(self) -> None:
        bus = InMemoryEventBus()
        await bus.start()
        try:
            received: list[BaseEvent] = []

            async def bad_handler(evt: BaseEvent) -> None:
                raise RuntimeError("boom")

            async def good_handler(evt: BaseEvent) -> None:
                received.append(evt)

            await bus.subscribe("device.*", bad_handler)
            await bus.subscribe("device.*", good_handler)
            event = BaseEvent(
                event_id="e1",
                source=EventSource.DEVICE,
                topic="device.door",
                user_id="u1",
                timestamp=datetime.now(UTC),
                payload={},
            )
            await bus.publish(event)
            import asyncio

            await asyncio.sleep(0.05)
            # good 仍收到,bad 抛了但没影响其他订阅
            assert len(received) == 1
        finally:
            await bus.stop()

    @pytest.mark.asyncio
    async def test_replay(self) -> None:
        bus = InMemoryEventBus()
        await bus.start()
        try:
            for i in range(5):
                await bus.publish(
                    BaseEvent(
                        event_id=f"e{i}",
                        source=EventSource.DEVICE,
                        topic="device.door" if i % 2 == 0 else "timer.tick",
                        user_id="u1",
                        timestamp=datetime.now(UTC),
                        payload={},
                    )
                )
            # 收一下 queue(避免 history 干扰)
            import asyncio

            await asyncio.sleep(0.05)
            events = [e async for e in bus.replay("device.door", limit=10)]
            # device.door 那几条都在
            assert len(events) == 3
            assert all(e.topic == "device.door" for e in events)
        finally:
            await bus.stop()

    @pytest.mark.asyncio
    async def test_start_stop_idempotent(self) -> None:
        bus = InMemoryEventBus()
        await bus.start()
        await bus.start()  # 幂等
        await bus.stop()
        await bus.stop()  # 幂等

    @pytest.mark.asyncio
    async def test_subscribe_requires_start(self) -> None:
        bus = InMemoryEventBus()

        async def handler(evt: BaseEvent) -> None:
            pass

        with pytest.raises(Exception):
            await bus.subscribe("*", handler)


# ---------------------------------------------------------------------------
# 4. ButlerServer 行为
# ---------------------------------------------------------------------------


class TestButlerServer:
    @pytest.mark.asyncio
    async def test_submit_reactive_returns_reply(self) -> None:
        orch, compiled = _make_orchestrator(ai_content="你好呀")
        output = InMemoryOutputAdapter()
        server = build_default_server(orchestrator=orch, output_adapters=[output])
        await server.start()
        try:
            reply = await server.submit_reactive(
                ReactiveSubmission(user_input="hi", user_id="alice", session_id="s-1")
            )
            assert reply.content == "你好呀"
            assert reply.user_id == "alice"
            assert reply.session_id == "s-1"
            assert reply.source == "reactive"
            # 也下发给了 output_adapter
            assert len(output.replies) == 1
            assert output.replies[0].content == "你好呀"
        finally:
            await server.stop()

    @pytest.mark.asyncio
    async def test_submit_reactive_empty_input(self) -> None:
        orch, compiled = _make_orchestrator(ai_content="不会用到")
        server = build_default_server(orchestrator=orch)
        await server.start()
        try:
            reply = await server.submit_reactive(ReactiveSubmission(user_input=""))
            assert reply.content == ""
            # 不会真触发 ainvoke
            assert len(compiled.invocations) == 0
        finally:
            await server.stop()

    @pytest.mark.asyncio
    async def test_submit_proactive_publishes_to_eventbus(self) -> None:
        orch, _ = _make_orchestrator()
        output = InMemoryOutputAdapter()
        server = build_default_server(orchestrator=orch, output_adapters=[output])
        await server.start()
        try:
            await server.submit_proactive(
                ProactiveSubmission(topic="device.door.opened", user_id="u1")
            )
            import asyncio

            await asyncio.sleep(0.05)
            # ProactiveLoop 默认 silent(我们没给它真 graph 之外的设置)
            # 所以不应有答复
            assert len(output.replies) == 0
        finally:
            await server.stop()

    @pytest.mark.asyncio
    async def test_start_stop_idempotent(self) -> None:
        orch, _ = _make_orchestrator()
        server = build_default_server(orchestrator=orch)
        await server.start()
        await server.start()  # 幂等
        await server.stop()
        await server.stop()  # 幂等

    @pytest.mark.asyncio
    async def test_submit_reactive_exception_does_not_crash(self) -> None:
        """ainvoke 抛异常时,server 仍能给出 error 答复(不挂)。"""
        orch, _ = _make_orchestrator()
        # 把 ainvoke 替换成抛异常的版本
        async def _boom(*args: Any, **kwargs: Any) -> str:
            raise RuntimeError("simulated LLM down")

        orch.ainvoke = _boom  # type: ignore[method-assign]
        output = InMemoryOutputAdapter()
        server = build_default_server(orchestrator=orch, output_adapters=[output])
        await server.start()
        try:
            reply = await server.submit_reactive(
                ReactiveSubmission(user_input="hi", user_id="u1")
            )
            assert "[error]" in reply.content
            assert "simulated LLM down" in reply.content
            # 也下发给 output
            assert len(output.replies) == 1
        finally:
            await server.stop()

    @pytest.mark.asyncio
    async def test_output_adapter_error_does_not_crash_server(self) -> None:
        """output_adapter.push 抛异常时,其他 adapter 仍能收到。"""
        orch, _ = _make_orchestrator(ai_content="hi")

        class BadOutput(OutputAdapter):
            async def push(self, reply: ButlerReply) -> None:
                raise RuntimeError("bad adapter")

        good = InMemoryOutputAdapter()
        server = build_default_server(
            orchestrator=orch, output_adapters=[BadOutput(), good]
        )
        await server.start()
        try:
            reply = await server.submit_reactive(
                ReactiveSubmission(user_input="hi", user_id="u1")
            )
            # reply 仍正常
            assert reply.content == "hi"
            # good 仍收到
            assert len(good.replies) == 1
        finally:
            await server.stop()

    @pytest.mark.asyncio
    async def test_add_output_at_runtime(self) -> None:
        orch, _ = _make_orchestrator(ai_content="hi")
        server = build_default_server(orchestrator=orch)
        await server.start()
        try:
            new_out = InMemoryOutputAdapter()
            server.add_output(new_out)
            await server.submit_reactive(ReactiveSubmission(user_input="hi"))
            assert len(new_out.replies) == 1
        finally:
            await server.stop()


# ---------------------------------------------------------------------------
# 5. ServerConfig 边界
# ---------------------------------------------------------------------------


class TestServerConfig:
    def test_default_config(self) -> None:
        cfg = ServerConfig()
        assert cfg.event_bus is None
        assert cfg.enable_event_bus is True
        assert cfg.auto_subscribe_proactive is True

    def test_custom_event_bus(self) -> None:
        bus = InMemoryEventBus()
        cfg = ServerConfig(event_bus=bus, enable_event_bus=False)
        assert cfg.event_bus is bus
        assert cfg.enable_event_bus is False


# ---------------------------------------------------------------------------
# 6. PrintOutputAdapter
# ---------------------------------------------------------------------------


class TestPrintOutputAdapter:
    @pytest.mark.asyncio
    async def test_push_writes_to_stream(self) -> None:
        import io

        stream = io.StringIO()
        ad = PrintOutputAdapter(stream=stream)
        reply = ButlerReply(content="hi", user_id="u1")
        await ad.push(reply)
        out = stream.getvalue()
        assert "hi" in out
        assert "u1" in out

    @pytest.mark.asyncio
    async def test_push_handles_exception(self) -> None:
        """stream 抛异常时 adapter 内部不抛(吞掉)。"""

        class BrokenStream:
            def write(self, s: str) -> int:
                raise RuntimeError("stream broken")

            def flush(self) -> None:
                raise RuntimeError("stream broken")

        ad = PrintOutputAdapter(stream=BrokenStream())  # type: ignore[arg-type]
        reply = ButlerReply(content="hi", user_id="u1")
        # 不抛
        await ad.push(reply)
