"""Sub-Agent 层 — 公共 API。

对业务层暴露：
- ``BaseAgent``：Sub-Agent 抽象基类
- ``AgentManager``：注册 / 调度入口（Phase 4 thinking 层唯一引用）
- ``AgentInput / AgentContext / AgentOutput``：数据类型
- ``AgentError`` 及子异常

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
"""
from smartbutler.agents.base.base import BaseAgent
from smartbutler.agents.base.errors import (
    AgentAlreadyRegisteredError,
    AgentError,
    AgentInvalidInputError,
    AgentNotFoundError,
    AgentTimeoutError,
)
from smartbutler.agents.base.requires_tools import requires_tools
from smartbutler.agents.base.types import AgentContext, AgentInput, AgentOutput
from smartbutler.agents.manager.manager import AgentManager

__all__ = [
    # 抽象
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
    # 装饰器
    "requires_tools",
]
