"""ButlerOrchestrator 的 Proactive 集成测试(参考 ADR-009)。

测试范围:
1. ``_should_request_proactive`` 规则匹配
2. ``proactive_tick`` 入口(默认 silent)
3. Reactive → Proactive 内部通道(``ainvoke`` 链尾挂载点)
4. ``enable_advice=False`` 时不影响现有 ainvoke 行为
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest
from langchain_core.messages import AIMessage
from langchain_core.tools import StructuredTool
from pydantic import BaseModel

from smartbutler.events.core.event import BaseEvent, EventPriority, EventSource
from smartbutler.thinking.loop.orchestrator import (
    ButlerOrchestrator,
    _should_request_proactive,
)


# ---------- _should_request_proactive 规则测试 ----------


class TestShouldRequestProactive:
    @pytest.mark.parametrize(
        "msg",
        [
            "该吃什么",
            "该做什么",
            "该喝什么",
            "吃什么好",
            "做什么菜",
            "喝什么茶",
            "怎么办",
            "我该怎么做",
        ],
    )
    def test_trigger_prefixes(self, msg: str) -> None:
        assert _should_request_proactive(msg) is True

    @pytest.mark.parametrize(
        "msg",
        [
            "现在几点",
            "今天天气",
            "你好",
            "帮我看下股票",
            "灯关了没",
        ],
    )
    def test_non_trigger(self, msg: str) -> None:
        assert _should_request_proactive(msg) is False

    @pytest.mark.parametrize("msg", ["", "   ", None])
    def test_empty(self, msg: Any) -> None:
        assert _should_request_proactive(msg) is False  # type: ignore[arg-type]


# ---------- ButlerOrchestrator 集成测试 ----------


class _NoArgs(BaseModel):
    pass


def _dummy_tool() -> StructuredTool:
    async def _call() -> str:
        return "ok"

    return StructuredTool.from_function(
        coroutine=_call,
        name="dummy",
        description="test",
        args_schema=_NoArgs,
    )


class _FakeBaseLLM:
    def __init__(self) -> None:
        self.closed = False

    async def chat(self, messages: list[Any], **kwargs: Any) -> Any:  # noqa: ARG002
        return None

    async def chat_stream(  # noqa: ARG002
        self,
        messages: list[Any],
        **kwargs: Any,
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
    def __init__(self, scripted_response: str = "管家回复: ok") -> None:
        self._response = scripted_response
        self.invocations: list[dict[str, Any]] = []

    async def ainvoke(
        self,
        state: dict[str, Any],
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.invocations.append({"state": state, "config": config})
        new_messages = list(state["messages"])
        new_messages.append(AIMessage(content=self._response))
        return {**state, "messages": new_messages, "iteration_count": 1}


def _make_orch(*, enable_advice: bool = False) -> tuple[ButlerOrchestrator, _FakeCompiledGraph]:
    compiled = _FakeCompiledGraph()
    orch = ButlerOrchestrator(
        llm=_FakeBaseLLM(),  # type: ignore[arg-type]
        llm_settings=_FakeSettings(),
        enable_proactive_advice=enable_advice,
    )
    orch._compiled = compiled  # type: ignore[attr-defined]
    return orch, compiled


def _make_event() -> BaseEvent:
    return BaseEvent(
        source=EventSource.DEVICE,
        topic="device.door.opened",
        user_id="alice",
        timestamp=datetime.now(UTC),
        priority=EventPriority.NORMAL,
    )


class TestProactiveTick:
    @pytest.mark.asyncio
    async def test_default_silent(self) -> None:
        """NORMAL 优先级 + 默认 RuleBasedProactiveReasoning → silent。"""
        # 不注入 _compiled,让 proactive_tick 看到 graph=None
        orch = ButlerOrchestrator(
            llm=_FakeBaseLLM(),  # type: ignore[arg-type]
            llm_settings=_FakeSettings(),
        )
        # _compiled 不注入 → 保持 None → ProactiveLoop 拿到 graph=None
        result = await orch.proactive_tick(_make_event())
        assert result.acted is False
        # 没 graph 也 silent:reason 是 no_graph_configured
        # 有 graph 但 default_silent:reason 是 default_silent
        # 都满足 acted=False
        assert result.silence_reason in ("no_graph_configured", "default_silent")

    @pytest.mark.asyncio
    async def test_uses_event_user_id(self) -> None:
        orch = ButlerOrchestrator(
            llm=_FakeBaseLLM(),  # type: ignore[arg-type]
            llm_settings=_FakeSettings(),
        )
        event = _make_event()
        result = await orch.proactive_tick(event)
        assert result.related_event_id == event.event_id


class TestAinvokeBackwardCompat:
    @pytest.mark.asyncio
    async def test_default_no_advice_no_change(self) -> None:
        """默认 enable_proactive_advice=False → ainvoke 行为完全不变。"""
        orch, compiled = _make_orch(enable_advice=False)
        # 触发型用户消息,但因为 enable_advice=False,不应追加
        result = await orch.ainvoke("该吃什么")
        assert result == "管家回复: ok"
        # 验证 graph 只被调一次(没调 ProactiveLoop 二次 ainvoke)
        assert len(compiled.invocations) == 1

    @pytest.mark.asyncio
    async def test_non_trigger_no_advice(self) -> None:
        """enable_advice=True 但消息非触发型 → 不追加。"""
        orch, compiled = _make_orch(enable_advice=True)
        result = await orch.ainvoke("现在几点")
        assert result == "管家回复: ok"
        assert len(compiled.invocations) == 1

    @pytest.mark.asyncio
    async def test_explicit_enable_advice_override(self) -> None:
        """构造时 False,调用时显式 True → 触发 Proactive 检查(默认 silent)。"""
        orch, _ = _make_orch(enable_advice=False)
        # 触发型 + enable_advice=True → 调 Proactive,但默认 silent → 不追加
        result = await orch.ainvoke("该吃什么", enable_advice=True)
        # Proactive 默认 silent,无 advice 追加 → 行为同无 advice
        assert result == "管家回复: ok"
