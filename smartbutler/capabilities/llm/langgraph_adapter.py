"""BaseLLM → LangChain BaseChatModel 适配器。

目的:
让 Phase 4 的 LangGraph 循环复用 Phase 1 写好的 ``BaseLLM`` 抽象,
而不是直接用 ``langchain_openai.ChatOpenAI``(会绕开我们的双后端架构)。

为什么不直接复用 ``LangChainLLMAdapter._chat``:
- ``LangChainLLMAdapter`` 是 ``BaseLLM`` 子类,只为 ``BaseLLM.chat()`` 服务。
- Phase 4 需要一个 ``BaseChatModel`` 实例来调 ``.ainvoke()`` / ``.bind_tools()``。
- 强行在 ``LangChainLLMAdapter`` 上加 LangChain 导出属性会污染 ``BaseLLM`` 抽象。

折中:
本适配器接受一个 ``BaseLLM`` 实例 + 同等 settings,内部用 LangChain 客户端
直接构造一个等价的 ``BaseChatModel``(不是复用 ``BaseLLM`` 的方法)。
这样:
- 不破坏 ``BaseLLM`` 抽象。
- 双后端(OpenAI 协议 vs LangChain 协议)在 Phase 4 都能用 —— 只要
  ``BaseLLM`` 的 ``settings.api_key`` + ``settings.base_url`` 一致。
- 异常处理仍走 ``LangChainLLMAdapter._map_exception``,行为一致。

限制(Phase 4):
- 只支持 OpenAI 兼容协议厂商(DeepSeek / Qwen / OpenRouter / Azure)。
- 不暴露 ``BaseLLM.chat_stream``,因为 Phase 4 GraphBuilder 只需要 ``ainvoke``。
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.tools import BaseTool
from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from smartbutler.capabilities.llm.base import BaseLLM
from smartbutler.capabilities.llm.langchain_adapter import LangChainLLMAdapter
from smartbutler.capabilities.llm.types import ToolSpec


def _tool_to_lc_format(t: Any) -> dict[str, Any]:  # noqa: ANN401
    """把任意 tool 序列化 OpenAI protocol dict。

    支持输入:
    - ``dict``: 已是 OpenAI 格式,直接返回。
    - ``ToolSpec`` (内部): 取 ``type/function.{name,description,parameters}``。
    - ``langchain_core.tools.BaseTool`` (含 StructuredTool / FunctionTool):
      用 LangChain 提供的 ``convert_to_openai_tool`` 序列化(统一入口)。
    """
    if isinstance(t, dict):
        return t
    if isinstance(t, BaseTool):
        from langchain_core.utils.function_calling import convert_to_openai_tool
        return convert_to_openai_tool(t)
    if isinstance(t, ToolSpec):
        return {
            "type": t.type,
            "function": {
                "name": t.function.name,
                "description": t.function.description,
                "parameters": t.function.parameters,
            },
        }
    msg = f"无法序列化的 tool 类型: {type(t).__name__}"
    raise TypeError(msg)


def _tools_to_lc_format(tools: list[Any]) -> list[dict[str, Any]]:  # noqa: ANN401
    """批量把 ``ToolSpec`` / ``BaseTool`` / ``dict`` 列表转 OpenAI dict 列表。"""
    if not tools:
        return []
    return [_tool_to_lc_format(t) for t in tools]


def _lc_messages_from_internal(messages: list[BaseMessage] | list[Any]) -> list[BaseMessage]:
    """Phase 4 decide_node 喂给 LLM 的是 LangChain ``BaseMessage`` 列表,
    本函数只做类型断言,避免污染。
    """
    if not messages:
        return []
    if not all(isinstance(m, BaseMessage) for m in messages):
        msg = f"expecting list[BaseMessage], got {[type(m).__name__ for m in messages]}"
        raise TypeError(msg)
    return list(messages)


class ButlerChatModelAdapter:
    """``BaseLLM`` 视角的统一入口,内部包一层 LangChain ``BaseChatModel``。

    为什么不直接继承 ``BaseChatModel``:
    - LangChain 的 ``BaseChatModel`` 抽象很大(几十个抽象方法),为 Phase 4
      单独重写一个子类工作量大且没必要。
    - 我们的需求只有 ``ainvoke(messages)`` + ``bind_tools(tools)``,
      这两个就是 ``ChatOpenAI`` 的现成方法。
    - 所以:**组合**优于**继承**。
    """

    def __init__(
        self,
        *,
        base_llm: BaseLLM,
        api_key: str,
        base_url: str,
        model: str,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        timeout: float = 60.0,
        max_retries: int = 2,
    ) -> None:
        self._base_llm = base_llm
        self._chat = ChatOpenAI(
            model=model,
            temperature=temperature,
            max_completion_tokens=max_tokens,
            api_key=SecretStr(api_key),
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            max_retries=max_retries,
        )

    @property
    def base_llm(self) -> BaseLLM:
        """暴露底层 ``BaseLLM``(供 Phase 4 orchestrator 调 ``aclose()`` 等)。"""
        return self._base_llm

    def bind_tools(self, tools: list[Any]) -> Any:
        """返回绑定工具的 LangChain chat model。

        接受 list[ToolSpec] 或 list[BaseTool] 或 list[dict],统一转为 OpenAI 协议。
        返回类型故意是 Any —— 实际是 ``Runnable[LanguageModelInput, BaseMessage]``。
        """
        return self._chat.bind_tools(_tools_to_lc_format(tools))

    async def ainvoke(
        self,
        messages: list[BaseMessage],
        **kwargs: Any,
    ) -> BaseMessage:
        """直接调 LangChain ``ainvoke``。异常按 ``LangChainLLMAdapter._map_exception`` 映射。

        返回类型故意放宽到 ``BaseMessage`` —— 调用方一般用 ``isinstance(msg, AIMessage)`` 区分。
        """
        lc_messages = _lc_messages_from_internal(messages)
        try:
            return await self._chat.ainvoke(lc_messages, **kwargs)
        except Exception as exc:  # noqa: BLE001
            raise LangChainLLMAdapter._map_exception(exc) from exc

    async def astream(
        self,
        messages: list[BaseMessage],
        **kwargs: Any,
    ) -> AsyncIterator[BaseMessage]:
        """流式版本。返回类型放宽到 ``BaseMessage``。"""
        lc_messages = _lc_messages_from_internal(messages)
        try:
            async for chunk in self._chat.astream(lc_messages, **kwargs):
                yield chunk
        except Exception as exc:  # noqa: BLE001
            raise LangChainLLMAdapter._map_exception(exc) from exc


def messages_from_internal(messages: list[Any]) -> list[BaseMessage]:
    """把 Phase 1 的 ``Message`` 列表转成 LangChain ``BaseMessage`` 列表。

    Phase 4 orchestrator 接 ``ButlerState.messages``(LangChain ``BaseMessage`` 列表),
    但如果调用方用 Phase 1 的 ``Message`` 喂入,需先过这个转换。
    """
    from smartbutler.capabilities.llm.types import Message, Role

    out: list[BaseMessage] = []
    for m in messages:
        if isinstance(m, BaseMessage):
            out.append(m)
            continue
        if not isinstance(m, Message):
            msg = f"unknown message type: {type(m).__name__}"
            raise TypeError(msg)
        if m.role is Role.SYSTEM:
            out.append(SystemMessage(content=m.content or ""))
        elif m.role is Role.USER:
            out.append(HumanMessage(content=m.content or ""))
        elif m.role is Role.ASSISTANT:
            if m.tool_calls:
                lc_tool_calls = [
                    {
                        "id": tc.id,
                        "name": tc.function.name,
                        "args": json.loads(tc.function.arguments) if tc.function.arguments else {},
                        "type": "tool_call",
                    }
                    for tc in m.tool_calls
                ]
                out.append(AIMessage(content=m.content or "", tool_calls=lc_tool_calls))
            else:
                out.append(AIMessage(content=m.content or ""))
        elif m.role is Role.TOOL:
            if not m.tool_call_id:
                raise ValueError("tool 消息必须带 tool_call_id")
            out.append(ToolMessage(content=m.content or "", tool_call_id=m.tool_call_id))
        else:
            raise ValueError(f"未知的 Role: {m.role}")
    return out


__all__ = [
    "ButlerChatModelAdapter",
    "messages_from_internal",
]
