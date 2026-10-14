"""Tool types 模块单元测试。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from smartbutler.capabilities.tools.types import (
    Permission,
    ToolContext,
    ToolResult,
    ToolScope,
    now_ms,
)


class TestToolScope:
    """ToolScope 枚举值校验。"""

    def test_all_scopes_defined(self) -> None:
        expected = {"global", "butler", "agent", "skill", "common"}
        actual = {s.value for s in ToolScope}
        assert actual == expected

    def test_scope_is_str_enum(self) -> None:
        assert isinstance(ToolScope.GLOBAL, str)
        assert ToolScope.GLOBAL == "global"


class TestPermission:
    """Permission 枚举值校验。"""

    def test_key_permissions_present(self) -> None:
        # 至少确认我们关心的几个权限都在
        assert Permission.READ_HOME_STATE in Permission
        assert Permission.WRITE_HOME_STATE in Permission
        assert Permission.NETWORK_CALL in Permission
        assert Permission.LLM_INVOKE in Permission

    def test_permission_values_are_dot_separated(self) -> None:
        # 习惯上权限用 <domain>.<action> 命名，便于权限策略引擎解析
        for p in Permission:
            assert "." in p.value or p.value in {"llm.invoke"}, (
                f"Permission '{p.value}' 不符合 <domain>.<action> 命名"
            )


class TestToolContext:
    """ToolContext 字段与校验。"""

    def test_required_fields(self) -> None:
        # user_id / session_id 必填
        with pytest.raises(ValidationError):
            ToolContext()  # type: ignore[call-arg]

    def test_optional_defaults(self) -> None:
        ctx = ToolContext(user_id="u1", session_id="s1")
        assert ctx.permissions == set()
        assert ctx.parent_agent is None
        assert ctx.parent_skill is None
        assert ctx.metadata == {}

    def test_permissions_set(self) -> None:
        ctx = ToolContext(
            user_id="u1",
            session_id="s1",
            permissions={Permission.NETWORK_CALL, Permission.READ_HOME_STATE},
            parent_agent="butler",
        )
        assert Permission.NETWORK_CALL in ctx.permissions
        assert ctx.parent_agent == "butler"

    def test_extra_forbid(self) -> None:
        with pytest.raises(ValidationError):
            ToolContext(user_id="u1", session_id="s1", unknown_field="x")  # type: ignore[call-arg]


class TestToolResult:
    """ToolResult 字段与序列化。"""

    def test_success_default(self) -> None:
        r = ToolResult(content="hello")
        assert r.success is True
        assert r.error is None
        assert r.duration_ms == 0.0
        assert r.data is None

    def test_failure_with_error(self) -> None:
        r = ToolResult(success=False, content="", error="boom")
        assert r.error == "boom"

    def test_model_dump_roundtrip(self) -> None:
        r = ToolResult(content="hello", data={"k": "v"}, duration_ms=12.5)
        dumped = r.model_dump()
        assert dumped["content"] == "hello"
        assert dumped["data"] == {"k": "v"}
        assert dumped["duration_ms"] == 12.5


class TestNowMs:
    """now_ms() 返回值是单调递增的毫秒数。"""

    def test_returns_positive_number(self) -> None:
        t = now_ms()
        assert isinstance(t, float)
        assert t > 0

    def test_monotonic_increasing(self) -> None:
        t1 = now_ms()
        t2 = now_ms()
        assert t2 >= t1
