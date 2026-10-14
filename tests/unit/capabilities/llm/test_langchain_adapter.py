"""tests/unit/capabilities/llm/test_langchain_adapter.py — LangChainLLMAdapter 单测。

为什么不用 respx
----------------
LangChain 1.6 内部对 ``httpx.AsyncClient`` 使用 ``@lru_cache`` 缓存,而
openai 3.26 在 Python 3.14 上走的是 ``httpx2``(与 ``httpx`` 是两个不同
的包)。respx 0.23 只 patch 旧 ``httpx`` 的 transport,完全不识别 ``httpx2``,
导致 ``@respx.mock`` 装饰器下请求仍然走到真实 deepseek.com,unit 测试失败。

替代方案
--------
直接替换 ``LangChainLLMAdapter._chat`` 为一个 ``MagicMock``,让它:
- ``ainvoke(...)`` 返回我们构造的 ``AIMessage``
- ``astream(...)`` 返回我们构造的 ``AsyncIterator[AIMessageChunk]``
- ``bind_tools(...)`` 返回另一个 mock(bind_tools 走的是 chat 自己的方法)

为什么是替换整个 _chat 而不是 patch 单个方法
-------------------------------------------
``ChatOpenAI`` 是 pydantic frozen model, ``patch.object(chat, 'astream', ...)``
或 ``create=True`` 都会在 pydantic 验证阶段抛 ``ValueError: object has no
field 'astream'``。所以只能替换整个实例为 MagicMock。

MagicMock 替换的副作用
---------------------
``self._chat.bind_tools(tools)`` 必须返回另一个有 ``ainvoke`` / ``astream``
的对象 — 否则 adapter 内部 ``_bound_chat(tools)`` 返回 mock,而
``chat.ainvoke(...)`` 也能调(MagicMock 自动生成子 mock)。所以 MagicMock
方案天然兼容。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk

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
    FunctionCall,
    FunctionSpec,
    Message,
    Role,
    ToolCall,
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


# ---------------------------------------------------------------------------
# Mock 辅助:构造 LangChain AIMessage / AIMessageChunk 来喂给 adapter 的解析逻辑
# ---------------------------------------------------------------------------


def _make_ai_message(
    content: str = "你好",
    *,
    tool_calls: list[dict[str, Any]] | None = None,
    finish_reason: str = "stop",
    model: str = "deepseek-flash",
    prompt_tokens: int = 5,
    completion_tokens: int = 3,
    total_tokens: int = 8,
) -> AIMessage:
    """构造一个 LangChain AIMessage,模拟 OpenAI chat.completions 响应。"""
    return AIMessage(
        content=content,
        tool_calls=tool_calls or [],
        response_metadata={"finish_reason": finish_reason, "model_name": model},
        usage_metadata={
            "input_tokens": prompt_tokens,
            "output_tokens": completion_tokens,
            "total_tokens": total_tokens,
        },
    )


def _make_chunk(
    content: str = "",
    *,
    reasoning_content: str | None = None,
    finish_reason: str | None = None,
) -> AIMessageChunk:
    """构造一个 LangChain AIMessageChunk,模拟流式 delta。"""
    additional: dict[str, Any] = {}
    if reasoning_content is not None:
        additional["reasoning_content"] = reasoning_content
    response_metadata: dict[str, Any] = {}
    if finish_reason is not None:
        response_metadata["finish_reason"] = finish_reason
    return AIMessageChunk(
        content=content,
        additional_kwargs=additional,
        response_metadata=response_metadata,
    )


async def _aiter_chunks(chunks: list[AIMessageChunk]) -> AsyncIterator[AIMessageChunk]:
    """把 list 转成 async iterator,模拟 chat.astream。"""
    for c in chunks:
        yield c


class _FakeChat:
    """替身 ``ChatOpenAI``,暴露 ``ainvoke`` / ``astream`` / ``bind_tools``。

    与 MagicMock 不同:这个替身显式声明接口,测试失败时错误更易读,
    且不会因为 MagicMock 自动创建子 mock 而引入"虚假调用"。

    ``bind_tools`` 默认返回 self(测试里通常不关心 bound 之后是否还是同一个对象,
    只要它有 ainvoke/astream 就行 — 这样 adapter 内的
    ``chat = self._bound_chat(tools)`` 拿到的就是它自己)。
    """

    def __init__(self) -> None:
        self.ainvoke_call_count = 0
        self.ainvoke_call_args: tuple[Any, dict[str, Any]] | None = None
        self.astream_call_count = 0
        self.bound_tools: list[Any] | None = None

    async def ainvoke(
        self, *args: Any, **kwargs: Any
    ) -> AIMessage:  # pragma: no cover - 由测试侧配置 side_effect
        self.ainvoke_call_count += 1
        self.ainvoke_call_args = (args, kwargs)
        raise NotImplementedError(
            "FakeChat.ainvoke 未配置返回值或 side_effect,检查测试 setup"
        )

    async def astream(  # pragma: no cover - 由测试侧配置
        self, *args: Any, **kwargs: Any
    ) -> AsyncIterator[AIMessageChunk]:
        self.astream_call_count += 1
        raise NotImplementedError(
            "FakeChat.astream 未配置返回值,检查测试 setup"
        )
        # async generator 必须 yield 一次才能被识别为 generator,这里用 unreachable
        # 让 Python 知道类型;实际测试里会被替换为 MagicMock(spec=AsyncMock)
        yield  # type: ignore[unreachable]

    def bind_tools(self, tools: list[Any]) -> _FakeChat:
        self.bound_tools = list(tools)
        return self


def _install_fake_chat(
    llm: LangChainLLMAdapter,
    *,
    ainvoke_return: AIMessage | None = None,
    ainvoke_side_effect: BaseException | None = None,
    astream_return: AsyncIterator[AIMessageChunk] | None = None,
    astream_side_effect: BaseException | None = None,
) -> _FakeChat:
    """把 ``llm._chat`` 替换为一个可配置的 ``_FakeChat`` 实例。

    返回 fake 实例,测试可通过它读 call_count / call_args。

    ``ainvoke_side_effect`` / ``astream_side_effect`` 是异常实例
    (不是 callable) — ainvoke 每次被调时直接 raise 它。
    """
    fake = _FakeChat()
    if ainvoke_return is not None or ainvoke_side_effect is not None:

        async def _ainvoke(*args: Any, **kwargs: Any) -> AIMessage:
            fake.ainvoke_call_count += 1
            fake.ainvoke_call_args = (args, kwargs)
            if ainvoke_side_effect is not None:
                raise ainvoke_side_effect
            assert ainvoke_return is not None  # for type check
            return ainvoke_return

        fake.ainvoke = _ainvoke  # type: ignore[method-assign]
    if astream_return is not None or astream_side_effect is not None:

        async def _astream(
            *args: Any, **kwargs: Any
        ) -> AsyncIterator[AIMessageChunk]:
            fake.astream_call_count += 1
            if astream_side_effect is not None:
                raise astream_side_effect
            assert astream_return is not None  # for type check
            async for chunk in astream_return:
                yield chunk

        fake.astream = _astream  # type: ignore[method-assign]
    llm._chat = fake  # type: ignore[assignment]
    return fake


# ---------------------------------------------------------------------------
# 内部类型转换单测(纯函数,无 HTTP / 无 mock)
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
# 协议转换单测(用 _FakeChat 替 respx)
# ---------------------------------------------------------------------------


class TestChatNonStream:
    @pytest.mark.asyncio
    async def test_simple_reply(self) -> None:
        """chat() 收到普通文本响应,正确解析 content/finish/usage。"""
        llm = _make_llm()
        try:
            fake = _install_fake_chat(llm, ainvoke_return=_make_ai_message("你好"))
            resp = await llm.chat([Message.user("hi")])
            assert fake.ainvoke_call_count == 1
            assert resp.content == "你好"
            assert resp.finish_reason is FinishReason.STOP
            assert resp.usage.total_tokens == 8
        finally:
            await llm.aclose()

    @pytest.mark.asyncio
    async def test_tool_calls_parsed(self) -> None:
        """chat() 收到 tool_calls,正确解析为内部 ToolCall 协议(JSON 字符串参数)。"""
        lc_tool_calls = [
            {
                "id": "tc_lc_1",
                "name": "get_weather",
                # 注意:LangChain 1.x 的 tool_call.args 已经是 dict,不是 JSON 字符串
                "args": {"city": "BJ"},
            }
        ]
        llm = _make_llm()
        try:
            _install_fake_chat(
                llm,
                ainvoke_return=_make_ai_message(
                    "",
                    tool_calls=lc_tool_calls,
                    finish_reason="tool_calls",
                ),
            )
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
    async def test_request_includes_tools(self) -> None:
        """chat() 调用 ainvoke 时,会 bind tools + 透传 temperature/max_tokens。

        验证三件事:
        1. self._chat.bind_tools 被调用,参数是 OpenAI 协议格式
        2. ainvoke 被调用,temperature / max_tokens 走 kwargs 透传
        3. max_tokens 是 OpenAI 协议的 max_tokens(不是 max_completion_tokens)
        """
        llm = _make_llm()
        try:
            fake = _install_fake_chat(llm, ainvoke_return=_make_ai_message("hi"))
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
            assert fake.ainvoke_call_count == 1
            # bind_tools 收到的 tools 应该是 OpenAI 协议格式(由 _tools_to_lc_format 转换)
            assert fake.bound_tools is not None
            assert len(fake.bound_tools) == 1
            assert fake.bound_tools[0]["type"] == "function"
            assert fake.bound_tools[0]["function"]["name"] == "f"
            # temperature / max_tokens 走 invoke_kwargs → 进 ainvoke 的 kwargs
            assert fake.ainvoke_call_args is not None
            _, call_kwargs = fake.ainvoke_call_args
            assert call_kwargs.get("temperature") == 0.9
            assert call_kwargs.get("max_tokens") == 256
        finally:
            await llm.aclose()

    @pytest.mark.asyncio
    async def test_extra_body_passed_as_model_kwargs(self) -> None:
        """chat() 把 extra_body 透传为 ainvoke 的 kwargs。"""
        llm = _make_llm()
        try:
            fake = _install_fake_chat(llm, ainvoke_return=_make_ai_message("ok"))
            await llm.chat(
                [Message.user("u")],
                extra_body={"thinking": {"type": "disabled"}},
            )
            assert fake.ainvoke_call_count == 1
            assert fake.ainvoke_call_args is not None
            _, call_kwargs = fake.ainvoke_call_args
            assert call_kwargs.get("extra_body") == {"thinking": {"type": "disabled"}}
        finally:
            await llm.aclose()


class TestErrorMapping:
    @pytest.mark.asyncio
    async def test_401_raises_auth(self) -> None:
        """ainvoke 抛 OpenAIAuthenticationError → LLMAuthError。"""
        from langchain_openai.chat_models.base import OpenAIAuthenticationError

        llm = _make_llm()
        try:
            _install_fake_chat(
                llm,
                ainvoke_side_effect=OpenAIAuthenticationError(
                    "bad key", response=MagicMock(), body={}
                ),
            )
            with pytest.raises(LLMAuthError, match="鉴权失败"):
                await llm.chat([Message.user("x")])
        finally:
            await llm.aclose()

    @pytest.mark.asyncio
    async def test_429_raises_ratelimit(self) -> None:
        """ainvoke 抛 openai.RateLimitError → LLMRateLimitError。"""
        from openai import RateLimitError

        llm = _make_llm()
        try:
            _install_fake_chat(
                llm,
                ainvoke_side_effect=RateLimitError(
                    "slow down", response=MagicMock(), body={}
                ),
            )
            with pytest.raises(LLMRateLimitError, match="限流"):
                await llm.chat([Message.user("x")])
        finally:
            await llm.aclose()

    @pytest.mark.asyncio
    async def test_context_length_raises_context_error(self) -> None:
        """ainvoke 抛 BadRequestError 含 context_length_exceeded → LLMContextLengthError。"""
        from openai import BadRequestError

        llm = _make_llm()
        try:
            _install_fake_chat(
                llm,
                ainvoke_side_effect=BadRequestError(
                    "context length exceeded",
                    response=MagicMock(),
                    body={"error": {"code": "context_length_exceeded"}},
                ),
            )
            with pytest.raises(LLMContextLengthError, match="上下文超长"):
                await llm.chat([Message.user("x")])
        finally:
            await llm.aclose()

    @pytest.mark.asyncio
    async def test_500_raises_upstream(self) -> None:
        """ainvoke 抛 InternalServerError → LLMUpstreamError。"""
        from openai import InternalServerError

        llm = _make_llm()
        try:
            _install_fake_chat(
                llm,
                ainvoke_side_effect=InternalServerError(
                    "boom", response=MagicMock(), body={}
                ),
            )
            with pytest.raises(LLMUpstreamError):
                await llm.chat([Message.user("x")])
        finally:
            await llm.aclose()

    @pytest.mark.asyncio
    async def test_timeout_raises_timeout(self) -> None:
        """ainvoke 抛 APITimeoutError → LLMTimeoutError。"""
        from openai import APITimeoutError

        llm = _make_llm()
        try:
            _install_fake_chat(
                llm,
                ainvoke_side_effect=APITimeoutError(request=MagicMock()),
            )
            with pytest.raises(LLMTimeoutError):
                await llm.chat([Message.user("x")])
        finally:
            await llm.aclose()


class TestChatStream:
    @pytest.mark.asyncio
    async def test_stream_basic(self) -> None:
        """chat_stream() 拼接所有 content chunk,最后一个 chunk 给 finish_reason。"""
        llm = _make_llm()
        try:
            chunks = [
                _make_chunk(content="你"),
                _make_chunk(content="好"),
                _make_chunk(finish_reason="stop"),
            ]
            fake = _install_fake_chat(llm, astream_return=_aiter_chunks(chunks))
            seen: list[str] = []
            final_finish: FinishReason | None = None
            async for sc in llm.chat_stream([Message.user("hi")]):
                if sc.content_delta:
                    seen.append(sc.content_delta)
                if sc.finish_reason is not None:
                    final_finish = sc.finish_reason
            assert fake.astream_call_count == 1
            assert "".join(seen) == "你好"
            assert final_finish is FinishReason.STOP
        finally:
            await llm.aclose()

    @pytest.mark.asyncio
    async def test_stream_reasoning_content(self) -> None:
        """chat_stream() 把 chunk.additional_kwargs['reasoning_content'] 透传为 reasoning_content_delta。"""
        llm = _make_llm()
        try:
            chunks = [
                _make_chunk(reasoning_content="思考中"),
                _make_chunk(content="答"),
                _make_chunk(finish_reason="stop"),
            ]
            _install_fake_chat(llm, astream_return=_aiter_chunks(chunks))
            text_chunks: list[str] = []
            reasoning_chunks: list[str] = []
            async for sc in llm.chat_stream([Message.user("hi")]):
                if sc.content_delta:
                    text_chunks.append(sc.content_delta)
                if sc.reasoning_content_delta:
                    reasoning_chunks.append(sc.reasoning_content_delta)
            assert "".join(text_chunks) == "答"
            assert "".join(reasoning_chunks) == "思考中"
        finally:
            await llm.aclose()

