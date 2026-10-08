"""Tool 能力层（capabilities/tools/）。

对业务层暴露:
- BaseTool / FunctionTool: 抽象基类
- ToolRegistry: 注册表
- register_tool: 装饰器
- ToolScope / Permission / ToolContext / ToolResult: 数据类型
- ToolError 及子异常: 异常体系
- to_langchain_tool: LangChain 适配（用于 Phase 4 接入 LangGraph）

业务层用法:
    from smartbutler.capabilities.tools import (
        BaseTool, ToolRegistry, register_tool, ToolScope,
        ToolContext, Permission,
    )

    @register_tool()
    def my_tool() -> str:
        return "ok"

LangChain 适配（Phase 4 才用）:
    from smartbutler.capabilities.tools import to_langchain_tool
    lc_tools = [to_langchain_tool(t) for t in registry.list_all()]
"""

from smartbutler.capabilities.tools.base import BaseTool, FunctionTool
from smartbutler.capabilities.tools.decorator import register_tool
from smartbutler.capabilities.tools.langchain_adapter import (
    collect_langchain_tools,
    to_langchain_tool,
)
from smartbutler.capabilities.tools.registry import ToolRegistry
from smartbutler.capabilities.tools.types import (
    Permission,
    ToolAlreadyRegisteredError,
    ToolContext,
    ToolError,
    ToolNotFoundError,
    ToolPermissionDeniedError,
    ToolResult,
    ToolScope,
    ToolTimeoutError,
)

__all__ = [
    # 抽象与实现
    "BaseTool",
    "FunctionTool",
    # 注册
    "ToolRegistry",
    "register_tool",
    # 数据类型
    "ToolScope",
    "Permission",
    "ToolContext",
    "ToolResult",
    # 异常
    "ToolError",
    "ToolNotFoundError",
    "ToolAlreadyRegisteredError",
    "ToolPermissionDeniedError",
    "ToolTimeoutError",
    # LangChain 适配
    "to_langchain_tool",
    "collect_langchain_tools",
]
