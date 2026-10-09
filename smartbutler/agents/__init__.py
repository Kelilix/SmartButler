"""Sub-Agent 层 — 公共 API。

对业务层暴露：
- ``AgentManager``：注册 / 调度入口（Phase 4 thinking 层唯一引用）
- ``AgentInput / AgentContext / AgentOutput``：数据类型
- ``AgentError`` 及子异常
- ``BaseAgent`` / ``requires_tools``：抽象基类与装饰器（**Lazy import**,
  见下方 NOTE）

典型用法::

    from smartbutler.agents import AgentManager, AgentInput, AgentContext
    from smartbutler.agents.time import TestTimeAgent
    from smartbutler.capabilities.llm import create_llm
    from smartbutler.config.llm import load_llm_settings

    llm = create_llm(load_llm_settings())
    manager = AgentManager.get_default()
    manager.register(TestTimeAgent(llm=llm))

NOTE: 现阶段仅保留一个可运行的 TestTimeAgent 用于驱动 Phase 4 集成测试,
      真实 Sub-Agent（HomeAgent / ScheduleAgent / SearchAgent）见 Phase 3.5
      (Emotion 后的核心 Sub-Agent 落地)。

Phase 6.1 修订:BaseAgent / requires_tools 改为 lazy import (PEP 562)。
原因:capabilities.tools 链路会反向 import agents.base.base,
顶层 import BaseAgent 会触发循环。lazy 让首次属性访问时 base 已完成初始化。
业务层 ``from smartbutler.agents import BaseAgent`` 仍正常使用。
"""
from smartbutler.agents.base.errors import (
    AgentAlreadyRegisteredError,
    AgentError,
    AgentInvalidInputError,
    AgentNotFoundError,
    AgentTimeoutError,
)
from smartbutler.agents.base.types import AgentContext, AgentInput, AgentOutput
from smartbutler.agents.manager.manager import AgentManager


def __getattr__(name: str):  # type: ignore[no-untyped-def]
    """PEP 562 lazy import: BaseAgent / requires_tools 不在顶层 import。"""
    if name == "BaseAgent":
        from smartbutler.agents.base.base import BaseAgent

        return BaseAgent
    if name == "requires_tools":
        from smartbutler.agents.base.requires_tools import requires_tools

        return requires_tools
    raise AttributeError(f"module 'smartbutler.agents' has no attribute {name!r}")


__all__ = [
    # 抽象（lazy）
    "BaseAgent",
    # 数据类型
    "AgentInput",
    "AgentContext",
    "AgentOutput",
    # 异常
    "AgentError",
    "AgentNotFoundError",
    "AgentAlreadyRegisteredError",
    "AgentTimeoutError",
    "AgentInvalidInputError",
    # Manager
    "AgentManager",
    # 装饰器（lazy）
    "requires_tools",
]
