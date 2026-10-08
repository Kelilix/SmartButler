"""LangChain 适配层单元测试。"""

from __future__ import annotations

import asyncio

from smartbutler.capabilities.tools.base import BaseTool, FunctionTool
from smartbutler.capabilities.tools.langchain_adapter import (
    collect_langchain_tools,
    to_langchain_tool,
)
from smartbutler.capabilities.tools.types import (
    Permission,
    ToolContext,
    ToolError,
)


def _ctx(perms: set[Permission] | None = None) -> ToolContext:
    return ToolContext(user_id="u", session_id="s", permissions=perms or set())


class TestToLangchainTool:
    def test_function_tool_basic(self) -> None:
        def add(a: int, b: int = 0) -> int:
            """加法"""
            return a + b

        tool = FunctionTool(func=add)
        lc = to_langchain_tool(tool)
        assert lc.name == "add"
        assert lc.description == "加法"
        # args_schema 应被 StructuredTool 填充
        assert lc.args is not None

    def test_async_function_tool(self) -> None:
        async def greet(name: str) -> str:
            """async greet"""
            return f"hi {name}"

        tool = FunctionTool(func=greet)
        lc = to_langchain_tool(tool)
        # 强制 await invoke（StructuredTool.coroutine）
        result = asyncio.run(lc.ainvoke({"name": "alice"}))
        assert result == "hi alice"

    def test_base_tool_subclass(self) -> None:
        class MyTool(BaseTool):
            name = "my_tool"
            description = "x"

            async def arun(self) -> str:
                return "ok"

        lc = to_langchain_tool(MyTool())
        result = asyncio.run(lc.ainvoke({}))
        assert result == "ok"

    def test_args_schema_passed_through(self) -> None:
        def search(query: str, limit: int = 10) -> str:
            """search docs"""
            return f"q={query}&n={limit}"

        tool = FunctionTool(func=search)
        lc = to_langchain_tool(tool)
        # LangChain 把 args_schema 解析为 .args dict
        assert "query" in lc.args

    def test_collect_langchain_tools(self) -> None:
        def f1() -> str:
            """f1"""
            return "1"

        def f2() -> str:
            """f2"""
            return "2"

        tools = [FunctionTool(func=f1), FunctionTool(func=f2)]
        lc_tools = collect_langchain_tools(tools)
        assert len(lc_tools) == 2
        assert {t.name for t in lc_tools} == {"f1", "f2"}


class TestErrorPropagation:
    def test_permission_error_becomes_string(self) -> None:
        def needs_perm() -> str:
            """needs perm"""
            return "ok"

        tool = FunctionTool(
            func=needs_perm,
            required_permissions=frozenset({Permission.NETWORK_CALL}),
        )
        lc = to_langchain_tool(tool)
        # 调用时 ctx 是默认空权限的，应返回错误字符串而非 raise
        result = asyncio.run(lc.ainvoke({}))
        assert "ToolError" in result
        assert "ToolPermissionDeniedError" in result

    def test_tool_error_becomes_string(self) -> None:
        def will_fail() -> str:
            """fails"""
            raise ValueError("boom")

        # 上面的 ValueError 不是 ToolError → 走 LangChain 默认行为（这里不测）
        # 业务主动抛 ToolError 子类时，langchain adapter 包成字符串
        class MyToolError(ToolError):
            pass

        def will_raise_tool_error() -> str:
            """tool error"""
            raise MyToolError("specific error")

        tool2 = FunctionTool(func=will_raise_tool_error)
        lc2 = to_langchain_tool(tool2)
        result = asyncio.run(lc2.ainvoke({}))
        assert "MyToolError" in result
        assert "specific error" in result


class TestContextFactory:
    def test_custom_context_factory(self) -> None:
        def factory() -> ToolContext:
            return ToolContext(
                user_id="alice",
                session_id="s1",
                permissions={Permission.NETWORK_CALL},
            )

        def fetch() -> str:
            """fetch"""
            return "fetched"

        tool = FunctionTool(
            func=fetch,
            required_permissions=frozenset({Permission.NETWORK_CALL}),
        )
        lc = to_langchain_tool(tool, context_factory=factory)
        result = asyncio.run(lc.ainvoke({}))
        assert result == "fetched"


class TestArgsSchemaCorrectness:
    def test_pydantic_schema_used_for_params(self) -> None:
        def search(query: str, max_results: int = 5) -> str:
            """search web"""
            return ""

        tool = FunctionTool(func=search)
        lc = to_langchain_tool(tool)
        # args 含 query 字段
        assert "query" in lc.args
        # max_results 有默认值，可为可选
        assert "max_results" in lc.args

    def test_tool_spec_clean(self) -> None:
        def f(name: str, age: int = 0) -> str:
            """tool"""
            return ""

        tool = FunctionTool(func=f)
        spec = tool.to_tool_spec()
        params = spec.function.parameters
        assert params["type"] == "object"
        assert "name" in params["required"]
        # 不应包含 title / $defs 等噪音
        assert "title" not in params
        assert "$defs" not in params
