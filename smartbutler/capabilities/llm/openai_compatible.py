"""LLM 能力层 — OpenAI 兼容协议的 LLM 实现。

适配范围：DeepSeek、Qwen（DashScope）、OpenRouter、Azure OpenAI（兼容模式）等所有
实现 OpenAI Chat Completion API 的厂商。

实现要点：
1. 用 httpx.AsyncClient 直连 OpenAI 协议端点，不引入 OpenAI SDK（依赖更轻、可控性更强）。
2. chat() 走非流式；chat_stream() 解析 SSE 流。
3. 异常按状态码精细映射到 BaseLLM 的异常体系。
4. 支持 tool calling（OpenAI tools 协议，DeepSeek / Qwen 均已兼容）。
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from typing import Any

import httpx
import structlog

from smartbutler.capabilities.llm.base import (
    BaseLLM,
    LLMAuthError,
    LLMContentFilterError,
    LLMContextLengthError,
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


def _normalize_base_url(base_url: str) -> str:
    """归一化 base_url,去掉尾部斜杠以方便拼接 /v1/chat/completions。"""
    return base_url.rstrip("/")


class OpenAICompatibleLLM(BaseLLM):
    """OpenAI Chat Completion API 兼容实现。

    覆盖 DeepSeek / Qwen / OpenRouter / Azure(兼容模式) 等所有
    严格遵守 OpenAI Chat Completion 协议的厂商。
    """

    def __init__(
        self,
        settings: LLMSettings,
        *,
        api_key: str,
        base_url: str,
        timeout: float = 60.0,
    ) -> None:
        if not api_key:
            msg = "OpenAICompatibleLLM 需要非空 api_key"
            raise ValueError(msg)
        if not base_url:
            msg = "OpenAICompatibleLLM 需要非空 base_url"
            raise ValueError(msg)

        self._settings = settings
        self._api_key = api_key
        self._base_url = _normalize_base_url(base_url)
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout),
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
        )

        structlog.get_logger(__name__).info(
            "llm.adapter.initialized",
            impl="OpenAICompatibleLLM",
            model=settings.model,
            base_url=self._base_url,
            timeout=timeout,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> OpenAICompatibleLLM:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # ------------------------------------------------------------------
    # 协议转换：内部 Message/ToolSpec -> OpenAI JSON
    # ------------------------------------------------------------------

    @staticmethod
    def _message_to_openai(msg: Message) -> dict[str, Any]:
        """把内部 Message 序列化为 OpenAI 协议字段。"""
        if msg.role is Role.TOOL:
            if not msg.tool_call_id:
                msg_ = "tool 消息必须带 tool_call_id"
                raise ValueError(msg_)
            payload: dict[str, Any] = {
                "role": "tool",
                "tool_call_id": msg.tool_call_id,
                "content": msg.content or "",
            }
        elif msg.role is Role.ASSISTANT:
            payload = {"role": "assistant", "content": msg.content or ""}
            if msg.tool_calls:
                payload["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": tc.type,
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in msg.tool_calls
                ]
        else:
            payload = {"role": msg.role.value, "content": msg.content or ""}
        if msg.name:
            payload["name"] = msg.name
        return payload

    @staticmethod
    def _tools_to_openai(tools: list[ToolSpec]) -> list[dict[str, Any]]:
        """把内部 ToolSpec 列表序列化为 OpenAI tools 字段。"""
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

    @staticmethod
    def _build_request_body(
        settings: LLMSettings,
        messages: list[Message],
        tools: list[ToolSpec] | None,
        temperature: float | None,
        max_tokens: int | None,
        stream: bool,
        extra_body: dict[str, Any] | None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": settings.model,
            "messages": [OpenAICompatibleLLM._message_to_openai(m) for m in messages],
        }
        body["temperature"] = temperature if temperature is not None else settings.temperature
        body["max_tokens"] = max_tokens if max_tokens is not None else settings.max_tokens
        if tools:
            body["tools"] = OpenAICompatibleLLM._tools_to_openai(tools)
            body["tool_choice"] = "auto"
        if stream:
            body["stream"] = True
        if extra_body:
            # OpenAI SDK 的 ``extra_body`` 是一个**合并机制**而非协议字段：
            # SDK 会把 extra_body 里的键值平铺到请求体顶层一起序列化。
            # 因此这里必须 ``body.update``，而不是 ``body["extra_body"] = ...``。
            body.update(extra_body)
        return body

    # ------------------------------------------------------------------
    # 错误映射
    # ------------------------------------------------------------------

    @staticmethod
    def _map_error(status: int, payload: dict[str, Any] | str) -> Exception:
        """把 HTTP 错误映射到具体异常子类。"""
        body = payload if isinstance(payload, dict) else {}
        err = body.get("error") if isinstance(body, dict) else None
        msg = err.get("message") if isinstance(err, dict) else str(payload)
        code = err.get("code") if isinstance(err, dict) else None

        if status in (401, 403):
            return LLMAuthError(f"鉴权失败({status}): {msg}")
        if status == 429:
            return LLMRateLimitError(f"限流(429): {msg}")
        if status == 400 and code in {"context_length_exceeded", "context_overflow"}:
            return LLMContextLengthError(f"上下文超长: {msg}")
        if status == 400 and code in {"content_filter", "policy_violation"}:
            return LLMContentFilterError(f"内容被拦截: {msg}")
        if status in (408, 504):
            return LLMTimeoutError(f"上游超时({status}): {msg}")
        if status >= 500:
            return LLMUpstreamError(f"上游服务错误({status}): {msg}")
        return LLMUpstreamError(f"未预期错误({status}): {msg}")

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
        body = self._build_request_body(
            self._settings, messages, tools, temperature, max_tokens, stream=False, extra_body=extra_body
        )
        url = f"{self._base_url}/chat/completions"
        try:
            resp = await self._client.post(url, json=body)
        except httpx.TimeoutException as exc:
            raise LLMTimeoutError(str(exc)) from exc
        except httpx.HTTPError as exc:
            raise LLMUpstreamError(f"网络错误: {exc}") from exc

        if resp.status_code >= 400:
            try:
                payload: Any = resp.json()
            except json.JSONDecodeError:
                payload = resp.text
            raise self._map_error(resp.status_code, payload)

        data = resp.json()
        return self._parse_response(data)

    @staticmethod
    def _parse_response(data: dict[str, Any]) -> LLMResponse:
        """解析 OpenAI Chat Completion 响应为 LLMResponse。"""
        choices = data.get("choices") or []
        if not choices:
            msg = f"OpenAI 响应缺少 choices: {data!r}"
            raise LLMUpstreamError(msg)
        first = choices[0]
        message = first.get("message") or {}
        finish = first.get("finish_reason") or "stop"

        tool_calls: list[ToolCall] | None = None
        raw_tcs = message.get("tool_calls")
        if raw_tcs:
            tool_calls = [
                ToolCall(
                    id=tc["id"],
                    type=tc.get("type", "function"),
                    function=FunctionCall(
                        name=tc["function"]["name"],
                        arguments=tc["function"]["arguments"],
                    ),
                )
                for tc in raw_tcs
            ]
            finish_reason = FinishReason.TOOL_CALLS
        else:
            try:
                finish_reason = FinishReason(finish)
            except ValueError:
                finish_reason = FinishReason.STOP

        usage_raw = data.get("usage") or {}
        usage = Usage(
            prompt_tokens=usage_raw.get("prompt_tokens", 0),
            completion_tokens=usage_raw.get("completion_tokens", 0),
            total_tokens=usage_raw.get("total_tokens", 0),
        )

        return LLMResponse(
            content=message.get("content") or "",
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            usage=usage,
            model=data.get("model", ""),
        )

    @staticmethod
    def _parse_stream_chunk(data: dict[str, Any]) -> StreamChunk | None:
        """解析单个 SSE chunk 为 StreamChunk。

        兼容 DeepSeek 推理模型：
        - ``content`` 字段为最终回复增量。
        - ``reasoning_content`` 字段携带思考过程增量（DeepSeek-Flash 特有）。
        """
        choices = data.get("choices") or []
        if not choices:
            return None
        first = choices[0]
        delta = first.get("delta") or {}
        content = delta.get("content") or ""
        reasoning_content = delta.get("reasoning_content") or None
        finish_raw = first.get("finish_reason")
        finish: FinishReason | None = None
        if finish_raw:
            try:
                finish = FinishReason(finish_raw)
            except ValueError:
                finish = FinishReason.STOP
        return StreamChunk(
            content_delta=content,
            reasoning_content_delta=reasoning_content,
            finish_reason=finish,
        )

    async def chat_stream(
        self,
        messages: list[Message],
        *,
        tools: list[ToolSpec] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        extra_body: dict[str, Any] | None = None,
    ) -> AsyncGenerator[StreamChunk, None]:
        """流式调用,逐 chunk 产出 StreamChunk。"""
        body = self._build_request_body(
            self._settings, messages, tools, temperature, max_tokens, stream=True, extra_body=extra_body
        )
        url = f"{self._base_url}/chat/completions"
        try:
            resp = await self._client.post(url, json=body)
        except httpx.TimeoutException as exc:
            raise LLMTimeoutError(str(exc)) from exc
        except httpx.HTTPError as exc:
            raise LLMUpstreamError(f"网络错误: {exc}") from exc

        if resp.status_code >= 400:
            try:
                payload: Any = resp.json()
            except json.JSONDecodeError:
                payload = resp.text
            raise self._map_error(resp.status_code, payload)

        # SSE 流解析
        try:
            async for raw_line in resp.aiter_lines():
                line = raw_line.strip()
                if not line or line.startswith(":"):
                    continue
                if not line.startswith("data:"):
                    continue
                data_str = line[5:].strip()
                if data_str == "[DONE]":
                    break
                try:
                    chunk = json.loads(data_str)
                except json.JSONDecodeError:
                    continue
                sc = self._parse_stream_chunk(chunk)
                if sc is not None:
                    yield sc
        finally:
            await resp.aclose()
