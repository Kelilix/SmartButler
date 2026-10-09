"""Skill 工具集合 — 包装 SmartButlerFilesystemBackend 为 LLM 可调的 BaseTool。

设计:
- 每个工具是一个 BaseTool 子类,scope=GLOBAL(管家直接可见)
- readonly=True(只读) 或 False(可写)
- arun() 内部调 filesystem_backend 的对应方法
- 错误以 ToolResult(success=False) 形式返回,不抛异常给 LLM

注册入口:
- 任何 import 这个包的人,只要调 `register_default_skill_tools(backend)`,
  就会把 7 个 skill tool 注册到 ToolRegistry.get_default()
- 这是显式注册(不像 common/ 下的 @register_tool 靠 import 副作用)
- 由 `smartbutler.capabilities.tools.bootstrap()` 统一调度
"""
from __future__ import annotations

from smartbutler.capabilities.tools.base import BaseTool
from smartbutler.capabilities.tools.registry import ToolRegistry
from smartbutler.capabilities.tools.skills.fs_tools import build_default_skill_tools
from smartbutler.thinking.skills.filesystem_backend import SmartButlerFilesystemBackend

__all__ = [
    "register_default_skill_tools",
    "build_default_skill_tools",
]


def register_default_skill_tools(
    backend: SmartButlerFilesystemBackend,
    registry: ToolRegistry | None = None,
) -> list[BaseTool]:
    """把 7 个默认 skill tool 注册到 registry(默认 = ToolRegistry.get_default())。

    Returns:
        已注册的 tool 列表。

    Raises:
        ToolAlreadyRegisteredError: 同名 tool 已存在(同实例幂等,异实例报错)。
    """
    target = registry if registry is not None else ToolRegistry.get_default()
    tools = build_default_skill_tools(backend)
    for t in tools:
        target.register(t)
    return tools
