"""tests/unit/capabilities/llm/test_base.py — BaseLLM 抽象契约测试。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from smartbutler.capabilities.llm.base import (
    BaseLLM,
    LLMAuthError,
    LLMContentFilterError,
    LLMContextLengthError,
    LLMError,
    LLMRateLimitError,
    LLMTimeoutError,
    LLMUpstreamError,
)
from smartbutler.capabilities.llm.types import LLMResponse, Message, StreamChunk


class _ConcreteLLM(BaseLLM):
    """最小可工作实现,用于验证抽象方法签名。"""

    def __init__(self) -> None:
        self.closed = False

    async def chat(
        self,
        messages: list[Message],
        *,
        tools: Any = None,
        temperature: Any = None,
        max_tokens: Any = None,
    ) -> LLMResponse:
        return LLMResponse(content="ok")

    def chat_stream(
        self,
        messages: list[Message],
        *,
        tools: Any = None,
        temperature: Any = None,
        max_tokens: Any = None,
    ) -> AsyncIterator[StreamChunk]:
        async def _gen() -> AsyncIterator[StreamChunk]:
            yield StreamChunk(content_delta="ok")

        return _gen()

    async def aclose(self) -> None:
        self.closed = True


class TestBaseLLM:
    def test_cannot_instantiate_directly(self) -> None:
        with pytest.raises(TypeError):
            BaseLLM()  # type: ignore[abstract]

    @pytest.mark.asyncio
    async def test_concrete_can_chat(self) -> None:
        llm = _ConcreteLLM()
        resp = await llm.chat([Message.user("hi")])
        assert resp.content == "ok"

    @pytest.mark.asyncio
    async def test_concrete_can_stream(self) -> None:
        llm = _ConcreteLLM()
        out: list[str] = []
        async for chunk in llm.chat_stream([Message.user("hi")]):
            out.append(chunk.content_delta)
        assert "".join(out) == "ok"

    @pytest.mark.asyncio
    async def test_default_aclose_is_noop(self) -> None:
        llm = _ConcreteLLM()
        await llm.aclose()
        assert llm.closed is True

    def test_partial_impl_cannot_instantiate(self) -> None:
        class _Partial(BaseLLM):
            async def chat(self, messages: Any, **kw: Any) -> LLMResponse:  # type: ignore[override]
                return LLMResponse(content="x")

        with pytest.raises(TypeError):
            _Partial()  # type: ignore[abstract]


class TestExceptionHierarchy:
    def test_all_inherit_from_llmerror(self) -> None:
        for cls in (
            LLMAuthError,
            LLMRateLimitError,
            LLMContextLengthError,
            LLMTimeoutError,
            LLMUpstreamError,
            LLMContentFilterError,
        ):
            err = cls("boom")
            assert isinstance(err, LLMError)
            assert str(err) == "boom"
