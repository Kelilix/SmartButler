"""Tool 能力层 — LangChain StructuredTool 适配。

设计原则（参考 TECHNICAL_DESIGN.md ADR-002）：
1. **adapter 只在本文件出现**：业务层 / ToolRegistry 都不依赖 LangChain。
2. **FunctionTool 优先用 args_schema**：JSON Schema 由 Pydantic 自动推导，LLM 拿到一致描述。
3. **ToolError → ToolMessage(error)**：异常不冒泡，由 LLM 看到错误消息后自主决策。
   这是 LangGraph ToolNode 的标准错误处理模式（参考 langgraph.prebuilt.ToolNode）。
4. **不持久化** StructuredTool：每次 to_langchain_tool() 现场构造，
   保证 picked up 最新的 tool 元数据（hot reload / 测试 mock 都安全）。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from langchain_core.tools import StructuredTool

from smartbutler.capabilities.tools.base import BaseTool, FunctionTool
from smartbutler.capabilities.tools.types import (
    ToolContext,
    ToolError,
    ToolResult,
)


def to_langchain_tool(
    tool: BaseTool,
    *,
    context_factory: Callable[[], ToolContext] | None = None,
) -> StructuredTool:
    """把 BaseTool 包装成 LangChain StructuredTool。

    Args:
        tool: 任意 BaseTool 实例（FunctionTool / 内置工具 / Skill 工具都支持）。
        context_factory: 构造 ToolContext 的工厂；默认用最小权限空 context。
            真实运行时由 LangGraph 节点注入（Phase 4 接入）。

    Returns:
        StructuredTool：可绑定到 llm.bind_tools([...]) 的 LangChain 工具对象。
    """
    # 函数式工具走 args_schema 路径（JSON Schema 完整）
    if isinstance(tool, FunctionTool):
        return _build_from_function_tool(tool, context_factory)

    # 自定义 BaseTool 子类：仅暴露空 schema（子类若需参数应继承 FunctionTool）
    return _build_from_base_tool(tool, context_factory)


def _default_context_factory() -> ToolContext:
    """默认 ToolContext 工厂：空权限，无 parent。

    Phase 4 接入 LangGraph 时会被替换为 graph.runnable config 派生。
    """
    return ToolContext(user_id="system", session_id="system")


def _build_from_function_tool(
    tool: FunctionTool,
    context_factory: Callable[[], ToolContext] | None,
) -> StructuredTool:
    """FunctionTool 走 StructuredTool.from_function（自动用 args_schema）。"""
    factory = context_factory or _default_context_factory

    async def _coroutine(**kwargs: Any) -> str:
        """真正的 LangChain tool 调用入口。

        LangChain 协议：返回 str 或 ToolMessage。
        我们统一返回 str（ToolResult.content / error），简化 LLM 端处理。
        """
        ctx = factory()
        try:
            result = await tool.ainvoke(ctx, **kwargs)
        except ToolError as exc:
            # ToolError 包成字符串错误，LLM 自主决策（重试 / 换工具 / 道歉）
            return f"[ToolError] {type(exc).__name__}: {exc}"
        if not result.success:
            return f"[ToolError] {result.error or 'unknown error'}"
        return result.content

    return StructuredTool.from_function(
        coroutine=_coroutine,
        name=tool.name,
        description=tool.description,
        args_schema=tool.args_schema,
    )


def _build_from_base_tool(
    tool: BaseTool,
    context_factory: Callable[[], ToolContext] | None,
) -> StructuredTool:
    """非 FunctionTool 的 BaseTool 子类走 args_schema=None（空 schema）。

    大多数场景应继承 FunctionTool（带函数签名自动推导 schema）；
    这里是兜底路径，比如未来可能出现的纯配置式 tool。
    """
    factory = context_factory or _default_context_factory

    async def _coroutine(**kwargs: Any) -> str:
        ctx = factory()
        try:
            result = await tool.ainvoke(ctx, **kwargs)
        except ToolError as exc:
            return f"[ToolError] {type(exc).__name__}: {exc}"
        if not result.success:
            return f"[ToolError] {result.error or 'unknown error'}"
        return result.content

    return StructuredTool.from_function(
        coroutine=_coroutine,
        name=tool.name,
        description=tool.description,
        # 不传 args_schema → LangChain 走空 schema（要求 LLM 不传任何参数）
    )


def collect_langchain_tools(
    tools: list[BaseTool],
    *,
    context_factory: Callable[[], ToolContext] | None = None,
) -> list[StructuredTool]:
    """批量转换 BaseTool 列表为 LangChain StructuredTool 列表。

    用于 LangGraph 节点: llm.bind_tools(collect_langchain_tools(registry.get_butler()))
    """
    return [to_langchain_tool(t, context_factory=context_factory) for t in tools]


__all__ = [
    "to_langchain_tool",
    "collect_langchain_tools",
]


# 导入兜底：ToolResult 用作类型注解
_ = ToolResult
