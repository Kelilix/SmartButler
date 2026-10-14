"""tests/unit/capabilities/llm/test_openai_compatible.py — 用 respx mock httpx。"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from smartbutler.capabilities.llm.base import (
    LLMAuthError,
    LLMContentFilterError,
    LLMContextLengthError,
    LLMRateLimitError,
    LLMTimeoutError,
    LLMUpstreamError,
)
from smartbutler.capabilities.llm.openai_compatible import OpenAICompatibleLLM
from smartbutler.capabilities.llm.types import (
    FinishReason,
    FunctionSpec,
    Message,
    ToolSpec,
)
from smartbutler.config.llm import LLMSettings


def _settings() -> LLMSettings:
    return LLMSettings(
        provider="openai",
        model="deepseek-flash",
        temperature=0.3,
        max_tokens=512,
        openai_api_key="sk-test",
        openai_base_url="https://api.deepseek.com",
    )


def _make_llm() -> OpenAICompatibleLLM:
    return OpenAICompatibleLLM(
        settings=_settings(),
        api_key="sk-test",
        base_url="https://api.deepseek.com",
    )


SIMPLE_RESPONSE: dict = {
    "id": "chatcmpl-1",
    "object": "chat.completion",
    "created": 1,
    "model": "deepseek-flash",
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "你好"},
            "finish_reason": "stop",
        }
    ],
    "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
}


TOOL_RESPONSE: dict = {
    "id": "chatcmpl-2",
    "object": "chat.completion",
    "created": 1,
    "model": "deepseek-flash",
    "choices": [
        {
            "index": 0,
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "tc_1",
                        "type": "function",
                        "function": {
                            "name": "get_weather",
                            "arguments": '{"city":"BJ"}',
                        },
                    }
                ],
            },
            "finish_reason": "tool_calls",
        }
    ],
    "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
}


class TestChatNonStream:
    @pytest.mark.asyncio
    @respx.mock
    async def test_simple_reply(self) -> None:
        route = respx.post("https://api.deepseek.com/chat/completions").mock(
            return_value=httpx.Response(200, json=SIMPLE_RESPONSE)
        )
        llm = _make_llm()
        try:
            resp = await llm.chat([Message.user("hi")])
            assert resp.content == "你好"
            assert resp.finish_reason is FinishReason.STOP
            assert resp.usage.total_tokens == 8
            assert resp.model == "deepseek-flash"
            assert route.called
        finally:
            await llm.aclose()

    @pytest.mark.asyncio
    @respx.mock
    async def test_tool_calls_parsed(self) -> None:
        respx.post("https://api.deepseek.com/chat/completions").mock(
            return_value=httpx.Response(200, json=TOOL_RESPONSE)
        )
        llm = _make_llm()
        try:
            resp = await llm.chat([Message.user("BJ 天气")])
            assert resp.finish_reason is FinishReason.TOOL_CALLS
            assert resp.tool_calls is not None
            assert len(resp.tool_calls) == 1
            tc = resp.tool_calls[0]
            assert tc.id == "tc_1"
            assert tc.function.name == "get_weather"
            assert tc.function.parsed_arguments() == {"city": "BJ"}
        finally:
            await llm.aclose()

    @pytest.mark.asyncio
    @respx.mock
    async def test_request_body(self) -> None:
        route = respx.post("https://api.deepseek.com/chat/completions").mock(
            return_value=httpx.Response(200, json=SIMPLE_RESPONSE)
        )
        llm = _make_llm()
        try:
            await llm.chat(
                [Message.system("sys"), Message.user("u")],
                tools=[
                    ToolSpec(
                        function=FunctionSpec(
                            name="f",
                            description="d",
                            parameters={"type": "object", "properties": {"x": {"type": "string"}}},
                        )
                    )
                ],
                temperature=0.9,
                max_tokens=256,
            )
            assert route.called
            request = route.calls.last.request
            body = json.loads(request.content)
            assert body["model"] == "deepseek-flash"
            assert body["temperature"] == 0.9
            assert body["max_tokens"] == 256
            assert body["messages"] == [
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "u"},
            ]
            assert body["tools"][0]["function"]["name"] == "f"
            assert body["tool_choice"] == "auto"
            assert request.headers["Authorization"] == "Bearer sk-test"
        finally:
            await llm.aclose()


class TestErrorMapping:
    @pytest.mark.asyncio
    @respx.mock
    async def test_401_raises_auth(self) -> None:
        respx.post("https://api.deepseek.com/chat/completions").mock(
            return_value=httpx.Response(401, json={"error": {"message": "bad key", "code": "auth"}})
        )
        llm = _make_llm()
        try:
            with pytest.raises(LLMAuthError, match="鉴权失败"):
                await llm.chat([Message.user("x")])
        finally:
            await llm.aclose()

    @pytest.mark.asyncio
    @respx.mock
    async def test_429_raises_ratelimit(self) -> None:
        respx.post("https://api.deepseek.com/chat/completions").mock(
            return_value=httpx.Response(
                429, json={"error": {"message": "slow down", "code": "rate"}}
            )
        )
        llm = _make_llm()
        try:
            with pytest.raises(LLMRateLimitError, match="限流"):
                await llm.chat([Message.user("x")])
        finally:
            await llm.aclose()

    @pytest.mark.asyncio
    @respx.mock
    async def test_context_length(self) -> None:
        respx.post("https://api.deepseek.com/chat/completions").mock(
            return_value=httpx.Response(
                400,
                json={
                    "error": {
                        "message": "too long",
                        "code": "context_length_exceeded",
                    }
                },
            )
        )
        llm = _make_llm()
        try:
            with pytest.raises(LLMContextLengthError, match="上下文超长"):
                await llm.chat([Message.user("x")])
        finally:
            await llm.aclose()

    @pytest.mark.asyncio
    @respx.mock
    async def test_content_filter(self) -> None:
        respx.post("https://api.deepseek.com/chat/completions").mock(
            return_value=httpx.Response(
                400,
                json={
                    "error": {
                        "message": "filtered",
                        "code": "content_filter",
                    }
                },
            )
        )
        llm = _make_llm()
        try:
            with pytest.raises(LLMContentFilterError, match="内容被拦截"):
                await llm.chat([Message.user("x")])
        finally:
            await llm.aclose()

    @pytest.mark.asyncio
    @respx.mock
    async def test_500_upstream(self) -> None:
        respx.post("https://api.deepseek.com/chat/completions").mock(
            return_value=httpx.Response(500, text="boom")
        )
        llm = _make_llm()
        try:
            with pytest.raises(LLMUpstreamError, match="上游服务错误"):
                await llm.chat([Message.user("x")])
        finally:
            await llm.aclose()

    @pytest.mark.asyncio
    @respx.mock
    async def test_timeout(self) -> None:
        respx.post("https://api.deepseek.com/chat/completions").mock(
            side_effect=httpx.TimeoutException("slow")
        )
        llm = _make_llm()
        try:
            with pytest.raises(LLMTimeoutError):
                await llm.chat([Message.user("x")])
        finally:
            await llm.aclose()


class TestChatStream:
    def test_parser_only(self) -> None:
        sc = OpenAICompatibleLLM._parse_stream_chunk(
            {
                "choices": [
                    {
                        "delta": {"content": "你"},
                        "finish_reason": None,
                    }
                ]
            }
        )
        assert sc is not None
        assert sc.content_delta == "你"
        assert sc.finish_reason is None

    @pytest.mark.asyncio
    @respx.mock
    async def test_stream_full(self) -> None:
        # x1
        s1 = "data: " + json.dumps(
            {"choices": [{"delta": {"content": "你"}, "finish_reason": None}]}
        )
        s2 = "data: " + json.dumps(
            {"choices": [{"delta": {"content": "好"}, "finish_reason": None}]}
        )
        s3 = "data: " + json.dumps({"choices": [{"delta": {}, "finish_reason": "stop"}]})
        s4 = "data: [DONE]"
        sse = "\n".join([s1, s2, s3, s4]) + "\n"

        respx.post("https://api.deepseek.com/chat/completions").mock(
            return_value=httpx.Response(
                200,
                content=sse.encode("utf-8"),
                headers={"content-type": "text/event-stream"},
            )
        )
        llm = _make_llm()
        try:
            chunks: list[str] = []
            async for sc in llm.chat_stream([Message.user("hi")]):
                chunks.append(sc.content_delta)
            assert "".join(chunks) == "你好"
        finally:
            await llm.aclose()
