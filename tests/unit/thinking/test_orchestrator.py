"""ButlerOrchestrator 单元测试。

策略:替换 graph_builder 注入,不让真实 LangGraph 跑,只测 orchestrator 自身
逻辑(空输入、参数透传、skill 注入等)。
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import StructuredTool
from pydantic import BaseModel

from smartbutler.capabilities.llm.langgraph_adapter import ButlerChatModelAdapter
from smartbutler.thinking.loop.graph import ButlerGraphBuilder
from smartbutler.thinking.loop.orchestrator import ButlerOrchestrator


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


class _FakeBaseLLM:
    """极简的 BaseLLM duck-type,只为 orchestrator 接受。"""

    def __init__(self) -> None:
        self.closed = False

    async def chat(self, messages: list[Any], **kwargs: Any) -> Any:  # noqa: ARG002
        return None

    async def chat_stream(  # noqa: ARG002
        self, messages: list[Any], **kwargs: Any
    ) -> AsyncIterator[Any]:
        return
        yield  # type: ignore[unreachable]

    async def aclose(self) -> None:
        self.closed = True


class _FakeSettings:
    """orchestrator 单元测试用的 settings duck-type。

    字段命名对齐 ``LLMSettings`` (Phase 4 当前只支持 openai 协议)。
    """

    provider = "openai"
    openai_api_key = "sk-fake"
    openai_base_url = "https://api.openai.com/v1"
    model = "gpt-4o-mini"
    temperature = 0.7
    max_tokens = 1024
    timeout = 60.0
    max_retries = 2


class _FakeCompiledGraph:
    """替换 ButlerGraphBuilder.build() 的产物。"""

    def __init__(self, scripted_responses: list[AIMessage]) -> None:
        self._responses = list(scripted_responses)
        self.invocations: list[dict[str, Any]] = []

    async def ainvoke(self, state: dict[str, Any], config: dict[str, Any] | None = None) -> dict[str, Any]:
        self.invocations.append({"state": state, "config": config})
        # 模拟 decide→END 单次循环:在 user 消息后追加一条 AI 消息
        new_messages = list(state["messages"])
        new_messages.append(self._responses.pop(0))
        return {**state, "messages": new_messages, "iteration_count": 1}


class TestButlerOrchestrator:
    def _make_orchestrator(self) -> tuple[ButlerOrchestrator, _FakeCompiledGraph]:
        compiled = _FakeCompiledGraph([AIMessage(content="管家回复: 现在是 12:00")])
        fake_graph_builder = ButlerGraphBuilder()
        orch = ButlerOrchestrator(
            llm=_FakeBaseLLM(),  # type: ignore[arg-type]
            llm_settings=_FakeSettings(),
            graph_builder=fake_graph_builder,
        )
        # 拦截 _ensure_graph,直接返回 fake
        orch._compiled = compiled  # type: ignore[attr-defined]
        return orch, compiled

    @pytest.mark.asyncio
    async def test_ainvoke_returns_string(self) -> None:
        orch, compiled = self._make_orchestrator()
        result = await orch.ainvoke("现在几点?")
        assert result == "管家回复: 现在是 12:00"
        assert len(compiled.invocations) == 1
        # 验证 state 形状
        state = compiled.invocations[0]["state"]
        assert isinstance(state["messages"][0], HumanMessage)
        assert state["user_id"] == "user"
        assert state["session_id"] == "default"
        assert state["iteration_count"] == 0

    @pytest.mark.asyncio
    async def test_ainvoke_empty_input_returns_empty(self) -> None:
        orch, _ = self._make_orchestrator()
        assert await orch.ainvoke("") == ""
        assert await orch.ainvoke("   ") == ""

    @pytest.mark.asyncio
    async def test_custom_user_session_id(self) -> None:
        orch, compiled = self._make_orchestrator()
        await orch.ainvoke("hi", user_id="alice", session_id="s-42", parent_agent="api")
        state = compiled.invocations[0]["state"]
        assert state["user_id"] == "alice"
        assert state["session_id"] == "s-42"
        assert state["parent_agent"] == "api"
        config = compiled.invocations[0]["config"]
        assert config["configurable"]["thread_id"] == "s-42"

    def test_add_skill_prompt_snippet(self) -> None:
        orch, _ = self._make_orchestrator()
        orch.add_skill_prompt_snippet("# pdf-summary\n1. step one")
        assert orch._skill_prompt_snippets == ["# pdf-summary\n1. step one"]  # type: ignore[attr-defined]

    def test_add_skill_skips_empty(self) -> None:
        orch, _ = self._make_orchestrator()
        orch.add_skill_prompt_snippet("")
        orch.add_skill_prompt_snippet("  ")
        assert orch._skill_prompt_snippets == []  # type: ignore[attr-defined]

    def test_add_skill_invalidates_compiled_graph(self) -> None:
        orch, compiled = self._make_orchestrator()
        assert orch._compiled is compiled  # type: ignore[attr-defined]
        orch.add_skill_prompt_snippet("real skill")
        assert orch._compiled is None  # type: ignore[attr-defined]

    def test_clear_skill_prompt_snippets(self) -> None:
        orch, _ = self._make_orchestrator()
        orch.add_skill_prompt_snippet("a")
        orch.add_skill_prompt_snippet("b")
        orch.clear_skill_prompt_snippets()
        assert orch._skill_prompt_snippets == []  # type: ignore[attr-defined]

    def test_ensure_graph_collects_tools(self) -> None:
        """未注入 compiled 时,_ensure_graph 应当构造并缓存。

        2026-10-09 改:用 mock 隔离 _collect_tools,因为 FakeMessagesListChatModel
        不支持 bind_tools,而 _collect_tools 真实返回的工具数会随注册表变化,
        不应作为本测试断言对象(本身有 test_collect_tools_* 覆盖)。
        """
        from unittest.mock import patch

        orch = ButlerOrchestrator(
            llm=_FakeBaseLLM(),  # type: ignore[arg-type]
            llm_settings=_FakeSettings(),
        )

        with patch.object(orch, "_collect_tools", return_value=[]):
            compiled = orch._ensure_graph()  # type: ignore[attr-defined]

        assert compiled is not None
        # 第二次调用复用
        assert orch._ensure_graph() is compiled  # type: ignore[attr-defined]
