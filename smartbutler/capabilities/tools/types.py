"""Tool 能力层 — 数据类型与异常体系。

设计原则（参考 TECHNICAL_DESIGN.md §3.2.2 tools/）：
1. **与厂商/LangChain 解耦**：ToolScope / Permission 用 StrEnum，
   业务层不引用 LangChain StructuredTool 类型。
2. **Pydantic v2**：ToolContext / ToolResult 用 BaseModel，保证序列化一致。
3. **scope + owner 二维定位**：工具既能全局用，也能归属某个 Sub-Agent 或 Skill，
   通过 ToolScope 决定可见性，通过 owner_agent / owner_skill 决定归属。
"""

from __future__ import annotations

import time
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ToolScope(StrEnum):
    """工具可见性范围。

    - GLOBAL：全局通用，任何 Sub-Agent / Skill 都能直接看到。
    - BUTLER：管家元工具（如 greet_user / daily_digest），只管家自己用。
    - AGENT：归属某个 Sub-Agent，只对该 Sub-Agent 可见（Sub-Agent 内部使用）。
    - SKILL：归属某个 Skill，由 Skill loader 注入管家 prompt 后管家直接调用。
    - COMMON：通用但默认不暴露，需显式引用。
    """

    GLOBAL = "global"
    BUTLER = "butler"
    AGENT = "agent"
    SKILL = "skill"
    COMMON = "common"


class Permission(StrEnum):
    """工具执行所需的权限。

    Phase 2 先列骨架；具体策略引擎（沙箱 / ABAC）在 Phase 6+ 落地。
    任何 tool 都可以声明自己需要的权限，
    调用方（管家 LLM / Sub-Agent）的 ToolContext 携带 permissions 集合，
    ToolRegistry 在 ainvoke 时做集合对比校验。
    """

    # 家居域
    READ_HOME_STATE = "home.read"
    WRITE_HOME_STATE = "home.write"

    # 日程域
    READ_SCHEDULE = "schedule.read"
    WRITE_SCHEDULE = "schedule.write"

    # 记忆域
    READ_MEMORY = "memory.read"
    WRITE_MEMORY = "memory.write"

    # 通用副作用
    NETWORK_CALL = "network.call"
    FILE_READ = "file.read"
    FILE_WRITE = "file.write"
    SHELL_EXEC = "shell.exec"

    # LLM 元工具
    LLM_INVOKE = "llm.invoke"


class ToolContext(BaseModel):
    """工具执行的运行时上下文。

    由调用方（LangGraph 节点 / Sub-Agent）构造并透传给 ainvoke()。
    """

    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(description="当前用户标识")
    session_id: str = Field(description="当前会话标识")
    permissions: set[Permission] = Field(
        default_factory=set,
        description="调用方持有的权限集合；ToolRegistry 会与 tool.required_permissions 做交集校验",
    )
    parent_agent: str | None = Field(
        default=None,
        description="发起调用的 Agent name（butler / home_agent / ...）",
    )
    parent_skill: str | None = Field(
        default=None,
        description="发起调用的 Skill name（如果是从 Skill 内发起）",
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="自由扩展字段（如 trace_id / request_id / 重试计数）",
    )


class ToolResult(BaseModel):
    """工具执行结果（统一外壳）。

    业务层 arun() 可返回任意类型；BaseTool.ainvoke() 统一包装成 ToolResult。
    LangChain 适配器把 ToolResult.content 透传给 LLM，
    success=False 时 LLM 看到的是 error 信息而非抛出异常。
    """

    model_config = ConfigDict(extra="forbid")

    success: bool = True
    content: str = Field(description="面向 LLM 的字符串结果（最终回灌到对话）")
    data: dict[str, Any] | None = Field(
        default=None,
        description="结构化数据（供后续 node / agent 程序化消费）",
    )
    error: str | None = Field(
        default=None,
        description="失败时的错误描述；success=False 时必有",
    )
    duration_ms: float = Field(
        default=0.0,
        description="执行耗时（含权限校验 / 超时等待 / 业务执行）",
    )


class ToolError(Exception):
    """Tool 调用的基类异常。

    所有 tool 相关错误（权限 / 超时 / 业务执行失败）都应继承本类，
    上层（LangGraph ToolNode / Sub-Agent）可统一捕获。
    """


class ToolNotFoundError(ToolError):
    """工具名未在 registry 中找到。"""


class ToolAlreadyRegisteredError(ToolError):
    """重复注册同名工具。"""


class ToolPermissionDeniedError(ToolError):
    """ToolContext.permissions 缺少 tool.required_permissions。"""


class ToolTimeoutError(ToolError):
    """arun() 执行超过 tool.timeout_seconds。"""


def now_ms() -> float:
    """返回当前时间戳（毫秒），用于记录 duration_ms。"""
    return time.perf_counter() * 1000.0
