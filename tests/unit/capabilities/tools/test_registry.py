"""ToolRegistry 单元测试。"""

from __future__ import annotations

import pytest

from smartbutler.capabilities.tools.base import BaseTool
from smartbutler.capabilities.tools.registry import ToolRegistry
from smartbutler.capabilities.tools.types import (
    ToolAlreadyRegisteredError,
    ToolNotFoundError,
    ToolScope,
)


class _Tool(BaseTool):
    name = "test_tool"
    description = "test"

    async def arun(self) -> str:
        return "ok"


class _AgentTool(BaseTool):
    name = "home_curtain"
    description = "control curtain"
    scope = ToolScope.AGENT
    owner_agent = "home_agent"

    async def arun(self) -> str:
        return "ok"


class _SkillTool(BaseTool):
    name = "pdf_extract"
    description = "extract pdf text"
    scope = ToolScope.SKILL
    owner_skill = "pdf-summary"

    async def arun(self) -> str:
        return "ok"


class _ButlerTool(BaseTool):
    name = "daily_digest"
    description = "butler meta"
    scope = ToolScope.BUTLER

    async def arun(self) -> str:
        return "ok"


class _CommonTool(BaseTool):
    name = "hidden_one"
    description = "default hidden"
    scope = ToolScope.COMMON

    async def arun(self) -> str:
        return "ok"


@pytest.fixture
def registry() -> ToolRegistry:
    """每个测试一个全新 registry。"""
    return ToolRegistry()


class TestRegistryBasic:
    def test_register_and_get(self, registry: ToolRegistry) -> None:
        tool = _Tool()
        registry.register(tool)
        assert registry.get("test_tool") is tool

    def test_register_duplicate_raises(self, registry: ToolRegistry) -> None:
        registry.register(_Tool())
        with pytest.raises(ToolAlreadyRegisteredError):
            registry.register(_Tool())

    def test_register_same_instance_idempotent(self, registry: ToolRegistry) -> None:
        tool = _Tool()
        registry.register(tool)
        registry.register(tool)  # 同一实例不报错
        assert len(registry) == 1

    def test_get_missing_raises(self, registry: ToolRegistry) -> None:
        with pytest.raises(ToolNotFoundError):
            registry.get("missing")

    def test_try_get_returns_none(self, registry: ToolRegistry) -> None:
        assert registry.try_get("missing") is None

    def test_unregister(self, registry: ToolRegistry) -> None:
        registry.register(_Tool())
        registry.unregister("test_tool")
        assert "test_tool" not in registry

    def test_unregister_missing_raises(self, registry: ToolRegistry) -> None:
        with pytest.raises(ToolNotFoundError):
            registry.unregister("missing")

    def test_clear(self, registry: ToolRegistry) -> None:
        registry.register(_Tool())
        registry.clear()
        assert len(registry) == 0

    def test_contains(self, registry: ToolRegistry) -> None:
        registry.register(_Tool())
        assert "test_tool" in registry
        assert "missing" not in registry

    def test_list_names(self, registry: ToolRegistry) -> None:
        registry.register(_Tool())
        assert "test_tool" in registry.list_names()


class TestRegistryFiltering:
    def test_get_global_includes_global_and_common(self, registry: ToolRegistry) -> None:
        registry.register(_Tool())  # scope=GLOBAL
        registry.register(_CommonTool())  # scope=COMMON
        names = {t.name for t in registry.get_global()}
        assert names == {"test_tool", "hidden_one"}

    def test_get_global_excludes_agent_and_skill(self, registry: ToolRegistry) -> None:
        registry.register(_AgentTool())
        registry.register(_SkillTool())
        names = {t.name for t in registry.get_global()}
        assert names == set()

    def test_get_for_agent_includes_global_and_owner(self, registry: ToolRegistry) -> None:
        registry.register(_Tool())  # global
        registry.register(_AgentTool())  # owner=home_agent
        names = {t.name for t in registry.get_for_agent("home_agent")}
        assert "test_tool" in names
        assert "home_curtain" in names

    def test_get_for_agent_excludes_other_agent(self, registry: ToolRegistry) -> None:
        registry.register(_AgentTool())
        names = {t.name for t in registry.get_for_agent("other_agent")}
        assert "home_curtain" not in names

    def test_get_for_skill(self, registry: ToolRegistry) -> None:
        registry.register(_Tool())
        registry.register(_SkillTool())
        names = {t.name for t in registry.get_for_skill("pdf-summary")}
        assert names == {"test_tool", "pdf_extract"}

    def test_get_butler_includes_global_butler_skill_common(self, registry: ToolRegistry) -> None:
        registry.register(_Tool())  # global
        registry.register(_ButlerTool())  # butler
        registry.register(_SkillTool())  # skill
        registry.register(_CommonTool())  # common
        registry.register(_AgentTool())  # agent ← 排除
        names = {t.name for t in registry.get_butler()}
        assert "test_tool" in names
        assert "daily_digest" in names
        assert "pdf_extract" in names
        assert "hidden_one" in names
        assert "home_curtain" not in names


class TestRegistrySingleton:
    def test_default_singleton(self) -> None:
        ToolRegistry.reset_default()
        r1 = ToolRegistry.get_default()
        r2 = ToolRegistry.get_default()
        assert r1 is r2

    def test_reset_default(self) -> None:
        ToolRegistry.reset_default()
        r1 = ToolRegistry.get_default()
        ToolRegistry.reset_default()
        r2 = ToolRegistry.get_default()
        assert r1 is not r2
