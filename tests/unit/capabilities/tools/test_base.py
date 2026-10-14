"""BaseTool / FunctionTool 单元测试。"""

from __future__ import annotations

import asyncio

import pytest

from smartbutler.capabilities.tools.base import BaseTool, FunctionTool, _normalize_result
from smartbutler.capabilities.tools.types import (
    Permission,
    ToolContext,
    ToolPermissionDeniedError,
    ToolResult,
    ToolScope,
    ToolTimeoutError,
)

# ---------- Fixtures ----------


def _ctx(perms: set[Permission] | None = None) -> ToolContext:
    return ToolContext(user_id="u1", session_id="s1", permissions=perms or set())


# ---------- BaseTool 抽象校验 ----------


class TestBaseToolValidation:
    def test_missing_name_raises(self) -> None:
        class BadTool(BaseTool):
            name = ""
            description = "x"

            async def arun(self) -> None: ...

        with pytest.raises(ValueError, match="必须声明 name"):
            BadTool()

    def test_missing_description_allowed_but_warn(self) -> None:
        # Phase 2 修订：description 不强制(BaseTool.__init__ 不再 raise);
        # 业务层负责保证 description 有意义,LLM 路由才准。
        class NoDescTool(BaseTool):
            name = "ok"
            description = ""

            async def arun(self) -> str:
                return "ok"

        NoDescTool()  # 不抛

    def test_agent_scope_requires_owner(self) -> None:
        class BadTool(BaseTool):
            name = "ok"
            description = "x"
            scope = ToolScope.AGENT

            async def arun(self) -> None: ...

        with pytest.raises(ValueError, match="owner_agent"):
            BadTool()

    def test_skill_scope_requires_owner(self) -> None:
        class BadTool(BaseTool):
            name = "ok"
            description = "x"
            scope = ToolScope.SKILL

            async def arun(self) -> None: ...

        with pytest.raises(ValueError, match="owner_skill"):
            BadTool()

    def test_valid_global_tool(self) -> None:
        class GoodTool(BaseTool):
            name = "ok"
            description = "x"

            async def arun(self) -> str:
                return "hi"

        # 不抛
        GoodTool()


# ---------- BaseTool.ainvoke 横切关注点 ----------


class _SimpleTool(BaseTool):
    name = "simple"
    description = "returns 42"

    async def arun(self) -> int:
        return 42


class _PermTool(BaseTool):
    name = "needs_perm"
    description = "needs network"
    required_permissions = frozenset({Permission.NETWORK_CALL})

    async def arun(self) -> str:
        return "ok"


class _ReadOnlyTool(BaseTool):
    name = "readonly"
    description = "no side effects"
    readonly = True
    idempotent = True

    async def arun(self) -> str:
        return "ok"


class _SlowTool(BaseTool):
    name = "slow"
    description = "sleeps"
    timeout_seconds = 0.1

    async def arun(self) -> str:
        await asyncio.sleep(1.0)
        return "too late"


class TestAinvokePermission:
    async def test_missing_perm_raises(self) -> None:
        tool = _PermTool()
        with pytest.raises(ToolPermissionDeniedError):
            await tool.ainvoke(_ctx())

    async def test_with_perm_passes(self) -> None:
        tool = _PermTool()
        result = await tool.ainvoke(_ctx({Permission.NETWORK_CALL}))
        assert result.content == "ok"


class TestAinvokeTimeout:
    async def test_timeout_raises(self) -> None:
        tool = _SlowTool()
        with pytest.raises(ToolTimeoutError):
            await tool.ainvoke(_ctx())

    async def test_fast_call_no_timeout(self) -> None:
        class FastTool(BaseTool):
            name = "fast"
            description = "fast"
            timeout_seconds = 1.0

            async def arun(self) -> str:
                return "fast"

        result = await FastTool().ainvoke(_ctx())
        assert result.content == "fast"


class TestAinvokeResultNormalization:
    async def test_str_returned_as_content(self) -> None:
        tool = _SimpleTool()
        result = await tool.ainvoke(_ctx())
        assert result.success is True
        assert result.content == "42"  # int 走 json.dumps
        assert result.duration_ms >= 0

    async def test_dict_returned(self) -> None:
        class DictTool(BaseTool):
            name = "dict_tool"
            description = "returns dict"

            async def arun(self) -> dict[str, int]:
                return {"a": 1, "b": 2}

        result = await DictTool().ainvoke(_ctx())
        assert result.success is True
        import json

        assert json.loads(result.content) == {"a": 1, "b": 2}
        assert result.data == {"a": 1, "b": 2}

    async def test_pydantic_returned(self) -> None:
        from pydantic import BaseModel

        class Out(BaseModel):
            x: int

        class PDTool(BaseTool):
            name = "pd_tool"
            description = "returns pydantic"

            async def arun(self) -> Out:
                return Out(x=7)

        result = await PDTool().ainvoke(_ctx())
        assert result.success is True
        # Pydantic 默认无空格 JSON；按结构验证
        import json

        assert json.loads(result.content) == {"x": 7}
        assert result.data == {"x": 7}


# ---------- FunctionTool ----------


class TestFunctionTool:
    def test_sync_function_wrapped(self) -> None:
        def add(a: int, b: int = 0) -> int:
            """加法"""
            return a + b

        tool = FunctionTool(func=add)
        assert tool.name == "add"
        assert tool.description == "加法"

    def test_async_function_wrapped(self) -> None:
        async def hello(name: str = "world") -> str:
            """async hello"""
            return f"hello {name}"

        tool = FunctionTool(func=hello)
        assert tool.name == "hello"
        assert tool.description == "async hello"

    def test_async_arun(self) -> None:
        async def greet(name: str) -> str:
            """greet"""
            return f"hi {name}"

        tool = FunctionTool(func=greet)
        result = asyncio.run(tool.arun(name="alice"))
        assert result == "hi alice"

    def test_sync_arun(self) -> None:
        def add(a: int, b: int) -> int:
            """add"""
            return a + b

        tool = FunctionTool(func=add)
        result = asyncio.run(tool.arun(a=1, b=2))
        assert result == 3

    def test_args_schema_generated(self) -> None:
        def f(name: str, count: int = 1) -> str:
            """example"""
            return name * count

        tool = FunctionTool(func=f)
        schema = tool.args_schema.model_json_schema()
        assert "name" in schema["properties"]
        assert "count" in schema["properties"]
        assert "name" in schema["required"]

    def test_to_tool_spec_includes_params(self) -> None:
        def f(query: str, limit: int = 10) -> str:
            """search"""
            return query

        tool = FunctionTool(func=f)
        spec = tool.to_tool_spec()
        assert spec.function.name == "f"
        assert spec.function.parameters["properties"]["query"]["type"] == "string"
        assert spec.function.parameters["properties"]["limit"]["type"] == "integer"
        assert "query" in spec.function.parameters["required"]


class TestNormalizeResult:
    """直接测试 _normalize_result 边界。"""

    def test_str(self) -> None:
        r = _normalize_result("t", "hello", 1.0)
        assert r.content == "hello"

    def test_pydantic(self) -> None:
        from pydantic import BaseModel

        class M(BaseModel):
            a: int

        r = _normalize_result("t", M(a=5), 1.0)
        import json

        assert json.loads(r.content) == {"a": 5}
        assert r.data == {"a": 5}

    def test_tool_result_passthrough(self) -> None:
        original = ToolResult(content="hi", data={"k": "v"}, duration_ms=999.0)
        r = _normalize_result("t", original, 2.0)
        assert r.duration_ms == 2.0  # 被覆盖为新计时
        assert r.data == {"k": "v"}
