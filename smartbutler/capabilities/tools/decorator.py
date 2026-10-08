"""Tool 能力层 — @register_tool 装饰器。

设计原则：
1. **一行注册**：业务方只写函数 + 装饰器，零样板。
2. **自动入默认 registry**：@register_tool() 立即生效，无需显式 register。
3. **透传原函数**：装饰后原函数仍可正常调用（便于测试）。
4. **延迟注册**：可配合 module import 时统一注册，避免循环依赖。

示例:
    @register_tool(scope=ToolScope.GLOBAL, readonly=True)
    def get_current_time(timezone: str = "UTC") -> str:
        '''返回当前时间'''
        return datetime.now(tz=ZoneInfo(timezone)).isoformat()
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from smartbutler.capabilities.tools.base import FunctionTool
from smartbutler.capabilities.tools.registry import ToolRegistry
from smartbutler.capabilities.tools.types import Permission, ToolScope


def register_tool(
    *,
    name: str | None = None,
    description: str | None = None,
    scope: ToolScope = ToolScope.GLOBAL,
    owner: str | None = None,
    required_permissions: list[Permission] | None = None,
    timeout_seconds: float | None = None,
    readonly: bool = False,
    idempotent: bool = False,
    registry: ToolRegistry | None = None,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """把函数包成 FunctionTool 并写入 registry。

    Args:
        name: 工具名（默认用函数名）。
        description: 工具描述（默认用 docstring 首行）。
        scope: 可见性范围，默认 GLOBAL。
        owner: 当 scope=AGENT 时是 agent name；当 scope=SKILL 时是 skill name。
        required_permissions: 调用方需持有的权限列表。
        timeout_seconds: 单次执行超时（秒），None 不限。
        readonly: 是否只读。
        idempotent: 是否幂等。
        registry: 自定义 registry（默认用 ToolRegistry.get_default()）。

    Returns:
        装饰器。装饰后的函数既可正常调用（透传），也已被注册。
    """
    # 提前校验 owner 与 scope 的一致性，避免运行时报错
    if scope == ToolScope.AGENT and not owner:
        msg = "@register_tool(scope=AGENT) 必须指定 owner=<agent_name>"
        raise ValueError(msg)
    if scope == ToolScope.SKILL and not owner:
        msg = "@register_tool(scope=SKILL) 必须指定 owner=<skill_name>"
        raise ValueError(msg)

    target_registry = registry if registry is not None else ToolRegistry.get_default()

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        owner_agent = owner if scope == ToolScope.AGENT else None
        owner_skill = owner if scope == ToolScope.SKILL else None

        tool = FunctionTool(
            func=func,
            name=name,
            description=description,
            scope=scope,
            owner_agent=owner_agent,
            owner_skill=owner_skill,
            required_permissions=frozenset(required_permissions or []),
            timeout_seconds=timeout_seconds,
            readonly=readonly,
            idempotent=idempotent,
        )
        target_registry.register(tool)

        # 透传原函数（保留调用能力，便于测试 / 调试）
        func.tool = tool  # type: ignore[attr-defined]
        return func

    return decorator
