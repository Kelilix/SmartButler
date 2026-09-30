"""LLM 能力层 — LangChain 适配器。

把 ``langchain_openai.ChatOpenAI`` 包装成符合 ``BaseLLM`` 抽象的实现。
覆盖 DeepSeek / Qwen / OpenRouter / Azure 兼容模式等所有 OpenAI Chat Completion
协议的厂商(通过 ``base_url`` 切换)。

设计要点:
1. **顶层契约对齐**:chat / chat_stream / aclose 三个方法的签名与返回值类型
   与 ``OpenAICompatibleLLM`` 完全一致,业务层无差别。
2. **LangChain 在底层**:消息类型转换、工具绑定、流式 chunk 处理全部由
   LangChain 完成,我们的代码量大幅减少,且能免费获得 LangChain 内置能力:
   - 流式 tool_calls 累积
   - reasoning_content 自动透传(DeepSeek 推理模型)
   - structured output(暂未启用,留作后续扩展点)
3. **异常映射统一**:LangChain / openai SDK 的异常按状态码与关键字映射到
   ``BaseLLM`` 的异常体系,与 HttpLLM 行为一致。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import structlog
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_openai import ChatOpenAI

from smartbutler.capabilities.llm.base import (
    BaseLLM,
    LLMAuthError,
    LLMContextLengthError,
    LLMContentFilterError,
    LLMError,
    LLMRateLimitError,
    LLMTimeoutError,
    LLMUpstreamError,
)
from smartbutler.capabilities.llm.types import (
    FinishReason,
    FunctionCall,
    LLMResponse,
    Message,
    Role,
    StreamChunk,
    ToolCall,
    ToolSpec,
    Usage,
)
from smartbutler.config.llm import LLMSettings


# ---------------------------------------------------------------------------
# 类型转换:内部 Message / ToolSpec <-> LangChain 类型
# ---------------------------------------------------------------------------


def _message_to_lc(msg: Message) -> BaseMessage:
    """把内部 ``Message`` 序列化为 LangChain ``BaseMessage``。"""
    if msg.role is Role.SYSTEM:
        return SystemMessage(content=msg.content or "")
    if msg.role is Role.USER:
        return HumanMessage(content=msg.content or "")
    if msg.role is Role.ASSISTANT:
        if msg.tool_calls:
            # LangChain 的 tool_call 是 dict(id/name/args/type=tool_call),其中
            # args 是 dict 而不是 JSON 字符串,与 OpenAI 协议相反。
            lc_tool_calls = [
                {
                    "id": tc.id,
                    "name": tc.function.name,
                    "args": json.loads(tc.function.arguments)
                    if tc.function.arguments
                    else {},
                    "type": "tool_call",
                }
                for tc in msg.tool_calls
            ]
            return AIMessage(content=msg.content or "", tool_calls=lc_tool_calls)
        return AIMessage(content=msg.content or "")
    if msg.role is Role.TOOL:
        if not msg.tool_call_id:
            msg_ = "tool 消息必须带 tool_call_id"
            raise ValueError(msg_)
        return ToolMessage(content=msg.content or "", tool_call_id=msg.tool_call_id)
    msg_ = f"未知的 Role: {msg.role}"
    raise ValueError(msg_)


def _messages_to_lc(messages: list[Message]) -> list[BaseMessage]:
    return [_message_to_lc(m) for m in messages]


def _tools_to_lc_format(tools: list[ToolSpec]) -> list[dict[str, Any]]:
    """把内部 ``ToolSpec`` 列表序列化为 LangChain 可接受的 OpenAI tools dict 列表。

    LangChain 的 ``convert_to_openai_tool`` 直接接受这种 dict 格式,所以无需
    在这里再生成 Pydantic 模型。
    """
    return [
        {
            "type": t.type,
            "function": {
                "name": t.function.name,
                "description": t.function.description,
                "parameters": t.function.parameters,
            },
        }
        for t in tools
    ]


# ---------------------------------------------------------------------------
# 适配器实现
# ---------------------------------------------------------------------------


class LangChainLLMAdapter(BaseLLM):
    """LangChain-based LLM 适配器(包装 ``ChatOpenAI``)。

    覆盖 DeepSeek / Qwen / OpenRouter / Azure 兼容模式等所有
    OpenAI Chat Completion 协议的厂商(通过 ``base_url`` 切换)。

    与 ``OpenAICompatibleLLM`` 的对外接口完全一致,业务层无须感知实现差异。
    """

    def __init__(
        self,
        settings: LLMSettings,
        *,
        api_key: str,
        base_url: str,
        timeout: float | None = None,
    ) -> None:
        if not api_key:
            msg = "LangChainLLMAdapter 需要非空 api_key"
            raise ValueError(msg)
        if not base_url:
            msg = "LangChainLLMAdapter 需要非空 base_url"
            raise ValueError(msg)

        self._settings = settings
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout if timeout is not None else settings.timeout

        # ChatOpenAI 接受 OpenAI 兼容协议的 base_url(DeepSeek / Qwen / OpenRouter)。
        self._chat = ChatOpenAI(
            model=settings.model,
            temperature=settings.temperature,
            max_tokens=settings.max_tokens,
            openai_api_key=api_key,
            base_url=self._base_url,
            timeout=self._timeout,
            max_retries=settings.max_retries,
        )

        structlog.get_logger(__name__).info(
            "llm.adapter.initialized",
            impl="LangChainLLMAdapter",
            model=settings.model,
            base_url=self._base_url,
            timeout=self._timeout,
            max_retries=settings.max_retries,
        )

    async def aclose(self) -> None:
        # ChatOpenAI 内部的 httpx.AsyncClient 由 LangChain 管理,无需显式关闭。
        # 若未来引入其它需要关闭的资源,在此 override。
        return None

    async def __aenter__(self) -> LangChainLLMAdapter:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # ------------------------------------------------------------------
    # 内部:按调用参数构造 bound chat model(支持 tools / extra_body)
    # ------------------------------------------------------------------

    def _bound_chat(self, tools: list[ToolSpec] | None) -> Any:
        """根据 tools 列表返回一个绑定了工具的 chat model。

        ``extra_body`` 不在此层处理,而是作为 ``ainvoke`` / ``astream`` 的 kwargs 直接
        传入 —— openai SDK 原生支持 ``extra_body``,会被注入到请求体顶层,
        用于透传 DeepSeek thinking 等非 OpenAI 标准字段。
        """
        if tools:
            return self._chat.bind_tools(_tools_to_lc_format(tools))
        return self._chat

    @staticmethod
    def _per_call_kwargs(
        temperature: float | None,
        max_tokens: int | None,
    ) -> dict[str, Any]:
        """构造单次调用级别的 kwargs(temperature / max_tokens 覆盖)。"""
        out: dict[str, Any] = {}
        if temperature is not None:
            out["temperature"] = temperature
        if max_tokens is not None:
            out["max_tokens"] = max_tokens
        return out

    # ------------------------------------------------------------------
    # chat()
    # ------------------------------------------------------------------

    async def chat(
        self,
        messages: list[Message],
        *,
        tools: list[ToolSpec] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        extra_body: dict[str, Any] | None = None,
    ) -> LLMResponse:
        lc_messages = _messages_to_lc(messages)
        chat = self._bound_chat(tools)
        invoke_kwargs = self._per_call_kwargs(temperature, max_tokens)
        if extra_body:
            invoke_kwargs["extra_body"] = extra_body

        try:
            response = await chat.ainvoke(lc_messages, **invoke_kwargs)
        except Exception as exc:  # noqa: BLE001 - 统一映射
            raise self._map_exception(exc) from exc

        return self._build_response(response)

    # ------------------------------------------------------------------
    # chat_stream()
    # ------------------------------------------------------------------

    async def chat_stream(
        self,
        messages: list[Message],
        *,
        tools: list[ToolSpec] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        extra_body: dict[str, Any] | None = None,
    ) -> AsyncIterator[StreamChunk]:
        lc_messages = _messages_to_lc(messages)
        chat = self._bound_chat(tools)
        invoke_kwargs = self._per_call_kwargs(temperature, max_tokens)
        if extra_body:
            invoke_kwargs["extra_body"] = extra_body

        try:
            async for chunk in chat.astream(lc_messages, **invoke_kwargs):
                sc = self._build_stream_chunk(chunk)
                if sc is not None:
                    yield sc
        except Exception as exc:  # noqa: BLE001 - 统一映射
            raise self._map_exception(exc) from exc

    # ------------------------------------------------------------------
    # 响应解析
    # ------------------------------------------------------------------

    @staticmethod
    def _build_response(msg: AIMessage) -> LLMResponse:
        """把 LangChain AIMessage 转回内部 LLMResponse。"""
        content = msg.content if isinstance(msg.content, str) else ""

        # tool_calls:LangChain 是 dict(name/args 已 parsed),我们转回 JSON string
        # 以满足 FunctionCall.arguments 的协议。
        tool_calls: list[ToolCall] | None = None
        if msg.tool_calls:
            tool_calls = [
                ToolCall(
                    id=tc["id"],
                    type="function",
                    function=FunctionCall(
                        name=tc["name"],
                        arguments=json.dumps(
                            tc.get("args") or {}, ensure_ascii=False
                        ),
                    ),
                )
                for tc in msg.tool_calls
            ]
            finish_reason = FinishReason.TOOL_CALLS
        else:
            raw = (msg.response_metadata or {}).get("finish_reason") or "stop"
            try:
                finish_reason = FinishReason(raw)
            except ValueError:
                finish_reason = FinishReason.STOP

        # token usage:LangChain 0.3 用 UsageMetadata(input/output/total_tokens)
        usage_raw = msg.usage_metadata or {}
        usage = Usage(
            prompt_tokens=usage_raw.get("input_tokens", 0),
            completion_tokens=usage_raw.get("output_tokens", 0),
            total_tokens=usage_raw.get("total_tokens", 0),
        )

        model_name = (msg.response_metadata or {}).get("model_name") or ""

        return LLMResponse(
            content=content,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            usage=usage,
            model=model_name,
        )

    @staticmethod
    def _build_stream_chunk(chunk: AIMessageChunk) -> StreamChunk | None:
        """把 LangChain AIMessageChunk 转回内部 StreamChunk。

        跳过全空 chunk(content / reasoning / finish 都为空),避免上层拿到噪声。
        """
        content = chunk.content if isinstance(chunk.content, str) else ""

        # reasoning_content:DeepSeek / 部分推理模型在 additional_kwargs 中携带。
        # 不同 langchain-openai 版本对 reasoning 字段的处理位置可能略有差异,
        # 这里优先读 additional_kwargs['reasoning_content']。
        reasoning = None
        if chunk.additional_kwargs:
            reasoning = chunk.additional_kwargs.get("reasoning_content")

        # finish_reason 通常只在最后一个 chunk 出现。
        finish: FinishReason | None = None
        if chunk.response_metadata:
            raw = chunk.response_metadata.get("finish_reason")
            if raw:
                try:
                    finish = FinishReason(raw)
                except ValueError:
                    finish = FinishReason.STOP

        if not content and not reasoning and finish is None:
            return None

        return StreamChunk(
            content_delta=content,
            reasoning_content_delta=reasoning,
            finish_reason=finish,
        )

    # ------------------------------------------------------------------
    # 异常映射
    # ------------------------------------------------------------------

    @staticmethod
    def _map_exception(exc: Exception) -> LLMError:
        """把 LangChain / openai SDK 的异常映射到 BaseLLM 异常体系。

        实现策略:按异常类名 + 消息关键字粗粒度匹配。对 HttpLLM 同样的状态码
        错误产生同样的异常类型,保证业务层无需区分实现。
        """
        cls_name = type(exc).__name__
        msg = str(exc) or cls_name

        # 401/403 鉴权
        if cls_name in ("AuthenticationError",) or "401" in msg or "403" in msg:
            return LLMAuthError(f"鉴权失败: {msg}")
        # 429 限流
        if cls_name in ("RateLimitError",) or "429" in msg:
            return LLMRateLimitError(f"限流: {msg}")
        # 上下文超长
        if (
            cls_name in ("ContextLengthError",)
            or "context_length_exceeded" in msg
            or "context length" in msg.lower()
        ):
            return LLMContextLengthError(f"上下文超长: {msg}")
        # 内容拦截
        if (
            "content_filter" in msg
            or "policy_violation" in msg
            or cls_name in ("ContentFilterError",)
        ):
            return LLMContentFilterError(f"内容被拦截: {msg}")
        # 超时
        if cls_name in ("TimeoutError", "APITimeoutError") or "timeout" in msg.lower():
            return LLMTimeoutError(f"超时: {msg}")
        # 其它
        return LLMUpstreamError(f"上游错误({cls_name}): {msg}")