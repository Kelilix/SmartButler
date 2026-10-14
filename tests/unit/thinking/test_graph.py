"""ButlerGraphBuilder 单元测试。

策略:写一个最小可用的 ``BaseChatModel`` 子类 fake,
支持 ``bind_tools`` + 按调用次数返回预置 AIMessage。
"""
from __future__ import annotations

from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import Runnable
from langchain_core.tools import StructuredTool
from pydantic import BaseModel

from smartbutler.thinking.loop.graph import ButlerGraphBuilder


class _ScriptedChatModel(BaseChatModel):
    """最简 fake chat model:支持 bind_tools + 按调用次数返预置 AIMessage。

    适用:Phase 4 单元测试 — 不依赖真实 LLM,完整跑通 LangGraph 循环。
    """

    responses: list[AIMessage]
    call_count: int = 0

    class Config:
        arbitrary_types_allowed = True

    @property
    def _llm_type(self) -> str:
        return "scripted-fake"

    def bind_tools(  # type: ignore[override]
        self,
        tools: Any,  # noqa: ANN401
        **kwargs: Any,
    ) -> Runnable[Any, BaseMessage]:
        """返回一个新的 fake,保留 responses,不真正绑定工具(因为我们的测试
        不验证 tool_calls 的 LLM 侧处理,只验证 graph 编排)。
        """
        return _ScriptedChatModel(responses=list(self.responses))

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,  # noqa: ARG002
        run_manager: Any = None,  # noqa: ARG002
        **kwargs: Any,
    ) -> ChatResult:
        if self.call_count >= len(self.responses):
            msg = f"_ScriptedChatModel: 预置响应已用完(已用 {self.call_count})"
            raise AssertionError(msg)
        ai = self.responses[self.call_count]
        self.call_count += 1
        return ChatResult(generations=[ChatGeneration(message=ai)])


class _NoArgs(BaseModel):
    pass


def _dummy_tool(name: str = "dummy_tool", desc: str = "测试用") -> StructuredTool:
    async def _call() -> str:
        return "ok"

    return StructuredTool.from_function(
        coroutine=_call,
        name=name,
        description=desc,
        args_schema=_NoArgs,
    )


class TestButlerGraphBuilder:
    def test_must_set_llm(self) -> None:
        builder = ButlerGraphBuilder().with_system_prompt("test")
        with __import__("pytest").raises(ValueError, match="with_llm"):
            builder.build()

    def test_must_set_system_prompt(self) -> None:
        builder = ButlerGraphBuilder().with_llm(_ScriptedChatModel(responses=[]))
        with __import__("pytest").raises(ValueError, match="with_system_prompt"):
            builder.build()

    def test_build_with_no_tools(self) -> None:
        """无工具时也能 build,decide 直接 END。"""
        builder = (
            ButlerGraphBuilder()
            .with_llm(_ScriptedChatModel(responses=[AIMessage(content="直接回答")]))
            .with_system_prompt("你是管家")
        )
        compiled = builder.build()
        assert compiled is not None

    def test_build_with_tools(self) -> None:
        compiled = (
            ButlerGraphBuilder()
            .with_llm(_ScriptedChatModel(responses=[AIMessage(content="done")]))
            .with_tools([_dummy_tool()])
            .with_system_prompt("你是管家")
            .build()
        )
        assert compiled is not None

    def test_max_iterations_validation(self) -> None:
        builder = ButlerGraphBuilder()
        with __import__("pytest").raises(ValueError, match="max_iterations"):
            builder.with_max_iterations(0)
        with __import__("pytest").raises(ValueError, match="max_iterations"):
            builder.with_max_iterations(-1)


# -- 集成测试:用真实 LangGraph 跑一次完整循环 --


class TestGraphRuns:
    def test_direct_response_no_tools(self) -> None:
        """无 tool,LLM 直接回答 → 一次 decide 后 END。"""
        from langchain_core.messages import HumanMessage

        compiled = (
            ButlerGraphBuilder()
            .with_llm(_ScriptedChatModel(responses=[AIMessage(content="今天晴天")]))
            .with_system_prompt("你是管家")
            .build()
        )
        import asyncio

        result = asyncio.run(
            compiled.ainvoke(
                {
                    "messages": [HumanMessage(content="今天天气?")],
                    "user_id": "u",
                    "session_id": "s",
                    "parent_agent": "user",
                    "skill_prompt_snippets": [],
                    "iteration_count": 0,
                    "max_iterations": 5,
                },
                config={"configurable": {"thread_id": "t1"}},
            )
        )
        msgs = result["messages"]
        # 最后一条是 AIMessage("今天晴天")
        assert msgs[-1].content == "今天晴天"
        assert result["iteration_count"] == 1

    def test_tool_call_then_final(self) -> None:
        """第 1 轮调 tool,第 2 轮 final。"""
        from langchain_core.messages import HumanMessage, ToolMessage

        # 第 1 轮:调 dummy_tool
        # 第 2 轮:直接回答
        scripted = _ScriptedChatModel(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[{"id": "call_1", "name": "dummy_tool", "args": {}, "type": "tool_call"}],
                ),
                AIMessage(content="调用完毕,答案是 42"),
            ]
        )
        compiled = (
            ButlerGraphBuilder()
            .with_llm(scripted)
            .with_tools([_dummy_tool()])
            .with_system_prompt("你是管家")
            .build()
        )
        import asyncio

        result = asyncio.run(
            compiled.ainvoke(
                {
                    "messages": [HumanMessage(content="调一下")],
                    "user_id": "u",
                    "session_id": "s",
                    "parent_agent": "user",
                    "skill_prompt_snippets": [],
                    "iteration_count": 0,
                    "max_iterations": 5,
                },
                config={"configurable": {"thread_id": "t1"}},
            )
        )
        msgs = result["messages"]
        # 期望顺序:Human → AI(tool_calls) → Tool → AI("调用完毕...")
        assert len(msgs) >= 4
        assert msgs[-1].content == "调用完毕,答案是 42"
        # 中间应有 ToolMessage
        tool_msgs = [m for m in msgs if isinstance(m, ToolMessage)]
        assert len(tool_msgs) == 1
        assert result["iteration_count"] == 2
