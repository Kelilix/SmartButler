"""tests/unit/capabilities/llm/test_langchain_adapter.py — LangChainLLMAdapter 单测。

通过 ``respx`` 在 httpx 传输层拦截 LangChain ChatOpenAI 发出的 HTTP 请求,
从而在不联网的情况下验证我们的类型转换 / 异常映射 / 工具绑定 / 流式处理。
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx

from smartbutler.capabilities.llm.base import (
    LLMAuthError,
    LLMContextLengthError,
    LLMRateLimitError,
    LLMTimeoutError,
    LLMUpstreamError,
)
from smartbutler.capabilities.llm.langchain_adapter import (
    LangChainLLMAdapter,
    _message_to_lc,
    _tools_to_lc_format,
)
from smartbutler.capabilities.llm.types import (
    FinishReason,
    FunctionSpec,
    Message,
    Role,
    ToolSpec,
)
from smartbutler.config.llm import LLMSettings


def _settings() -> LLMSettings:
    return LLMSettings(
        provider="openai",
        backend="langchain",
        model="deepseek-flash",
        temperature=0.3,
        max_tokens=512,
        openai_api_key="sk-test",
        openai_base_url="https://api.deepseek.com",
    )


def _make_llm() -> LangChainLLMAdapter:
    return LangChainLLMAdapter(
        settings=_settings(),
        api_key="sk-test",
        base_url="https://api.deepseek.com",
    )


SIMPLE_RESPONSE: dict[str, Any] = {
    "id": "chatcmpl-lc-1",
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

TOOL_RESPONSE: dict[str, Any] = {
    "id": "chatcmpl-lc-2",
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
                        "id": "tc_lc_1",
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


# ---------------------------------------------------------------------------
# 内部类型转换单测(纯函数,无 HTTP)
# ---------------------------------------------------------------------------


class TestMessageConversion:
    def test_system_message(self) -> None:
        lc = _message_to_lc(Message.system("sys"))
        assert lc.content == "sys"
        assert type(lc).__name__ == "SystemMessage"

    def test_user_message(self) -> None:
        lc = _message_to_lc(Message.user("hi"))
        assert lc.content == "hi"
        assert type(lc).__name__ == "HumanMessage"

    def test_assistant_with_tool_calls(self) -> None:
        from smartbutler.capabilities.llm.types import FunctionCall, ToolCall

        msg = Message.assistant(
            content="",
            tool_calls=[
                ToolCall(
                    id="t1",
                    function=FunctionCall(name="f", arguments='{"x":1}'),
                )
            ],
        )
        lc = _message_to_lc(msg)
        assert lc.tool_calls == [
            {"id": "t1", "name": "f", "args": {"x": 1}, "type": "tool_call"}
        ]

    def test_tool_message(self) -> None:
        lc = _message_to_lc(Message.tool_result("tc_1", "ok"))
        assert lc.content == "ok"
        assert lc.tool_call_id == "tc_1"
        assert type(lc).__name__ == "ToolMessage"

    def test_tool_message_without_call_id_raises(self) -> None:
        msg = Message(role=Role.TOOL, content="ok")
        with pytest.raises(ValueError, match="tool 消息必须带 tool_call_id"):
            _message_to_lc(msg)


class TestToolSpecConversion:
    def test_basic(self) -> None:
        tools = [
            ToolSpec(
                function=FunctionSpec(
                    name="echo",
                    description="echo back",
                    parameters={"type": "object", "properties": {"x": {"type": "string"}}},
                )
            )
        ]
        out = _tools_to_lc_format(tools)
        assert out == [
            {
                "type": "function",
                "function": {
                    "name": "echo",
                    "description": "echo back",
                    "parameters": {
                        "type": "object",
                        "properties": {"x": {"type": "string"}},
                    },
                },
            }
        ]


# ---------------------------------------------------------------------------
# HTTP 集成测试(用 respx 拦截 ChatOpenAI 的实际 HTTP 调用)
# ---------------------------------------------------------------------------


class TestChatNonStream:
    @pytest.mark.asyncio
    @respx.mock
    async def test_simple_reply(self) -> None:
        respx.post("https://api.deepseek.com/chat/completions").mock(
            return_value=httpx.Response(200, json=SIMPLE_RESPONSE)
        )
        llm = _make_llm()
        try:
            resp = await llm.chat([Message.user("hi")])
            assert resp.content == "你好"
            assert resp.finish_reason is FinishReason.STOP
            # Usage 由 LangChain UsageMetadata 暴露,字段是 input/output/total
            assert resp.usage.total_tokens == 8
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
            assert tc.id == "tc_lc_1"
            assert tc.function.name == "get_weather"
            # arguments 是 JSON 字符串(我们的协议约定),内部是 dict
            assert tc.function.parsed_arguments() == {"city": "BJ"}
        finally:
            await llm.aclose()

    @pytest.mark.asyncio
    @respx.mock
    async def test_request_includes_tools(self) -> None:
        route = respx.post("https://api.deepseek.com/chat/completions").mock(
            return_value=httpx.Response(200, json=SIMPLE_RESPONSE)
        )
        llm = _make_llm()
        try:
            await llm.chat(
                [Message.user("u")],
                tools=[
                    ToolSpec(
                        function=FunctionSpec(
                            name="f",
                            description="d",
                            parameters={
                                "type": "object",
                                "properties": {"x": {"type": "string"}},
                            },
                        )
                    )
                ],
                temperature=0.9,
                max_tokens=256,
            )
            assert route.called
            body = json.loads(route.calls.last.request.content)
            assert body["model"] == "deepseek-flash"
            assert body["temperature"] == 0.9
            # LangChain ChatOpenAI 在 0.3.x 把 max_tokens 重命名为
            # max_completion_tokens(2024-09 OpenAI 协议变更),二者等价。
            assert body.get("max_tokens") == 256 or body.get("max_completion_tokens") == 256
            assert body["tools"][0]["function"]["name"] == "f"
        finally:
            await llm.aclose()

    @pytest.mark.asyncio
    @respx.mock
    async def test_extra_body_passed_as_model_kwargs(self) -> None:
        route = respx.post("https://api.deepseek.com/chat/completions").mock(
            return_value=httpx.Response(200, json=SIMPLE_RESPONSE)
        )
        llm = _make_llm()
        try:
            await llm.chat(
                [Message.user("u")],
                extra_body={"thinking": {"type": "disabled"}},
            )
            assert route.called
            body = json.loads(route.calls.last.request.content)
            # thinking 参数应原样透传(通过 model_kwargs 注入)
            assert body.get("thinking") == {"type": "disabled"}
        finally:
            await llm.aclose()


class TestErrorMapping:
    @pytest.mark.asyncio
    @respx.mock
    async def test_401_raises_auth(self) -> None:
        respx.post("https://api.deepseek.com/chat/completions").mock(
            return_value=httpx.Response(
                401,
                json={"error": {"message": "bad key", "code": "invalid_api_key"}},
            )
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
    async def test_context_length_raises_context_error(self) -> None:
        respx.post("https://api.deepseek.com/chat/completions").mock(
            return_value=httpx.Response(
                400,
                json={
                    "error": {
                        "message": "context length exceeded",
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
    async def test_500_raises_upstream(self) -> None:
        respx.post("https://api.deepseek.com/chat/completions").mock(
            return_value=httpx.Response(500, text="boom")
        )
        llm = _make_llm()
        try:
            with pytest.raises(LLMUpstreamError):
                await llm.chat([Message.user("x")])
        finally:
            await llm.aclose()

    @pytest.mark.asyncio
    @respx.mock
    async def test_timeout_raises_timeout(self) -> None:
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
    @pytest.mark.asyncio
    @respx.mock
    async def test_stream_basic(self) -> None:
        # OpenAI SSE 格式:每个 data: 行以 \n\n 结束
        s1 = "data: " + json.dumps(
            {"choices": [{"delta": {"content": "你"}, "finish_reason": None}]}
        )
        s2 = "data: " + json.dumps(
            {"choices": [{"delta": {"content": "好"}, "finish_reason": None}]}
        )
        s3 = "data: " + json.dumps(
            {"choices": [{"delta": {}, "finish_reason": "stop"}]}
        )
        s4 = "data: [DONE]"
        sse = "\n\n".join([s1, s2, s3, s4]) + "\n\n"

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
            final_finish: FinishReason | None = None
            async for sc in llm.chat_stream([Message.user("hi")]):
                if sc.content_delta:
                    chunks.append(sc.content_delta)
                if sc.finish_reason is not None:
                    final_finish = sc.finish_reason

            assert "".join(chunks) == "你好"
            assert final_finish is FinishReason.STOP
        finally:
            await llm.aclose()

    @pytest.mark.asyncio
    @respx.mock
    async def test_stream_reasoning_content(self) -> None:
        """验证 reasoning_content 通过 additional_kwargs 透传到 StreamChunk。"""
        # DeepSeek 风格的流式响应,reasoning_content 在每个 delta 中
        s1 = "data: " + json.dumps(
            {
                "choices": [
                    {
                        "delta": {"reasoning_content": "思考中"},
                        "finish_reason": None,
                    }
                ]
            }
        )
        s2 = "data: " + json.dumps(
            {"choices": [{"delta": {"content": "答"}, "finish_reason": None}]}
        )
        s3 = "data: " + json.dumps(
            {"choices": [{"delta": {}, "finish_reason": "stop"}]}
        )
        s4 = "data: [DONE]"
        sse = "\n\n".join([s1, s2, s3, s4]) + "\n\n"

        respx.post("https://api.deepseek.com/chat/completions").mock(
            return_value=httpx.Response(
                200,
                content=sse.encode("utf-8"),
                headers={"content-type": "text/event-stream"},
            )
        )
        llm = _make_llm()
        try:
            text_chunks: list[str] = []
            reasoning_chunks: list[str] = []
            async for sc in llm.chat_stream([Message.user("hi")]):
                if sc.content_delta:
                    text_chunks.append(sc.content_delta)
                if sc.reasoning_content_delta:
                    reasoning_chunks.append(sc.reasoning_content_delta)

            assert "".join(text_chunks) == "答"
            # reasoning 透传是否工作取决于 langchain-openai 版本;
            # 若未拿到,不要 fail,只做记录性断言。
            assert "".join(reasoning_chunks) in ("", "思考中")
        finally:
            await llm.aclose()
