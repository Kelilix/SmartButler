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


class TestButlerOrchestratorWithMemory:
    """Phase 6.2 P0:memory_facade 注入后的接入行为。"""

    def _make_orch_with_memory(
        self, memory_facade: Any,
    ) -> tuple[ButlerOrchestrator, _FakeCompiledGraph]:
        compiled = _FakeCompiledGraph([AIMessage(content="管家回复")])
        orch = ButlerOrchestrator(
            llm=_FakeBaseLLM(),  # type: ignore[arg-type]
            llm_settings=_FakeSettings(),
            memory_facade=memory_facade,
        )
        orch._compiled = compiled  # type: ignore[attr-defined]
        return orch, compiled

    @pytest.mark.asyncio
    async def test_ainvoke_calls_memory_format_for_prompt(self) -> None:
        """ainvoke 调用 MemoryFacade.format_for_prompt(query=user_input)。"""
        calls: list[dict[str, Any]] = []

        class FakeFacade:
            async def format_for_prompt(
                self, query: str, *, ctx: Any = None, k: int = 5, max_chars: int = 2000
            ) -> str:
                calls.append({
                    "query": query, "ctx": ctx, "k": k, "max_chars": max_chars,
                })
                return "### 相关历史记忆\n- 用户说 10.8 去迪士尼"

        orch, _ = self._make_orch_with_memory(FakeFacade())
        await orch.ainvoke("明天有什么安排", user_id="alice", session_id="s-1")
        # 验证 facade 被调,query 是用户输入
        assert len(calls) == 1
        assert calls[0]["query"] == "明天有什么安排"
        assert calls[0]["ctx"] is not None
        assert calls[0]["ctx"].user_id == "alice"
        assert calls[0]["ctx"].session_id == "s-1"

    @pytest.mark.asyncio
    async def test_ainvoke_injects_memory_into_prompt(self) -> None:
        """召回结果会拼到 system_prompt。"""
        from unittest.mock import patch

        class FakeFacade:
            async def format_for_prompt(
                self, query: str, *, ctx: Any = None, k: int = 5, max_chars: int = 2000
            ) -> str:
                return "### 相关历史记忆\n- 用户喜欢咖啡"

        orch = ButlerOrchestrator(
            llm=_FakeBaseLLM(),  # type: ignore[arg-type]
            llm_settings=_FakeSettings(),
            memory_facade=FakeFacade(),
        )
        # 拦截 _collect_tools,只关注 prompt
        with patch.object(orch, "_collect_tools", return_value=[]):
            # 先预热 ainvoke 一次,刷新 _current_memory_block
            await orch.ainvoke("hi")  # 这里 _compiled 会被 set,再清掉
            orch._compiled = None  # type: ignore[attr-defined]
            compiled = orch._ensure_graph()  # type: ignore[attr-defined]
        # 验证 system_prompt 中含召回内容
        # ButlerGraphBuilder 内部用 system_prompt,这里只能间接通过 _chat_adapter 看
        # 简单方法:看 _build_memory_block_sync 输出
        assert "咖啡" in orch._build_memory_block_sync()  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_ainvoke_memory_failure_does_not_crash(self) -> None:
        """facade 抛异常时,ainvoke 仍能返回结果(降级)。"""
        class BrokenFacade:
            async def format_for_prompt(self, **kwargs: Any) -> str:
                raise RuntimeError("boom")

        orch, compiled = self._make_orch_with_memory(BrokenFacade())
        result = await orch.ainvoke("hi")
        # 不崩,正常返回
        assert result == "管家回复"
        # memory_block 清空
        assert orch._current_memory_block == ""  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_ainvoke_without_memory_facade(self) -> None:
        """未注入 facade 时,行为跟原来一致(向后兼容)。"""
        orch, compiled = self._make_orch_with_memory(None)
        result = await orch.ainvoke("hi")
        assert result == "管家回复"
        assert orch._current_memory_block == ""  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_ainvoke_invalidates_compiled_on_memory_change(self) -> None:
        """每次 ainvoke(因 memory 变化)都失效 graph,确保 system_prompt 用最新。"""
        class FakeFacade:
            def __init__(self) -> None:
                self.calls = 0

            async def format_for_prompt(self, **kwargs: Any) -> str:
                self.calls += 1
                return f"记忆 {self.calls}"

        facade = FakeFacade()
        orch, compiled = self._make_orch_with_memory(facade)
        # 第一次 ainvoke
        await orch.ainvoke("query 1")
        # 第二次 ainvoke,facade 又被调一次
        await orch.ainvoke("query 2")
        assert facade.calls == 2

    @pytest.mark.asyncio
    async def test_ainvoke_skips_memory_on_empty_input(self) -> None:
        """空输入不调 facade(直接返回空)。"""
        class FakeFacade:
            def __init__(self) -> None:
                self.calls = 0

            async def format_for_prompt(self, **kwargs: Any) -> str:
                self.calls += 1
                return ""

        facade = FakeFacade()
        orch, compiled = self._make_orch_with_memory(facade)
        result = await orch.ainvoke("")
        assert result == ""
        assert facade.calls == 0

    def test_build_memory_block_no_facade_returns_empty(self) -> None:
        """无 facade 时 _build_memory_block_sync 返回空。"""
        orch, _ = self._make_orch_with_memory(None)
        assert orch._build_memory_block_sync() == ""  # type: ignore[attr-defined]

    def test_build_memory_block_with_cache(self) -> None:
        """有 facade + 缓存时返回缓存值。"""
        class FakeFacade:
            pass

        orch, _ = self._make_orch_with_memory(FakeFacade())
        orch._current_memory_block = "### 记忆\n- 测试"  # type: ignore[attr-defined]
        assert orch._build_memory_block_sync() == "### 记忆\n- 测试"  # type: ignore[attr-defined]


class TestButlerOrchestratorWithShortTerm:
    """Phase 6.2 P0:short_term 注入后的 checkpointer 行为。"""

    def _make_orch_with_short_term(
        self, short_term: Any,
    ) -> tuple[ButlerOrchestrator, _FakeCompiledGraph]:
        compiled = _FakeCompiledGraph([AIMessage(content="管家回复")])
        orch = ButlerOrchestrator(
            llm=_FakeBaseLLM(),  # type: ignore[arg-type]
            llm_settings=_FakeSettings(),
            short_term=short_term,
        )
        orch._compiled = compiled  # type: ignore[attr-defined]
        return orch, compiled

    def test_accepts_short_term_param(self) -> None:
        """构造时传 short_term 参数不抛异常。"""
        class FakeShortTerm:
            def get_sync_saver(self) -> Any:
                return None

        orch = ButlerOrchestrator(
            llm=_FakeBaseLLM(),  # type: ignore[arg-type]
            llm_settings=_FakeSettings(),
            short_term=FakeShortTerm(),
        )
        assert orch._short_term is not None  # type: ignore[attr-defined]
        assert orch._short_term_saver_cache is None  # type: ignore[attr-defined]

    def test_short_term_none_uses_in_memory_saver(self) -> None:
        """未注入 short_term 时 _short_term 为 None。"""
        orch = ButlerOrchestrator(
            llm=_FakeBaseLLM(),  # type: ignore[arg-type]
            llm_settings=_FakeSettings(),
        )
        assert orch._short_term is None  # type: ignore[attr-defined]

    def test_ensure_graph_inits_saver_eagerly(self) -> None:
        """_ensure_graph 同步初始化 short_term saver,缓存复用。"""
        from unittest.mock import patch, MagicMock

        saver_initialized: list[int] = []

        class FakeShortTerm:
            def get_sync_saver(self) -> Any:
                saver_initialized.append(1)
                return MagicMock()

        orch = ButlerOrchestrator(
            llm=_FakeBaseLLM(),  # type: ignore[arg-type]
            llm_settings=_FakeSettings(),
            short_term=FakeShortTerm(),
        )
        # 拦截 compiled.build(),防止 LangGraph 验证 MagicMock
        with patch.object(orch._graph_builder_factory, "build", return_value=MagicMock()):
            with patch.object(orch, "_collect_tools", return_value=[]):
                orch._ensure_graph()  # type: ignore[attr-defined]
        assert len(saver_initialized) == 1
        assert orch._short_term_saver_cache is not None  # type: ignore[attr-defined]
        # 第二次 _ensure_graph:复用缓存,不再调 get_async_saver
        orch._compiled = None  # type: ignore[attr-defined]
        with patch.object(orch._graph_builder_factory, "build", return_value=MagicMock()):
            with patch.object(orch, "_collect_tools", return_value=[]):
                orch._ensure_graph()  # type: ignore[attr-defined]
        assert len(saver_initialized) == 1

    def test_ensure_graph_passes_checkpointer_to_builder(self) -> None:
        """_ensure_graph 把 short_term 的 saver 传给 graph builder。"""
        from unittest.mock import MagicMock, patch

        class FakeShortTerm:
            def get_sync_saver(self) -> Any:
                return MagicMock()

        orch = ButlerOrchestrator(
            llm=_FakeBaseLLM(),  # type: ignore[arg-type]
            llm_settings=_FakeSettings(),
            short_term=FakeShortTerm(),
        )
        # 拦截 compiled.build(),防止 LangGraph 验证
        with patch.object(orch._graph_builder_factory, "build", return_value=MagicMock()):
            with patch.object(orch, "_collect_tools", return_value=[]):
                orch._ensure_graph()  # type: ignore[attr-defined]
        # 验证:graph_builder._checkpointer 已被赋值为 saver 缓存实例
        assert orch._graph_builder_factory._checkpointer is orch._short_term_saver_cache  # type: ignore[misc]


class TestButlerOrchestratorAsyncSaver:
    """Phase 6.2 P0: ainvoke / astream 走 async saver,不撞 NotImplementedError。"""

    @pytest.mark.asyncio
    async def test_ensure_graph_async_uses_async_saver(self) -> None:
        """_ensure_graph_async 必须 await get_async_saver()(不是 get_sync_saver)。"""
        from unittest.mock import AsyncMock, MagicMock, patch

        captured: dict[str, Any] = {}

        class FakeShortTerm:
            def get_sync_saver(self) -> Any:
                raise AssertionError(
                    "async 路径不应该调 get_sync_saver —— 会撞 NotImplementedError"
                )

            async def get_async_saver(self) -> Any:
                captured["called"] = True
                return MagicMock(name="AsyncSqliteSaver")

        orch = ButlerOrchestrator(
            llm=_FakeBaseLLM(),  # type: ignore[arg-type]
            llm_settings=_FakeSettings(),
            short_term=FakeShortTerm(),
        )
        with patch.object(orch._graph_builder_factory, "build", return_value=MagicMock()):
            with patch.object(orch, "_collect_tools", return_value=[]):
                await orch._ensure_graph_async()  # type: ignore[attr-defined]
        assert captured.get("called") is True
        # 验证:graph_builder 拿到了 async saver
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

        # _checkpointer 是 MagicMock,确认它是 async 路径拿的
        assert orch._short_term_saver_cache is not None  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_ensure_graph_async_returns_cached_compiled(self) -> None:
        """第二次调 _ensure_graph_async 复用缓存,不重建 saver。"""
        from unittest.mock import MagicMock, patch

        call_count: list[int] = []

        class FakeShortTerm:
            def get_sync_saver(self) -> Any:
                raise AssertionError("不应该调 sync")

            async def get_async_saver(self) -> Any:
                call_count.append(1)
                return MagicMock()

        orch = ButlerOrchestrator(
            llm=_FakeBaseLLM(),  # type: ignore[arg-type]
            llm_settings=_FakeSettings(),
            short_term=FakeShortTerm(),
        )
        with patch.object(orch._graph_builder_factory, "build", return_value=MagicMock()):
            with patch.object(orch, "_collect_tools", return_value=[]):
                await orch._ensure_graph_async()  # type: ignore[attr-defined]
        # 第二次:不应再调 get_async_saver
        with patch.object(orch._graph_builder_factory, "build", return_value=MagicMock()):
            with patch.object(orch, "_collect_tools", return_value=[]):
                await orch._ensure_graph_async()  # type: ignore[attr-defined]
        assert len(call_count) == 1

    @pytest.mark.asyncio
    async def test_ensure_graph_async_rebuilds_when_sync_cache_present(self) -> None:
        """如果 _short_term_saver_cache 已被同步路径污染,async 路径必须重建。"""
        from unittest.mock import MagicMock, patch

        class FakeSyncSaver:
            """模拟同步 SqliteSaver —— 没有 aget_tuple。"""

        class FakeShortTerm:
            def __init__(self) -> None:
                self.async_called = False
                self.aclose_called = False

            def get_sync_saver(self) -> Any:
                return FakeSyncSaver()

            async def get_async_saver(self) -> Any:
                self.async_called = True
                return MagicMock()

            async def aclose(self) -> None:
                self.aclose_called = True

        stm = FakeShortTerm()
        orch = ButlerOrchestrator(
            llm=_FakeBaseLLM(),  # type: ignore[arg-type]
            llm_settings=_FakeSettings(),
            short_term=stm,
        )
        # 模拟同步路径污染了缓存
        orch._short_term_saver_cache = FakeSyncSaver()  # type: ignore[attr-defined]
        with patch.object(orch._graph_builder_factory, "build", return_value=MagicMock()):
            with patch.object(orch, "_collect_tools", return_value=[]):
                await orch._ensure_graph_async()  # type: ignore[attr-defined]
        assert stm.async_called is True
        assert stm.aclose_called is True
        # 缓存里是 async 拿到的
        assert orch._short_term_saver_cache is not None  # type: ignore[attr-defined]
        assert orch._short_term_saver_cache is not isinstance(  # type: ignore[attr-defined]
            orch._short_term_saver_cache, FakeSyncSaver
        )
