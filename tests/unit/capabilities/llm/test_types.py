"""tests/unit/capabilities/llm/test_types.py — LLM 数据类型契约测试。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from smartbutler.capabilities.llm.types import (
    FinishReason,
    FunctionCall,
    LLMResponse,
    Message,
    Role,
    StreamChunk,
    ToolCall,
    ToolSpec,
    Usage,
)


class TestMessage:
    def test_system_helper(self) -> None:
        m = Message.system("you are helpful")
        assert m.role is Role.SYSTEM
        assert m.content == "you are helpful"
        assert m.tool_calls is None
        assert m.tool_call_id is None

    def test_user_helper(self) -> None:
        m = Message.user("hi")
        assert m.role is Role.USER
        assert m.content == "hi"

    def test_assistant_with_text(self) -> None:
        m = Message.assistant("reply")
        assert m.role is Role.ASSISTANT
        assert m.content == "reply"
        assert m.tool_calls is None

    def test_assistant_with_tool_calls(self) -> None:
        tc = ToolCall(
            id="tc_1",
            type="function",
            function=FunctionCall(name="get_weather", arguments='{"city":"BJ"}'),
        )
        m = Message.assistant(content=None, tool_calls=[tc])
        assert m.tool_calls == [tc]
        assert m.content is None

    def test_tool_result_success(self) -> None:
        m = Message.tool_result(tool_call_id="tc_1", content="sunny")
        assert m.role is Role.TOOL
        assert m.tool_call_id == "tc_1"
        assert m.content == "sunny"

    def test_tool_message_without_tool_call_id_rejected_by_protocol(
        self,
    ) -> None:
        """tool 消息缺 tool_call_id 时,基础模型不报错（约束在协议层）。"""
        m = Message(role=Role.TOOL, content="result")
        assert m.tool_call_id is None
        # 真正的强制约束在 OpenAI 协议转换层（capabilities/llm/openai_compatible.py）。
        # 这里仅校验基础模型接受构造。

    def test_extra_forbid(self) -> None:
        with pytest.raises(ValidationError):
            Message(role=Role.USER, content="hi", extra="x")  # type: ignore[call-arg]


class TestFunctionCall:
    def test_parsed_arguments(self) -> None:
        fc = FunctionCall(name="f", arguments='{"a":1}')
        assert fc.parsed_arguments() == {"a": 1}

    def test_parsed_arguments_invalid_json(self) -> None:
        fc = FunctionCall(name="f", arguments="{not json")
        with pytest.raises(ValueError, match="不是合法 JSON"):
            fc.parsed_arguments()

    def test_parsed_arguments_not_object(self) -> None:
        fc = FunctionCall(name="f", arguments="[1,2,3]")
        with pytest.raises(ValueError, match="必须是 JSON object"):
            fc.parsed_arguments()


class TestToolSpec:
    def test_default_function_type(self) -> None:
        spec = ToolSpec(function={"name": "f"})  # type: ignore[arg-type]
        assert spec.type == "function"
        assert spec.function.name == "f"
        assert spec.function.description == ""
        assert spec.function.parameters == {"type": "object", "properties": {}}


class TestFinishReason:
    def test_enum_values(self) -> None:
        assert FinishReason.STOP.value == "stop"
        assert FinishReason.TOOL_CALLS.value == "tool_calls"
        assert FinishReason.LENGTH.value == "length"
        assert FinishReason.CONTENT_FILTER.value == "content_filter"
        assert FinishReason.ERROR.value == "error"


class TestLLMResponse:
    def test_minimal(self) -> None:
        r = LLMResponse(content="hi")
        assert r.content == "hi"
        assert r.tool_calls is None
        assert r.finish_reason is FinishReason.STOP
        assert r.usage.total_tokens == 0
        assert r.model == ""

    def test_with_tool_calls(self) -> None:
        tc = ToolCall(id="1", type="function", function=FunctionCall(name="f", arguments="{}"))
        r = LLMResponse(tool_calls=[tc], finish_reason=FinishReason.TOOL_CALLS)
        assert r.finish_reason is FinishReason.TOOL_CALLS
        assert r.tool_calls == [tc]
        # content 默认 None,纯工具调用场景下保持 None。
        assert r.content is None


class TestUsage:
    def test_zero_default(self) -> None:
        u = Usage()
        assert u.prompt_tokens == 0
        assert u.completion_tokens == 0
        assert u.total_tokens == 0


class TestStreamChunk:
    def test_default(self) -> None:
        c = StreamChunk()
        assert c.content_delta == ""
        assert c.finish_reason is None
        assert c.tool_calls_delta is None

    def test_with_delta(self) -> None:
        c = StreamChunk(content_delta="你", finish_reason=FinishReason.STOP)
        assert c.content_delta == "你"
        assert c.finish_reason is FinishReason.STOP
