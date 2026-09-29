"""LLM 能力层 — 数据类型定义（按文档 §5.2）。

设计原则：
1. **与具体厂商解耦**：Message / ToolSpec / LLMResponse 等不引用 OpenAI、Anthropic 的 SDK 类型，
   任何厂商适配器都需要先转换为本模块定义的 Pydantic 模型。
2. **Pydantic v2 BaseModel**：保证序列化、校验、可观测字段一致。
3. **字段命名贴近 OpenAI 协议**：方便 OpenAI 兼容适配器（DeepSeek / Qwen / OpenRouter / Azure）零成本转换。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class Role(StrEnum):
    """消息角色。

    对应 OpenAI Chat Completion 的 role 字段；tool 角色用于把工具调用结果回传给模型。
    """

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class FunctionCall(BaseModel):
    """assistant 消息中的单个函数调用。

    arguments 字段在 OpenAI 协议中是 JSON 字符串（不是 dict），保持原样以便协议级联调。
    业务层可通过 .parsed_arguments() 解析为 dict。
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    arguments: str  # JSON string, 协议级原始数据

    def parsed_arguments(self) -> dict[str, Any]:
        """解析 arguments JSON 字符串为 dict。"""
        import json

        try:
            result = json.loads(self.arguments)
        except json.JSONDecodeError as exc:
            msg = f"FunctionCall.arguments 不是合法 JSON: {self.arguments!r}"
            raise ValueError(msg) from exc
        if not isinstance(result, dict):
            msg = f"FunctionCall.arguments 必须是 JSON object,实际为 {type(result).__name__}"
            raise ValueError(msg)
        return result


class ToolCall(BaseModel):
    """assistant 消息中的一次工具调用（OpenAI Tool Calls 协议）。

    一个 assistant 消息可以包含多个 ToolCall（并行调用）。
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(description="tool call id,用于回传 tool 结果")
    type: str = Field(default="function", description="OpenAI 协议固定为 'function'")
    function: FunctionCall


class Message(BaseModel):
    """对话中的一条消息。

    构造规则：
    - role=user / system：仅 content。
    - role=assistant：content 可空（纯工具调用时），可同时含 tool_calls。
    - role=tool：必须带 tool_call_id（对应上轮 assistant 的 ToolCall.id），
      content 是工具返回的字符串结果。
    """

    model_config = ConfigDict(extra="forbid")

    role: Role
    content: str | None = None
    name: str | None = Field(default=None, description="多角色场景下的名字,目前保留")
    tool_calls: list[ToolCall] | None = Field(
        default=None,
        description="assistant 消息携带的工具调用列表",
    )
    tool_call_id: str | None = Field(
        default=None,
        description="tool 消息回传时,标识对应 assistant 的 ToolCall.id",
    )

    @classmethod
    def system(cls, content: str) -> Message:
        """便捷构造 system 消息。"""
        return cls(role=Role.SYSTEM, content=content)

    @classmethod
    def user(cls, content: str) -> Message:
        """便捷构造 user 消息。"""
        return cls(role=Role.USER, content=content)

    @classmethod
    def assistant(
        cls,
        content: str | None = None,
        tool_calls: list[ToolCall] | None = None,
    ) -> Message:
        """便捷构造 assistant 消息。"""
        return cls(role=Role.ASSISTANT, content=content, tool_calls=tool_calls)

    @classmethod
    def tool_result(cls, tool_call_id: str, content: str) -> Message:
        """便捷构造 tool 回传消息。"""
        return cls(role=Role.TOOL, tool_call_id=tool_call_id, content=content)


class FunctionSpec(BaseModel):
    """工具的函数描述（OpenAI function 协议）。"""

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str = ""
    parameters: dict[str, Any] = Field(
        default_factory=lambda: {"type": "object", "properties": {}},
        description="JSON Schema 对象,描述函数入参",
    )


class ToolSpec(BaseModel):
    """工具描述（OpenAI tools 协议）。

    后续 LangGraph 节点会按此格式注入 LLM,LLM 返回的 ToolCall.function.name
    会用于路由到具体可执行工具。
    """

    model_config = ConfigDict(extra="forbid")

    type: str = Field(default="function", description="固定为 'function'")
    function: FunctionSpec


class Usage(BaseModel):
    """单次调用的 token 用量统计。"""

    model_config = ConfigDict(extra="forbid")

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class FinishReason(StrEnum):
    """完成原因。

    - STOP：模型自然结束（普通文本回复）。
    - TOOL_CALLS：模型希望调用工具（loop 节点继续推进）。
    - LENGTH：达到 max_tokens 截断。
    - CONTENT_FILTER：内容被策略拦截。
    - ERROR：上游错误。
    """

    STOP = "stop"
    TOOL_CALLS = "tool_calls"
    LENGTH = "length"
    CONTENT_FILTER = "content_filter"
    ERROR = "error"


class LLMResponse(BaseModel):
    """LLM 单次调用的完整响应。"""

    model_config = ConfigDict(extra="forbid")

    content: str | None = Field(
        default=None,
        description="assistant 文本回复;若纯工具调用则为空字符串",
    )
    tool_calls: list[ToolCall] | None = Field(
        default=None,
        description="assistant 请求的工具调用列表;无则为 None",
    )
    finish_reason: FinishReason = FinishReason.STOP
    usage: Usage = Field(default_factory=Usage)
    model: str = Field(default="", description="实际调用的模型名（可能因负载均衡与请求不同）")


class StreamChunk(BaseModel):
    """流式响应的单个 chunk。

    chat_stream() 在增量内容到达时产出;tool_calls_delta 用于累积工具调用（跨 chunk 拼接）。
    reasoning_content_delta 携带推理模型的思考过程增量（DeepSeek 等推理优化模型专用）。
    """

    model_config = ConfigDict(extra="forbid")

    content_delta: str = ""
    reasoning_content_delta: str | None = None
    finish_reason: FinishReason | None = None
    tool_calls_delta: list[ToolCall] | None = None
