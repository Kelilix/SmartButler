"""Phase 3 agent 单元测试 — 公共 conftest。"""
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from smartbutler.capabilities.llm.base import BaseLLM
from smartbutler.capabilities.llm.types import (
    FinishReason,
    LLMResponse,
    Message,
    StreamChunk,
    ToolCall,
    Usage,
)


class FakeLLM(BaseLLM):
    """测试用 LLM stub：返回预置 response 列表，按调用顺序消费。"""

    def __init__(self, responses: list[LLMResponse] | None = None) -> None:
        self._responses: list[LLMResponse] = list(responses or [])
        self._call_count = 0
        self.last_messages: list[Message] | None = None
        self.last_tools: list[Any] | None = None

    def queue(self, *responses: LLMResponse) -> None:
        """追加预置响应。"""
        self._responses.extend(responses)

    def call_count(self) -> int:
        return self._call_count

    async def chat(
        self,
        messages: list[Message],
        *,
        tools: list[Any] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        extra_body: dict[str, Any] | None = None,
    ) -> LLMResponse:
        self.last_messages = list(messages)
        self.last_tools = list(tools) if tools else None
        if not self._responses:
            msg = "FakeLLM: 预置响应已用完"
            raise AssertionError(msg)
        self._call_count += 1
        return self._responses.pop(0)

    async def chat_stream(
        self,
        messages: list[Message],
        *,
        tools: list[Any] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        extra_body: dict[str, Any] | None = None,
    ) -> AsyncIterator[StreamChunk]:
        if False:  # 让 mypy 高兴
            yield
        msg = "FakeLLM 不支持流式"
        raise NotImplementedError(msg)

    async def aclose(self) -> None:
        return None


def make_tool_call(call_id: str, name: str, arguments: str) -> ToolCall:
    """构造 ToolCall 的便捷函数（避免每次写一堆字段）。"""
    from smartbutler.capabilities.llm.types import FunctionCall

    return ToolCall(
        id=call_id,
        type="function",
        function=FunctionCall(name=name, arguments=arguments),
    )


def make_response(
    content: str = "",
    tool_calls: list[ToolCall] | None = None,
    finish: FinishReason | None = None,
) -> LLMResponse:
    """构造 LLMResponse 的便捷函数。"""
    if finish is None:
        finish = FinishReason.TOOL_CALLS if tool_calls else FinishReason.STOP
    return LLMResponse(
        content=content,
        tool_calls=tool_calls,
        finish_reason=finish,
        usage=Usage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        model="fake-llm",
    )


@pytest.fixture
def fresh_manager() -> Any:
    """每个测试拿全新的 AgentManager，不污染默认单例。"""
    from smartbutler.agents import AgentManager

    AgentManager.reset_default()
    yield AgentManager()
    AgentManager.reset_default()


@pytest.fixture
def fake_llm() -> FakeLLM:
    return FakeLLM()
