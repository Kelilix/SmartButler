"""BaseAgent 契约 + to_langchain_tool 单元测试。"""
from __future__ import annotations

import pytest

from smartbutler.agents.base.base import BaseAgent
from smartbutler.agents.base.requires_tools import requires_tools
from smartbutler.agents.base.types import AgentContext, AgentInput, AgentOutput


class _DummyAgent(BaseAgent):
    """最小可实例化的 Sub-Agent 用于测试。"""

    name = "dummy_agent"
    description = "用于单元测试的占位 Agent。"

    def __init__(self, return_value: AgentOutput) -> None:
        super().__init__()
        self._return = return_value
        self.calls: list[tuple[AgentInput, AgentContext]] = []

    async def handle(self, input: AgentInput, ctx: AgentContext) -> AgentOutput:
        self.calls.append((input, ctx))
        return self._return


class TestBaseAgentContract:
    def test_must_declare_name(self) -> None:
        class _NoName(BaseAgent):
            description = "x"

            async def handle(self, input: AgentInput, ctx: AgentContext) -> AgentOutput:
                return AgentOutput(content="")

        with pytest.raises(ValueError, match="必须声明 name"):
            _NoName()

    def test_must_declare_description(self) -> None:
        class _NoDesc(BaseAgent):
            name = "x"

            async def handle(self, input: AgentInput, ctx: AgentContext) -> AgentOutput:
                return AgentOutput(content="")

        with pytest.raises(ValueError, match="必须声明 description"):
            _NoDesc()

    def test_tools_property_returns_copy(self) -> None:
        agent = _DummyAgent(AgentOutput(content="ok"))
        tools = agent.tools
        assert tools == []
        # 修改返回 list 不影响内部
        tools.append("fake")  # type: ignore[arg-type]
        assert agent.tools == []


class TestAinvokeEmptyRaw:
    @pytest.mark.asyncio
    async def test_empty_raw_returns_failure(self) -> None:
        agent = _DummyAgent(AgentOutput(content="ok"))
        result = await agent.ainvoke(
            AgentInput(raw=""),
            AgentContext(user_id="u", session_id="s"),
        )
        assert result.success is False
        assert result.error is not None and "raw 不能为空" in result.error
        # handle 不应被调用
        assert agent.calls == []


class TestAinvokeSuccess:
    @pytest.mark.asyncio
    async def test_passes_through_success_output(self) -> None:
        ok = AgentOutput(content="done", data={"k": 1}, tool_calls=["a", "b"])
        agent = _DummyAgent(ok)
        result = await agent.ainvoke(
            AgentInput(raw="hi"),
            AgentContext(user_id="u1", session_id="s1"),
        )
        assert result.success is True
        assert result.content == "done"
        assert result.data == {"k": 1}
        assert result.tool_calls == ["a", "b"]


class TestAinvokeTimeout:
    @pytest.mark.asyncio
    async def test_timeout_returns_failure(self) -> None:
        class _SlowAgent(BaseAgent):
            name = "slow"
            description = "永远超时"
            timeout_seconds = 0.05

            async def handle(self, input: AgentInput, ctx: AgentContext) -> AgentOutput:
                import asyncio

                await asyncio.sleep(1.0)
                return AgentOutput(content="never")

        agent = _SlowAgent()
        result = await agent.ainvoke(
            AgentInput(raw="hi"),
            AgentContext(user_id="u", session_id="s"),
        )
        assert result.success is False
        assert result.error is not None
        assert "超时" in result.error
        assert "TimeoutError" in result.error or "AgentTimeoutError" in result.error


class TestAinvokeException:
    @pytest.mark.asyncio
    async def test_handle_exception_caught(self) -> None:
        class _BoomAgent(BaseAgent):
            name = "boom"
            description = "boom"

            async def handle(self, input: AgentInput, ctx: AgentContext) -> AgentOutput:
                raise RuntimeError("kaboom")

        agent = _BoomAgent()
        result = await agent.ainvoke(
            AgentInput(raw="hi"),
            AgentContext(user_id="u", session_id="s"),
        )
        assert result.success is False
        assert result.error is not None
        assert "RuntimeError" in result.error
        assert "kaboom" in result.error


class TestAinvokeRetry:
    @pytest.mark.asyncio
    async def test_retry_then_success(self) -> None:
        call_count = 0

        class _FlakyAgent(BaseAgent):
            name = "flaky"
            description = "第二次成功"
            max_retries = 2

            async def handle(self, input: AgentInput, ctx: AgentContext) -> AgentOutput:
                nonlocal call_count
                call_count += 1
                if call_count < 2:
                    raise RuntimeError("transient")
                return AgentOutput(content="eventually")

        agent = _FlakyAgent()
        result = await agent.ainvoke(
            AgentInput(raw="hi"),
            AgentContext(user_id="u", session_id="s"),
        )
        assert result.success is True
        assert result.content == "eventually"
        assert call_count == 2


class TestToLangchainTool:
    @pytest.mark.asyncio
    async def test_delegate_tool_name_format(self) -> None:
        agent = _DummyAgent(AgentOutput(content="ok"))
        tool = agent.to_langchain_tool()
        assert tool.name == "delegate_to_dummy_agent"
        assert "用于单元测试" in (tool.description or "")

    @pytest.mark.asyncio
    async def test_delegate_tool_invokes_handle(self) -> None:
        agent = _DummyAgent(AgentOutput(content="hello from agent"))
        tool = agent.to_langchain_tool()
        result = await tool.ainvoke({"task": "do something"})
        assert result == "hello from agent"
        assert len(agent.calls) == 1
        assert agent.calls[0][0].raw == "do something"
        # AgentContext 默认 parent_agent=butler
        assert agent.calls[0][1].parent_agent == "butler"

    @pytest.mark.asyncio
    async def test_delegate_tool_error_returns_string(self) -> None:
        class _FailingAgent(BaseAgent):
            name = "failing"
            description = "always fails"

            async def handle(self, input: AgentInput, ctx: AgentContext) -> AgentOutput:
                return AgentOutput(content="", success=False, error="intentional")

        tool = _FailingAgent().to_langchain_tool()
        result = await tool.ainvoke({"task": "anything"})
        assert "[AgentError]" in result
        assert "intentional" in result

    @pytest.mark.asyncio
    async def test_delegate_tool_exception_returns_string(self) -> None:
        class _CrashAgent(BaseAgent):
            name = "crash"
            description = "always crashes"

            async def handle(self, input: AgentInput, ctx: AgentContext) -> AgentOutput:
                raise RuntimeError("boom")

        tool = _CrashAgent().to_langchain_tool()
        result = await tool.ainvoke({"task": "anything"})
        assert "[AgentError]" in result
        assert "boom" in result


class TestBuildToolContext:
    def test_converts_permissions_list_to_set(self) -> None:
        from smartbutler.capabilities.tools.types import Permission

        agent = _DummyAgent(AgentOutput(content="ok"))
        ctx = agent._build_tool_context(
            AgentContext(
                user_id="u",
                session_id="s",
                permissions=[Permission.READ_HOME_STATE, Permission.WRITE_HOME_STATE],
                parent_agent="butler",
            )
        )
        assert ctx.user_id == "u"
        assert ctx.session_id == "s"
        assert ctx.permissions == {Permission.READ_HOME_STATE, Permission.WRITE_HOME_STATE}
        assert ctx.parent_agent == "butler"

    def test_falls_back_to_own_name_when_no_parent(self) -> None:
        agent = _DummyAgent(AgentOutput(content="ok"))
        ctx = agent._build_tool_context(
            AgentContext(user_id="u", session_id="s", parent_agent=None)
        )
        assert ctx.parent_agent == "dummy_agent"


class TestRequiresToolsDecorator:
    """@requires_tools 装饰器 + BaseAgent.__init_subclass__ 校验。"""

    def test_decorator_writes_required_tool_names(self) -> None:

        @requires_tools("a", "b")
        class _A(BaseAgent):
            name = "a"
            description = "x"

            async def handle(  # noqa: D401
                self, input: AgentInput, ctx: AgentContext
            ) -> AgentOutput:
                return AgentOutput(content="")

        assert _A.required_tool_names == ["a", "b"]

    def test_classvar_form_works_too(self) -> None:
        """也可以不装饰器,直接 ClassVar 声明。"""

        class _B(BaseAgent):
            name = "b"
            description = "x"
            required_tool_names: list[str] = ["x", "y"]

            async def handle(  # noqa: D401
                self, input: AgentInput, ctx: AgentContext
            ) -> AgentOutput:
                return AgentOutput(content="")

        assert _B.required_tool_names == ["x", "y"]

    def test_decorator_rejects_empty_string(self) -> None:

        with pytest.raises(TypeError, match="非空字符串"):
            requires_tools("")

    def test_decorator_rejects_duplicates(self) -> None:

        with pytest.raises(ValueError, match="重复项"):
            requires_tools("a", "b", "a")

    def test_subclass_validation_rejects_non_list(self) -> None:
        with pytest.raises(TypeError, match="必须是 list"):

            class _Bad(BaseAgent):
                name = "bad"
                description = "x"
                required_tool_names = "not-a-list"  # type: ignore[assignment]

                async def handle(  # noqa: D401
                    self, input: AgentInput, ctx: AgentContext
                ) -> AgentOutput:
                    return AgentOutput(content="")

    def test_subclass_validation_rejects_non_string(self) -> None:
        with pytest.raises(ValueError, match="非空字符串"):

            class _Bad(BaseAgent):
                name = "bad"
                description = "x"
                required_tool_names: list = [123, "ok"]  # type: ignore[assignment]

                async def handle(  # noqa: D401
                    self, input: AgentInput, ctx: AgentContext
                ) -> AgentOutput:
                    return AgentOutput(content="")

    def test_subclass_validation_rejects_duplicate(self) -> None:
        with pytest.raises(ValueError, match="重复项"):

            class _Bad(BaseAgent):
                name = "bad"
                description = "x"
                required_tool_names: list[str] = ["a", "b", "a"]

                async def handle(  # noqa: D401
                    self, input: AgentInput, ctx: AgentContext
                ) -> AgentOutput:
                    return AgentOutput(content="")

    def test_required_tool_names_is_frozen_per_subclass(self) -> None:
        """__init_subclass__ 应把 list 替换成新 list,防止类级 list 共享。"""

        @requires_tools("x")
        class _A(BaseAgent):
            name = "a"
            description = "x"

            async def handle(  # noqa: D401
                self, input: AgentInput, ctx: AgentContext
            ) -> AgentOutput:
                return AgentOutput(content="")

        # 不再是父类那个空 list 的同一对象
        assert _A.required_tool_names is not BaseAgent.required_tool_names


class TestBaseAgentResolveTools:
    """__init__ 自动从 registry 解析 required_tool_names 的契约。"""

    def test_missing_required_tool_raises_runtime_error(self) -> None:
        """声明的 required tool 在空 registry 找不到 → __init__ 抛 RuntimeError。"""
        from smartbutler.capabilities.tools.registry import ToolRegistry

        class _Bad(BaseAgent):
            name = "bad"
            description = "x"
            required_tool_names: list[str] = ["definitely_not_registered"]

            async def handle(  # noqa: D401
                self, input: AgentInput, ctx: AgentContext
            ) -> AgentOutput:
                return AgentOutput(content="")

        with pytest.raises(RuntimeError, match="definitely_not_registered"):
            _Bad(registry=ToolRegistry())

    def test_empty_required_tool_names_is_ok(self) -> None:
        """不依赖任何 tool 的 Sub-Agent 应该能正常实例化。"""
        agent = _DummyAgent(AgentOutput(content="ok"))
        assert agent.tools == []
