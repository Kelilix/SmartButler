"""ProactiveLoop 单元测试。"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from langchain_core.messages import AIMessage

from smartbutler.events.core.event import BaseEvent, EventPriority, EventSource
from smartbutler.thinking.loop.proactive_loop import ProactiveLoop
from smartbutler.thinking.proactive import (
    ProactiveDecision,
    ProactiveUrgency,
)


def _make_event(
    *,
    priority: EventPriority = EventPriority.NORMAL,
    topic: str = "device.door.opened",
) -> BaseEvent:
    return BaseEvent(
        source=EventSource.DEVICE,
        topic=topic,
        user_id="alice",
        timestamp=datetime.now(UTC),
        priority=priority,
    )


class _FakeGraph:
    """替换 CompiledStateGraph 的最小 stub。"""

    def __init__(self, scripted_response: str = "建议:做西红柿炒蛋") -> None:
        self._response = scripted_response
        self.invocations: list[dict[str, Any]] = []

    async def ainvoke(
        self,
        input: dict[str, Any],
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.invocations.append({"input": input, "config": config})
        new_messages = list(input["messages"]) + [
            AIMessage(content=self._response),
        ]
        return {**input, "messages": new_messages}


class _FakeReasoning:
    """可注入的 ProactiveReasoning stub。"""

    def __init__(self, decision: ProactiveDecision) -> None:
        self._decision = decision
        self.calls: list[BaseEvent] = []

    async def should_respond(
        self,
        event: BaseEvent,
        *,
        now: datetime | None = None,
    ) -> ProactiveDecision:
        self.calls.append(event)
        return self._decision


class TestProactiveLoopSilent:
    @pytest.mark.asyncio
    async def test_silent_decision_returns_silent(self) -> None:
        fake_reasoning = _FakeReasoning(
            ProactiveDecision.silent("default_silent"),
        )
        loop = ProactiveLoop(reasoning=fake_reasoning, graph=_FakeGraph())
        result = await loop.tick(_make_event())
        assert result.acted is False
        assert result.silence_reason == "default_silent"
        assert result.related_event_id  # 透传 event_id

    @pytest.mark.asyncio
    async def test_silent_event_priority(self) -> None:
        fake_reasoning = _FakeReasoning(
            ProactiveDecision.silent("event_priority_silent"),
        )
        loop = ProactiveLoop(reasoning=fake_reasoning, graph=_FakeGraph())
        result = await loop.tick(_make_event(priority=EventPriority.SILENT))
        assert result.acted is False
        assert result.silence_reason == "event_priority_silent"


class TestProactiveLoopActed:
    @pytest.mark.asyncio
    async def test_acted_decision_with_graph(self) -> None:
        fake_graph = _FakeGraph("建议:冰箱有西红柿,做西红柿炒蛋吧")
        fake_reasoning = _FakeReasoning(
            ProactiveDecision(
                should_respond=True,
                reason="user_asked",
                urgency=ProactiveUrgency.LOW,
            ),
        )
        loop = ProactiveLoop(reasoning=fake_reasoning, graph=fake_graph)
        result = await loop.tick(_make_event())
        assert result.acted is True
        assert result.message == "建议:冰箱有西红柿,做西红柿炒蛋吧"
        assert result.urgency == ProactiveUrgency.LOW
        assert result.related_event_id
        # 验证 graph 被调用
        assert len(fake_graph.invocations) == 1

    @pytest.mark.asyncio
    async def test_acted_empty_message_silenced(self) -> None:
        """ReAct 生成空消息 → 降级 silent。"""
        fake_graph = _FakeGraph("")  # 空响应
        fake_reasoning = _FakeReasoning(
            ProactiveDecision(should_respond=True, reason="x"),
        )
        loop = ProactiveLoop(reasoning=fake_reasoning, graph=fake_graph)
        result = await loop.tick(_make_event())
        assert result.acted is False
        assert result.silence_reason == "empty_message_generated"


class TestProactiveLoopNoGraph:
    @pytest.mark.asyncio
    async def test_no_graph_falls_back_silent(self) -> None:
        """没有 graph 时 → silent(占位实现)。"""
        fake_reasoning = _FakeReasoning(
            ProactiveDecision(should_respond=True, reason="x"),
        )
        loop = ProactiveLoop(reasoning=fake_reasoning, graph=None)
        result = await loop.tick(_make_event())
        assert result.acted is False
        assert result.silence_reason == "no_graph_configured"


class TestProactiveLoopErrorFallback:
    @pytest.mark.asyncio
    async def test_reasoning_error_falls_back_silent(self) -> None:
        """reasoning 内部异常 → silent(永不抛异常给上游)。"""

        class _BrokenReasoning:
            async def should_respond(
                self,
                event: BaseEvent,
                *,
                now: datetime | None = None,
            ) -> ProactiveDecision:
                msg = "boom"
                raise RuntimeError(msg)

        loop = ProactiveLoop(reasoning=_BrokenReasoning(), graph=_FakeGraph())
        result = await loop.tick(_make_event())
        assert result.acted is False
        assert result.silence_reason is not None
        assert result.silence_reason.startswith("error_fallback:")

    @pytest.mark.asyncio
    async def test_graph_error_falls_back_silent(self) -> None:
        """graph 内部异常 → silent。"""

        class _BrokenGraph:
            async def ainvoke(
                self,
                input: dict[str, Any],
                config: dict[str, Any] | None = None,
            ) -> dict[str, Any]:
                msg = "graph boom"
                raise RuntimeError(msg)

        fake_reasoning = _FakeReasoning(
            ProactiveDecision(should_respond=True, reason="x"),
        )
        loop = ProactiveLoop(reasoning=fake_reasoning, graph=_BrokenGraph())
        result = await loop.tick(_make_event())
        assert result.acted is False
        assert result.silence_reason is not None
        assert result.silence_reason.startswith("error_fallback:")


class TestProactiveLoopProtocol:
    def test_loop_type(self) -> None:
        loop = ProactiveLoop(reasoning=_FakeReasoning(ProactiveDecision.silent()))
        from smartbutler.thinking.proactive import LoopType

        assert loop.loop_type == LoopType.PROACTIVE

    def test_get_tools_empty_without_graph(self) -> None:
        loop = ProactiveLoop(reasoning=_FakeReasoning(ProactiveDecision.silent()))
        assert loop.get_tools() == []


class TestProactiveLoopAdvise:
    @pytest.mark.asyncio
    async def test_advise_returns_none_phase5_placeholder(self) -> None:
        """Phase 5 占位:advise 永远返 None(等 Phase 6 emotion 上线)。"""
        loop = ProactiveLoop(
            reasoning=_FakeReasoning(ProactiveDecision.silent()),
            graph=_FakeGraph(),
        )
        result = await loop.advise(
            context="我该吃啥", current_answer="随便"
        )
        assert result is None
