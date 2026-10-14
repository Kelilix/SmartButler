"""Sub-Agent 层 — 数据类型。

设计原则（参考 TECHNICAL_DESIGN.md §3.2.3 + ADR-005）：
1. **业务层不依赖 LangChain**：AgentInput/Output/Context 全部用 Pydantic + 内置类型。
2. **与 ToolContext 解耦但字段对齐**：AgentContext 透出 user_id / session_id / permissions，
   内部 tool 调用时包装成 ToolContext（Phase 3 由 Agent 显式包装）。
3. **raw / structured 双输出**：AgentOutput.content 给 LLM 看，data 给程序消费。
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from smartbutler.capabilities.tools.types import Permission


class AgentInput(BaseModel):
    """Sub-Agent 的输入。

    管家 LLM 通过 ``delegate_to_<name>(task=...)`` 调用 Sub-Agent 时，
    args 序列化成 JSON 字符串进入 ``raw``。Sub-Agent 内部可自行 parse。
    """

    model_config = ConfigDict(extra="forbid")

    raw: str = Field(description="任务原始文本（管家 LLM 通过 tool args 传入）")
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="自由扩展字段（如 trace_id / sub_task_id）",
    )


class AgentContext(BaseModel):
    """Sub-Agent 执行的运行时上下文。

    由调用方（Phase 4 管家 Loop）构造并透传给 ainvoke()。
    与 ToolContext 字段对齐，但权限集合简化成普通 list（避免 set 序列化歧义）。
    """

    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(description="当前用户标识")
    session_id: str = Field(description="当前会话标识")
    permissions: list[Permission] = Field(
        default_factory=list,
        description="Sub-Agent 持有的权限集合；其内部 tool 调用时会转成 set 透传",
    )
    parent_agent: str | None = Field(
        default=None,
        description="发起调用的 Agent name（Phase 3 通常为 'butler'）",
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="自由扩展字段",
    )


class AgentOutput(BaseModel):
    """Sub-Agent 的输出。

    - ``content`` 是给 LLM 看的字符串（最终会通过 ToolResult.content 回灌到管家）。
    - ``data`` 是结构化数据（供后续 node / agent 程序化消费，Phase 4 才用）。
    - ``tool_calls`` 记录 Sub-Agent 内部实际调了哪些 tool（仅审计用，Phase 3 默认空）。
    """

    model_config = ConfigDict(extra="forbid")

    content: str = Field(description="面向 LLM 的字符串结果")
    data: dict[str, Any] | None = Field(
        default=None,
        description="结构化数据（供后续节点程序化消费）",
    )
    success: bool = Field(default=True, description="是否成功")
    error: str | None = Field(default=None, description="失败时的错误描述")
    tool_calls: list[str] = Field(
        default_factory=list,
        description="Sub-Agent 内部实际调用的 tool name 列表（审计用）",
    )


__all__ = ["AgentInput", "AgentContext", "AgentOutput"]
