"""@register_tool 装饰器单元测试。"""

from __future__ import annotations

import asyncio

import pytest

from smartbutler.capabilities.tools.decorator import register_tool
from smartbutler.capabilities.tools.registry import ToolRegistry
from smartbutler.capabilities.tools.types import (
    Permission,
    ToolContext,
    ToolScope,
)


@pytest.fixture(autouse=True)
def _reset_registry() -> None:
    """每个测试清空默认 registry。"""
    ToolRegistry.reset_default()
    yield
    ToolRegistry.reset_default()


def _ctx(perms: set[Permission] | None = None) -> ToolContext:
    return ToolContext(user_id="u", session_id="s", permissions=perms or set())


class TestBasicDecoration:
    def test_sync_function(self) -> None:
        @register_tool()
        def add(a: int, b: int = 0) -> int:
            """加法"""
            return a + b

        registry = ToolRegistry.get_default()
        assert "add" in registry
        tool = registry.get("add")
        assert tool.description == "加法"
        assert tool.scope == ToolScope.GLOBAL

    def test_async_function(self) -> None:
        @register_tool()
        async def greet(name: str) -> str:
            """greet async"""
            return f"hi {name}"

        tool = ToolRegistry.get_default().get("greet")
        result = asyncio.run(tool.arun(name="alice"))
        assert result == "hi alice"

    def test_original_function_still_callable(self) -> None:
        @register_tool()
        def double(x: int) -> int:
            return x * 2

        # 装饰器透传：既可加 return 仍可正常调用
        assert double(3) == 6

    def test_tool_attached_to_function(self) -> None:
        @register_tool()
        def my_tool() -> str:
            return "x"

        assert hasattr(my_tool, "tool")
        assert my_tool.tool.name == "my_tool"


class TestDecoratorParams:
    def test_custom_name(self) -> None:
        @register_tool(name="custom_name")
        def func() -> str:
            return "x"

        registry = ToolRegistry.get_default()
        assert "custom_name" in registry
        assert "func" not in registry

    def test_owner_required_with_agent_scope(self) -> None:
        with pytest.raises(ValueError, match="owner"):

            @register_tool(scope=ToolScope.AGENT)
            def f() -> str:
                return "x"

    def test_owner_required_with_skill_scope(self) -> None:
        with pytest.raises(ValueError, match="owner"):

            @register_tool(scope=ToolScope.SKILL)
            def f() -> str:
                return "x"

    def test_agent_scope_with_owner(self) -> None:
        @register_tool(scope=ToolScope.AGENT, owner="home_agent")
        def curtain_close() -> str:
            return "closed"

        tool = ToolRegistry.get_default().get("curtain_close")
        assert tool.scope == ToolScope.AGENT
        assert tool.owner_agent == "home_agent"
        assert tool.owner_skill is None

    def test_skill_scope_with_owner(self) -> None:
        @register_tool(scope=ToolScope.SKILL, owner="pdf-summary")
        def pdf_extract() -> str:
            return "ok"

        tool = ToolRegistry.get_default().get("pdf_extract")
        assert tool.scope == ToolScope.SKILL
        assert tool.owner_skill == "pdf-summary"
        assert tool.owner_agent is None

    def test_required_permissions(self) -> None:
        @register_tool(required_permissions=[Permission.NETWORK_CALL])
        def fetch() -> str:
            return "x"

        tool = ToolRegistry.get_default().get("fetch")
        assert Permission.NETWORK_CALL in tool.required_permissions

    def test_timeout(self) -> None:
        @register_tool(timeout_seconds=0.5)
        def f() -> str:
            return "x"

        tool = ToolRegistry.get_default().get("f")
        assert tool.timeout_seconds == 0.5

    def test_readonly_idempotent_flags(self) -> None:
        @register_tool(readonly=True, idempotent=True)
        def f() -> str:
            return "x"

        tool = ToolRegistry.get_default().get("f")
        assert tool.readonly is True
        assert tool.idempotent is True


class TestDecoratorRuntimeIntegration:
    def test_invoke_via_ainvoke(self) -> None:
        @register_tool()
        def multiply(a: int, b: int) -> int:
            """mul"""
            return a * b

        tool = ToolRegistry.get_default().get("multiply")
        result = asyncio.run(tool.ainvoke(_ctx(), a=3, b=4))
        assert result.success is True
        assert "12" in result.content

    def test_invoke_with_permission_check(self) -> None:
        @register_tool(required_permissions=[Permission.FILE_WRITE])
        def f() -> str:
            return "ok"

        tool = ToolRegistry.get_default().get("f")
        # 无权限应抛
        with pytest.raises(Exception):
            asyncio.run(tool.ainvoke(_ctx()))

    def test_register_same_tool_idempotent(self) -> None:
        @register_tool()
        def f() -> str:
            return "x"

        # 第二次 import 同 module 不会报错（FunctionTool 实例相同）
        # 这里我们手动模拟：再调一次装饰器
        # 注意：装饰器在 import 时已执行；要测试重复注册得用不同方式
        # 直接用 registry.register 测试
        tool = ToolRegistry.get_default().get("f")
        ToolRegistry.get_default().register(tool)  # 同一实例不报错
