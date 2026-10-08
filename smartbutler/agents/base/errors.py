"""Sub-Agent 层 — 异常体系。

与 capabilities.tools.ToolError / LLMError 平级，构成 Sub-Agent 的异常族。
Sub-Agent 失败信息应尽量**回流**到 AgentOutput（success=False + error 字段），
让管家 LLM 自主决策而非抛异常。
"""
from __future__ import annotations


class AgentError(Exception):
    """Sub-Agent 相关错误的基类。"""


class AgentNotFoundError(AgentError):
    """Agent name 未在 AgentManager 中找到。"""


class AgentAlreadyRegisteredError(AgentError):
    """重复注册同名 Agent。"""


class AgentTimeoutError(AgentError):
    """Agent.handle() 执行超过 timeout_seconds。"""


class AgentInvalidInputError(AgentError):
    """AgentInput 校验失败或语义非法（如 raw 为空）。"""


__all__ = [
    "AgentError",
    "AgentNotFoundError",
    "AgentAlreadyRegisteredError",
    "AgentTimeoutError",
    "AgentInvalidInputError",
]
