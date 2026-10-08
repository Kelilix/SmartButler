"""Tool 能力层 — ToolRegistry。

设计原则（参考 TECHNICAL_DESIGN.md §3.2.2 tools/）：
1. **单例 + 隔离实例双模式**：
   - 模块级默认单例（业务调用 ToolRegistry.get_default() 即可）。
   - 支持显式实例化（用于测试隔离 / 多租户）。
2. **按 scope + owner 过滤**：get_for_agent / get_for_skill / get_global 各司其职。
3. **重复名检测**：防止同名 tool 覆盖；启动期就能发现冲突。
4. **线程/异步安全**：asyncio 场景下 register / get 均无锁（CPython GIL + 单线程事件循环保证）。
"""

from __future__ import annotations

from smartbutler.capabilities.tools.base import BaseTool
from smartbutler.capabilities.tools.types import (
    ToolAlreadyRegisteredError,
    ToolNotFoundError,
    ToolScope,
)


class ToolRegistry:
    """Tool 注册表。

    用法:
        registry = ToolRegistry.get_default()
        registry.register(my_tool)
        tool = registry.get("my_tool")
        all_tools = registry.list_all()
    """

    def __init__(self) -> None:
        self._tools: dict[str, BaseTool] = {}

    # ---------- 单例访问 ----------

    _default: ToolRegistry | None = None

    @classmethod
    def get_default(cls) -> ToolRegistry:
        """获取（或惰性创建）默认 registry。"""
        if cls._default is None:
            cls._default = cls()
        return cls._default

    @classmethod
    def reset_default(cls) -> None:
        """重置默认 registry（仅测试用）。"""
        cls._default = None

    # ---------- CRUD ----------

    def register(self, tool: BaseTool) -> None:
        """注册 tool；同名已注册抛 ToolAlreadyRegisteredError。

        同一个 tool 重新注册同一个实例是幂等的（用 id() 判定）；
        同名但不同实例则抛错。
        """
        if tool.name in self._tools:
            existing = self._tools[tool.name]
            if existing is tool:
                return  # 幂等
            msg = f"Tool '{tool.name}' 已被注册（现有: {type(existing).__name__}）"
            raise ToolAlreadyRegisteredError(msg)
        self._tools[tool.name] = tool

    def unregister(self, name: str) -> None:
        """注销 tool；不存在抛 ToolNotFoundError。"""
        if name not in self._tools:
            msg = f"Tool '{name}' 未注册"
            raise ToolNotFoundError(msg)
        del self._tools[name]

    def get(self, name: str) -> BaseTool:
        """按 name 查找；不存在抛 ToolNotFoundError。"""
        if name not in self._tools:
            msg = f"Tool '{name}' 未注册"
            raise ToolNotFoundError(msg)
        return self._tools[name]

    def try_get(self, name: str) -> BaseTool | None:
        """按 name 查找，不存在返回 None。"""
        return self._tools.get(name)

    def list_all(self) -> list[BaseTool]:
        """列出所有 tool（顺序不保证）。"""
        return list(self._tools.values())

    def list_names(self) -> list[str]:
        """列出所有 tool name。"""
        return list(self._tools.keys())

    def clear(self) -> None:
        """清空所有 tool（仅测试用）。"""
        self._tools.clear()

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    def __len__(self) -> int:
        return len(self._tools)

    # ---------- 过滤 ----------

    def get_global(self) -> list[BaseTool]:
        """全局可见工具（scope=GLOBAL 或 scope=COMMON）。

        COMMON 默认不暴露，但允许管家在 system_prompt 中显式引用时拿到。
        """
        return [
            t
            for t in self._tools.values()
            if t.scope in (ToolScope.GLOBAL, ToolScope.COMMON)
        ]

    def get_for_agent(self, agent_name: str) -> list[BaseTool]:
        """给某个 Sub-Agent 用的工具集合（全局 + 该 agent 专属）。"""
        result: list[BaseTool] = []
        for t in self._tools.values():
            if t.scope in (ToolScope.GLOBAL, ToolScope.COMMON):
                result.append(t)
            elif t.scope == ToolScope.AGENT and t.owner_agent == agent_name:
                result.append(t)
        return result

    def get_for_skill(self, skill_name: str) -> list[BaseTool]:
        """给某个 Skill 用的工具集合（全局 + 该 skill 专属）。"""
        result: list[BaseTool] = []
        for t in self._tools.values():
            if t.scope in (ToolScope.GLOBAL, ToolScope.COMMON):
                result.append(t)
            elif t.scope == ToolScope.SKILL and t.owner_skill == skill_name:
                result.append(t)
        return result

    def get_butler(self) -> list[BaseTool]:
        """管家自己的工具集合（全局 + BUTLER + SKILL）。

        Sub-Agent 工具（scope=AGENT）对管家不可见 —— 管家必须通过
        delegate_to_<agent> 调用，不直接拿到原始 tool。
        """
        result: list[BaseTool] = []
        for t in self._tools.values():
            if t.scope in (
                ToolScope.GLOBAL,
                ToolScope.COMMON,
                ToolScope.BUTLER,
                ToolScope.SKILL,
            ):
                result.append(t)
        return result
