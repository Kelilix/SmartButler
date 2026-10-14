"""AgentManager 单元测试。"""
from __future__ import annotations

import pytest

from smartbutler.agents import AgentManager
from smartbutler.agents.base.base import BaseAgent
from smartbutler.agents.base.errors import AgentAlreadyRegisteredError, AgentNotFoundError
from smartbutler.agents.base.types import AgentContext, AgentInput, AgentOutput


class _StubAgent(BaseAgent):
    name: str = ""
    description: str = "stub"

    def __init__(self, name: str) -> None:
        # 注意: name 在 BaseAgent.__init__ 中通过 self.name 校验,
        # 但 ClassVar 覆写后不会生效,这里用实例属性绕过。
        self.name = name
        BaseAgent.__init__(self)
        self.invocations: list[str] = []

    async def handle(self, input: AgentInput, ctx: AgentContext) -> AgentOutput:
        self.invocations.append(input.raw)
        return AgentOutput(content=f"echo: {input.raw}")


class TestRegisterAndLookup:
    def test_register_and_get(self, fresh_manager: AgentManager) -> None:
        a = _StubAgent("alpha")
        fresh_manager.register(a)
        assert fresh_manager.get("alpha") is a
        assert "alpha" in fresh_manager
        assert len(fresh_manager) == 1

    def test_register_is_idempotent_same_instance(self, fresh_manager: AgentManager) -> None:
        a = _StubAgent("alpha")
        fresh_manager.register(a)
        fresh_manager.register(a)  # 不应抛错
        assert len(fresh_manager) == 1

    def test_register_same_name_different_instance_raises(
        self, fresh_manager: AgentManager
    ) -> None:
        a1 = _StubAgent("dup")
        a2 = _StubAgent("dup")
        fresh_manager.register(a1)
        with pytest.raises(AgentAlreadyRegisteredError):
            fresh_manager.register(a2)

    def test_get_unknown_raises(self, fresh_manager: AgentManager) -> None:
        with pytest.raises(AgentNotFoundError):
            fresh_manager.get("ghost")

    def test_try_get_returns_none_when_missing(
        self, fresh_manager: AgentManager
    ) -> None:
        assert fresh_manager.try_get("ghost") is None

    def test_unregister_removes(self, fresh_manager: AgentManager) -> None:
        fresh_manager.register(_StubAgent("temp"))
        fresh_manager.unregister("temp")
        assert "temp" not in fresh_manager

    def test_unregister_unknown_raises(self, fresh_manager: AgentManager) -> None:
        with pytest.raises(AgentNotFoundError):
            fresh_manager.unregister("ghost")

    def test_list_all_and_names(self, fresh_manager: AgentManager) -> None:
        a = _StubAgent("a")
        b = _StubAgent("b")
        fresh_manager.register(a)
        fresh_manager.register(b)
        assert set(fresh_manager.list_names()) == {"a", "b"}
        assert set(agent.name for agent in fresh_manager.list_all()) == {"a", "b"}

    def test_clear(self, fresh_manager: AgentManager) -> None:
        fresh_manager.register(_StubAgent("a"))
        fresh_manager.clear()
        assert len(fresh_manager) == 0


class TestGetDelegateTools:
    def test_empty_manager_returns_empty_list(self, fresh_manager: AgentManager) -> None:
        assert fresh_manager.get_delegate_tools() == []

    def test_returns_one_tool_per_agent(self, fresh_manager: AgentManager) -> None:
        fresh_manager.register(_StubAgent("alpha"))
        fresh_manager.register(_StubAgent("beta"))
        tools = fresh_manager.get_delegate_tools()
        assert len(tools) == 2
        names = {t.name for t in tools}
        assert names == {"delegate_to_alpha", "delegate_to_beta"}


class TestSingleton:
    def test_get_default_is_singleton(self) -> None:
        AgentManager.reset_default()
        m1 = AgentManager.get_default()
        m2 = AgentManager.get_default()
        assert m1 is m2
        AgentManager.reset_default()

    def test_reset_default(self) -> None:
        AgentManager.reset_default()
        m1 = AgentManager.get_default()
        AgentManager.reset_default()
        m2 = AgentManager.get_default()
        assert m1 is not m2
        AgentManager.reset_default()
