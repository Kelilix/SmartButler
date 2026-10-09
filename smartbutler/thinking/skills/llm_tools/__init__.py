"""thinking/skills/llm_tools/ — SmartButlerFilesystemBackend 的 LLM 适配层。

为什么住在 thinking/skills/ 而非 capabilities/tools/:
- 这 7 个 tool 强依赖 SmartButlerFilesystemBackend(thinking 业务实体)
- 它们的"工具性"是 LLM 适配层,核心是 backend.xxx() 业务方法调用
- capabilities/tools/ 应该是"通用 LLM 工具能力"(无业务域),不是业务后端包装
- 放 thinking 让依赖单向:thinking → capabilities(LLM 接口契约),无回指

导出:
- 7 个 tool 类(给需要单独引的场景)
- build_default_skill_tools(backend) → list[BaseTool]:给 ToolRegistry.register_many 用
"""
from __future__ import annotations

from smartbutler.capabilities.tools.base import BaseTool
from smartbutler.thinking.skills.filesystem_backend import SmartButlerFilesystemBackend
from smartbutler.thinking.skills.llm_tools.fs_tools import (
    DeleteFileTool,
    EditFileTool,
    GlobTool,
    GrepTool,
    LsTool,
    build_default_skill_tools,
)
from smartbutler.thinking.skills.llm_tools.read_file import ReadFileTool
from smartbutler.thinking.skills.llm_tools.write_file import WriteFileTool

__all__ = [
    "ReadFileTool",
    "WriteFileTool",
    "EditFileTool",
    "DeleteFileTool",
    "LsTool",
    "GrepTool",
    "GlobTool",
    "build_default_skill_tools",
]


def register_default_skill_tools(
    backend: SmartButlerFilesystemBackend,
    registry=None,
) -> list[BaseTool]:
    """把 7 个默认 skill tool 注册到 registry(默认 = ToolRegistry.get_default())。

    Returns:
        已注册的 tool 列表。

    Raises:
        ToolAlreadyRegisteredError: 同名 tool 已存在(同实例幂等,异实例报错)。
    """
    # ToolRegistry 在 capabilities 层(单向依赖:thinking → capabilities)，
    # 函数内 import 是为了不污染模块加载顺序,不是治循环。
    from smartbutler.capabilities.tools.registry import ToolRegistry

    target = registry if registry is not None else ToolRegistry.get_default()
    tools = build_default_skill_tools(backend)
    for t in tools:
        target.register(t)
    return tools
