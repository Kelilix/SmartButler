"""TestTimeAgent 单元测试 (用 FakeLLM 模拟 LLM 决策)。"""
from __future__ import annotations

import json

import pytest

from smartbutler.agents.base.types import AgentContext, AgentInput
from smartbutler.agents.time.test_time_agent import TestTimeAgent
from tests.unit.agents.conftest import FakeLLM, make_response, make_tool_call


def _content_of(llm: FakeLLM, idx: int) -> str:
    """从 llm.last_messages 拿第 idx 条消息的 content,做 None 收窄。"""
    assert llm.last_messages is not None, "llm.last_messages is None"
    msg = llm.last_messages[idx]
    assert msg.content is not None, f"messages[{idx}].content is None"
    return msg.content


def _ctx_with_perms() -> AgentContext:
    return AgentContext(user_id="u1", session_id="s1", parent_agent="butler")


class TestTimeAgentInit:
    def test_metadata(self) -> None:
        llm = FakeLLM()
        agent = TestTimeAgent(llm=llm)
        assert agent.name == "test_time_agent"
        assert "时间" in agent.description
        # 默认 get_current_time 已 register 进 ToolRegistry.get_default()
        assert len(agent.tools) == 1

    def test_tool_names(self) -> None:
        agent = TestTimeAgent(llm=FakeLLM())
        names = {t.name for t in agent.tools}
        assert names == {"get_current_time"}

    def test_tool_specs_built(self) -> None:
        agent = TestTimeAgent(llm=FakeLLM())
        assert len(agent._tool_specs) == 1
        assert all(spec.type == "function" for spec in agent._tool_specs)

    def test_requires_tools_decorator_declared(self) -> None:
        """@requires_tools 装饰器必须把 tool 名写到 ClassVar。"""
        assert TestTimeAgent.required_tool_names == ["get_current_time"]


class TestTimeAgentNoToolCall:
    @pytest.mark.asyncio
    async def test_direct_response(self) -> None:
        llm = FakeLLM([make_response(content="现在大约下午三点")])
        agent = TestTimeAgent(llm=llm)
        result = await agent.ainvoke(AgentInput(raw="现在几点了?"), _ctx_with_perms())
        assert result.success is True
        assert "三点" in result.content
        assert result.tool_calls == []
        assert llm.call_count() == 1

    @pytest.mark.asyncio
    async def test_system_prompt_passed_in(self) -> None:
        llm = FakeLLM([make_response(content="好")])
        agent = TestTimeAgent(llm=llm)
        await agent.ainvoke(AgentInput(raw="hi"), _ctx_with_perms())
        assert llm.last_messages is not None
        assert llm.last_messages[0].role.value == "system"
        sys_prompt = _content_of(llm, 0)
        assert "test_time_agent" in sys_prompt
        assert "get_current_time" in sys_prompt
        assert llm.last_messages[1].role.value == "user"
        user_msg = _content_of(llm, 1)
        assert user_msg == "hi"


class TestTimeAgentWithToolCall:
    @pytest.mark.asyncio
    async def test_one_tool_call_then_response(self) -> None:
        """LLM 调一次 get_current_time 后给最终回复。"""
        llm = FakeLLM(
            [
                make_response(
                    content="我先查一下",
                    tool_calls=[
                        make_tool_call(
                            "call_1", "get_current_time", json.dumps({"timezone_name": "Asia/Shanghai"})
                        ),
                    ],
                ),
                make_response(content="现在是北京时间 2026-10-08T15:00:00+08:00"),
            ]
        )
        agent = TestTimeAgent(llm=llm)
        result = await agent.ainvoke(AgentInput(raw="现在北京时间几点?"), _ctx_with_perms())
        assert result.success is True
        assert "北京时间" in result.content
        assert result.tool_calls == ["get_current_time"]
        assert llm.call_count() == 2

    @pytest.mark.asyncio
    async def test_tool_message_includes_real_iso_result(self) -> None:
        """tool 出的字符串回流给 LLM,LLM 第二轮能拿到 ISO 时间。"""
        llm = FakeLLM(
            [
                make_response(
                    tool_calls=[
                        make_tool_call(
                            "call_1", "get_current_time", json.dumps({"timezone_name": "UTC"})
                        )
                    ]
                ),
                make_response(content="ok"),
            ]
        )
        agent = TestTimeAgent(llm=llm)
        await agent.ainvoke(AgentInput(raw="查时间"), _ctx_with_perms())
        second_call_messages = llm.last_messages
        assert second_call_messages is not None
        tool_msgs = [m for m in second_call_messages if m.role.value == "tool"]
        assert len(tool_msgs) == 1
        assert tool_msgs[0].tool_call_id == "call_1"
        # 真实工具返回的是 ISO 8601 字符串,test 期间也能跑
        assert tool_msgs[0].content is not None
        # ISO 8601 必含 "T" + "+" (timezone offset) 或 "Z"
        assert "T" in tool_msgs[0].content
        assert "+" in tool_msgs[0].content or "Z" in tool_msgs[0].content


class TestTimeAgentErrorHandling:
    @pytest.mark.asyncio
    async def test_unknown_tool_returns_error_string(self) -> None:
        llm = FakeLLM(
            [
                make_response(
                    tool_calls=[make_tool_call("c1", "some_other_tool", "{}")]
                ),
                make_response(content="抱歉,我没有这个工具"),
            ]
        )
        agent = TestTimeAgent(llm=llm)
        result = await agent.ainvoke(AgentInput(raw="干别的"), _ctx_with_perms())
        assert result.success is True
        assert result.content is not None
        # LLM 第二次看到 tool result 含错误
        assert llm.last_messages is not None
        tool_msg = next(m for m in llm.last_messages if m.role.value == "tool")
        assert tool_msg.content is not None and "未知工具" in tool_msg.content

    @pytest.mark.asyncio
    async def test_invalid_args_returns_error(self) -> None:
        llm = FakeLLM(
            [
                make_response(
                    tool_calls=[
                        make_tool_call("c1", "get_current_time", "not-a-json")
                    ]
                ),
                make_response(content="参数有问题"),
            ]
        )
        agent = TestTimeAgent(llm=llm)
        result = await agent.ainvoke(AgentInput(raw="查时间"), _ctx_with_perms())
        assert result.success is True
        assert llm.last_messages is not None
        tool_msg = next(m for m in llm.last_messages if m.role.value == "tool")
        assert tool_msg.content is not None and "JSON" in tool_msg.content

    @pytest.mark.asyncio
    async def test_bad_timezone_reflows_error_to_llm(self) -> None:
        """非法 IANA 时区 → 真实 tool 抛 ToolError → 包成字符串回流到 LLM。"""
        llm = FakeLLM(
            [
                make_response(
                    tool_calls=[
                        make_tool_call(
                            "c1", "get_current_time", json.dumps({"timezone_name": "Mars/Olympus"})
                        )
                    ]
                ),
                make_response(content="抱歉,Mars/Olympus 不是合法时区"),
            ]
        )
        agent = TestTimeAgent(llm=llm)
        result = await agent.ainvoke(AgentInput(raw="火星几点?"), _ctx_with_perms())
        assert result.success is True
        assert llm.last_messages is not None
        tool_msg = next(m for m in llm.last_messages if m.role.value == "tool")
        assert tool_msg.content is not None and "ToolError" in tool_msg.content


class TestTimeAgentMaxIterations:
    @pytest.mark.asyncio
    async def test_hits_max_iterations_returns_failure(self) -> None:
        # 5 次 tool call,没有收敛 → 第 6 次没人响应
        tool_responses = [
            make_response(
                tool_calls=[
                    make_tool_call(f"c{i}", "get_current_time", json.dumps({"timezone_name": "UTC"}))
                ]
            )
            for i in range(5)
        ]
        llm = FakeLLM(tool_responses)
        agent = TestTimeAgent(llm=llm)
        # 减少 max_internal_iterations 让测试更快
        agent.max_internal_iterations = 3
        result = await agent.ainvoke(AgentInput(raw="loop"), _ctx_with_perms())
        assert result.success is False
        assert result.error is not None and "max_internal_iterations" in result.error
        assert llm.call_count() == 3


class TestTimeAgentDelegation:
    @pytest.mark.asyncio
    async def test_to_langchain_tool_wraps_time_agent(self) -> None:
        llm = FakeLLM([make_response(content="现在北京时间下午三点")])
        agent = TestTimeAgent(llm=llm)
        tool = agent.to_langchain_tool()
        assert tool.name == "delegate_to_test_time_agent"
        assert "时间" in (tool.description or "")

        result = await tool.ainvoke({"task": "现在北京时间几点"})
        assert "北京时间" in result
