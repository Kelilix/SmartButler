"""Sub-Agent 层 — AgentManager。

设计原则（参考 TECHNICAL_DESIGN.md §3.2.3 + ADR-005）：
1. **单例 + 显式实例双模式**：与 ToolRegistry 对齐，便于测试隔离。
2. **唯一对外入口**：Phase 4 thinking 层只与 Manager 接口交互，不直接 import 具体 Agent。
3. **重复名检测**：启动期就发现冲突；同一个 agent 重新注册同一实例是幂等的。
4. **get_delegate_tools() 关键**：把所有 Agent 包成 ``delegate_to_<name>`` 工具集，
   Phase 4 注入管家 LLM 的 bind_tools() 列表。
5. **可选 auto-discover**：Phase 3 默认开启，扫 smartbutler.agents.* 子包，
   自动 register 已 import 的 Agent。
"""
from __future__ import annotations

import importlib
import pkgutil
from typing import Any

import structlog

from smartbutler.agents.base.base import BaseAgent
from smartbutler.agents.base.errors import AgentAlreadyRegisteredError, AgentNotFoundError

_logger = structlog.get_logger(__name__)


class AgentManager:
    """Sub-Agent 注册表 / 调度入口。

    用法::

        manager = AgentManager.get_default()
        manager.register(MyAgent())
        all_agents = manager.list_all()
        delegate_tools = manager.get_delegate_tools()  # 给 Phase 4 管家
    """

    def __init__(self) -> None:
        self._agents: dict[str, BaseAgent] = {}

    # ---------- 单例访问 ----------

    _default: AgentManager | None = None

    @classmethod
    def get_default(cls) -> AgentManager:
        """获取（或惰性创建）默认 manager。"""
        if cls._default is None:
            cls._default = cls()
        return cls._default

    @classmethod
    def reset_default(cls) -> None:
        """重置默认 manager（仅测试用）。"""
        cls._default = None

    # ---------- CRUD ----------

    def register(self, agent: BaseAgent) -> None:
        """注册 agent；同名已注册抛 AgentAlreadyRegisteredError。

        同一个 agent 重新注册同一实例是幂等的（用 id() 判定）。
        """
        if agent.name in self._agents:
            existing = self._agents[agent.name]
            if existing is agent:
                return
            msg = (
                f"Agent {agent.name!r} 已被注册"
                f"（现有: {type(existing).__name__}）"
            )
            raise AgentAlreadyRegisteredError(msg)
        self._agents[agent.name] = agent
        _logger.info(
            "agent.registered",
            agent_name=agent.name,
            agent_class=type(agent).__name__,
            tool_count=len(agent.tools),
        )

    def unregister(self, name: str) -> None:
        """注销 agent；不存在抛 AgentNotFoundError。"""
        if name not in self._agents:
            msg = f"Agent {name!r} 未注册"
            raise AgentNotFoundError(msg)
        del self._agents[name]

    def get(self, name: str) -> BaseAgent:
        """按 name 查找；不存在抛 AgentNotFoundError。"""
        if name not in self._agents:
            msg = f"Agent {name!r} 未注册"
            raise AgentNotFoundError(msg)
        return self._agents[name]

    def try_get(self, name: str) -> BaseAgent | None:
        """按 name 查找，不存在返回 None。"""
        return self._agents.get(name)

    def list_all(self) -> list[BaseAgent]:
        """列出所有 agent（顺序不保证）。"""
        return list(self._agents.values())

    def list_names(self) -> list[str]:
        """列出所有 agent name。"""
        return list(self._agents.keys())

    def clear(self) -> None:
        """清空所有 agent（仅测试用）。"""
        self._agents.clear()

    def __contains__(self, name: str) -> bool:
        return name in self._agents

    def __len__(self) -> int:
        return len(self._agents)

    # ---------- 工具视图（Phase 4 关键接口） ----------

    def get_delegate_tools(self) -> list[Any]:  # noqa: ANN401
        """把所有 Sub-Agent 包装成 LangChain StructuredTool 列表。

        返回类型故意是 ``Any``：实际类型是 ``list[StructuredTool]``，
        本模块不静态引用以保持业务层零 LangChain 依赖。
        """
        return [agent.to_langchain_tool() for agent in self._agents.values()]

    # ---------- 自动发现 ----------

    def auto_discover(self, package: str = "smartbutler.agents") -> int:
        """遍历 ``package`` 下所有子模块，import 它们以触发其模块级 ``register`` 调用。

        子 Agent 约定：
        - 在子包 ``__init__.py`` 里写 ``register_default(MyAgent())``，
          ``auto_discover()`` 触发 import 后自动生效。

        Args:
            package: 要扫描的包路径。

        Returns:
            成功 import 的子模块数量。
        """
        try:
            pkg = importlib.import_module(package)
        except ImportError as exc:
            _logger.warning("agent.auto_discover.import_failed", package=package, error=str(exc))
            return 0

        count = 0
        for _finder, name, _is_pkg in pkgutil.walk_packages(pkg.__path__, prefix=f"{pkg.__name__}."):
            try:
                importlib.import_module(name)
                count += 1
            except Exception as exc:  # noqa: BLE001 - 任意模块错误不阻塞
                _logger.warning(
                    "agent.auto_discover.module_failed", module=name, error=str(exc)
                )
        _logger.info("agent.auto_discover.done", package=package, modules_imported=count)
        return count


__all__ = ["AgentManager"]
