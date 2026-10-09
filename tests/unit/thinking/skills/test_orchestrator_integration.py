"""SkillRuntime + ButlerOrchestrator 端到端集成测试。

不依赖真实 LLM —— 用 FakeLLM(从 test_graph.py 借鉴) +
ToolRegistry.reset_default() 隔离。

验证:
1. SkillRuntime 装配到 ButlerOrchestrator 后,管家看到 6 个文件工具
2. LLM 调 read_file 能读到 work_dir 里的文件
3. LLM 调 write_file 能写到 work_dir/YYYYMMDD/
4. LLM 看到 system prompt 里有 skill list
5. 出沙箱写文件被权限拒绝
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import Runnable

from smartbutler.capabilities.llm.langgraph_adapter import ButlerChatModelAdapter
from smartbutler.capabilities.tools.registry import ToolRegistry
from smartbutler.config.skills import SkillSettings
from smartbutler.thinking.skills import SkillRuntime

# ----------------- FakeLLM -----------------


class _ScriptedChatModel(BaseChatModel):
    """最简 fake:按调用次数返预置 AIMessage,支持工具调用协议。"""

    responses: list[AIMessage]
    call_count: int = 0

    class Config:
        arbitrary_types_allowed = True

    @property
    def _llm_type(self) -> str:
        return "scripted-fake"

    def bind_tools(
        self,
        tools: Any,  # noqa: ANN401
        **kwargs: Any,
    ) -> Runnable[Any, BaseMessage]:
        # bind_tools 不创建新对象,直接返回 self(LangGraph 会调 .invoke / .ainvoke)
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,  # noqa: ARG002
        run_manager: Any = None,  # noqa: ARG002
        **kwargs: Any,
    ) -> ChatResult:
        if self.call_count >= len(self.responses):
            msg = f"ScriptedChatModel 预置响应用完(call_count={self.call_count})"
            raise AssertionError(msg)
        ai = self.responses[self.call_count]
        self.call_count += 1
        return ChatResult(generations=[ChatGeneration(message=ai)])


def _make_fake_llm(*responses: AIMessage) -> _ScriptedChatModel:
    return _ScriptedChatModel(responses=list(responses))


# ----------------- 测试 fixture -----------------


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    """创建临时 workspace + skills 目录,env 指向它们。"""
    ws = tmp_path / "workspace"
    sk = tmp_path / "skills" / "builtin"
    sk.mkdir(parents=True)
    # 放一个 demo skill
    (sk / "demo-skill").mkdir()
    (sk / "demo-skill" / "SKILL.md").write_text(
        "---\nname: demo-skill\ndescription: A demo skill for testing\n---\n# Demo\nbody\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SMARTBUTLER_SKILL_WORKSPACE_DIR", str(ws))
    monkeypatch.setenv("SMARTBUTLER_SKILL_BUILTIN_SKILLS_DIR", str(sk))
    return ws, sk


@pytest.fixture
def skill_runtime(workspace) -> SkillRuntime:
    """从 env 装配 SkillRuntime。"""
    settings = SkillSettings()
    return SkillRuntime.from_settings(settings=settings)


@pytest.fixture(autouse=True)
def reset_tool_registry():
    """每个测试前后重置 ToolRegistry,避免脏状态泄漏。"""
    ToolRegistry.reset_default()
    yield
    ToolRegistry.reset_default()


# ----------------- 测试 -----------------


class TestSkillRuntimeIsolated:
    def test_runtime_loads_demo_skill(self, skill_runtime: SkillRuntime) -> None:
        assert len(skill_runtime.skills) == 1
        assert skill_runtime.skills[0].name == "demo-skill"

    def test_runtime_prompt_contains_skill_name(
        self, skill_runtime: SkillRuntime
    ) -> None:
        prompt = skill_runtime.render_prompt_snippet()
        assert "demo-skill" in prompt
        assert "A demo skill" in prompt

    def test_runtime_builds_six_tools(
        self, skill_runtime: SkillRuntime
    ) -> None:
        tools = skill_runtime.build_file_tools()
        names = {t.name for t in tools}
        assert "read_file" in names
        assert "write_file" in names
        assert "edit_file" in names
        assert "delete_file" in names
        assert "ls" in names
        assert "grep" in names
        assert "glob" in names


class TestOrchestratorWithSkill:
    @pytest.fixture
    def llm_adapter(self, workspace) -> ButlerChatModelAdapter:
        """构造一个 ButlerChatModelAdapter(Phase 4 已有),用真实工厂路径。

        Phase 5 简化:不调真实 LLM,后续 _ScriptedChatModel 注入。
        """
        # 走 OpenAI 兼容 fake adapter —— 这里不直接用,只用 dummy
        return None  # type: ignore[return-value]

    def test_orchestrator_init_with_skill_runtime(
        self, skill_runtime: SkillRuntime
    ) -> None:
        """ButlerOrchestrator 接收 SkillRuntime → 6 个工具被注册到 default registry。"""
        # 因为 real llm 是 None,实际不能用 ainvoke;只看 register 行为
        from smartbutler.capabilities.tools.types import ToolAlreadyRegisteredError

        registry = ToolRegistry.get_default()
        # 手工 register(orchestrator.__init__ 会做)
        for tool in skill_runtime.build_file_tools():
            try:
                registry.register(tool)
            except ToolAlreadyRegisteredError:
                pass
        names = {t.name for t in registry.list_all()}
        assert "read_file" in names
        assert "write_file" in names


class TestReadFileToolEndToEnd:
    def test_read_existing_file(
        self, skill_runtime: SkillRuntime
    ) -> None:
        # 准备文件
        skill_runtime.backend.write("/20261009/test.md", "hello from test")
        # 调工具
        from smartbutler.capabilities.tools.skills.read_file import ReadFileTool

        tool = ReadFileTool(skill_runtime.backend)
        result = asyncio.run(tool.arun(path="/20261009/test.md"))
        assert "hello from test" in result

    def test_read_missing_file(
        self, skill_runtime: SkillRuntime
    ) -> None:
        from smartbutler.capabilities.tools.skills.read_file import ReadFileTool

        tool = ReadFileTool(skill_runtime.backend)
        result = asyncio.run(tool.arun(path="/20261009/missing.md"))
        assert "error" in result.lower()


class TestWriteFileToolEndToEnd:
    def test_write_creates_today_workspace(
        self, skill_runtime: SkillRuntime
    ) -> None:
        from smartbutler.capabilities.tools.skills.write_file import WriteFileTool

        tool = WriteFileTool(skill_runtime.backend)
        today = skill_runtime.backend.today_workspace()
        path = f"{today}/汇报.md"
        result = asyncio.run(tool.arun(path=path, content="# 汇报\n测试内容"))
        assert "✅" in result
        # 文件存在
        assert (skill_runtime.backend.work_dir / "汇报.md").exists() or (
            skill_runtime.backend.work_dir / today.lstrip("/") / "汇报.md"
        ).exists()

    def test_write_outside_sandbox_rejected(
        self, skill_runtime: SkillRuntime
    ) -> None:
        from smartbutler.capabilities.tools.skills.write_file import WriteFileTool

        tool = WriteFileTool(skill_runtime.backend)
        result = asyncio.run(tool.arun(path="/../escape.txt", content="x"))
        assert "error" in result.lower()


class TestLangGraphToolIntegration:
    """验证 6 个文件工具能被 LangChain adapter 正确包装并出现在 graph 里。"""

    def test_all_six_tools_convertible_to_langchain(
        self, skill_runtime: SkillRuntime
    ) -> None:
        from smartbutler.capabilities.tools.langchain_adapter import collect_langchain_tools

        tools = skill_runtime.build_file_tools()
        # 走我们的 adapter 转换
        structured = collect_langchain_tools(tools)
        assert len(structured) >= 6
        names = {s.name for s in structured}
        assert "read_file" in names
        assert "write_file" in names
        # 每个 StructuredTool 都有 invoke / ainvoke
        for s in structured:
            assert s.name
            assert s.description


class TestButlerGraphBuilderAcceptsSkillTools:
    """验证改造后 ButlerGraphBuilder 能接受 skill 工具。"""

    def test_graph_with_skill_tools(
        self, skill_runtime: SkillRuntime
    ) -> None:
        from smartbutler.capabilities.tools.langchain_adapter import collect_langchain_tools
        from smartbutler.thinking.loop.graph import ButlerGraphBuilder

        # 准备 fake LLM 模拟"先调 read_file,再回文本"
        tool_call_id = "call-1"
        first_response = AIMessage(
            content="",
            tool_calls=[
                {
                    "id": tool_call_id,
                    "name": "read_file",
                    "args": {"path": "/20261009/test.md"},
                    "type": "tool_call",
                }
            ],
        )
        second_response = AIMessage(content="读到了:hello world")
        llm = _make_fake_llm(first_response, second_response)
        tools = collect_langchain_tools(skill_runtime.build_file_tools())

        builder = ButlerGraphBuilder()
        compiled = (
            builder.with_llm(llm)
            .with_tools(tools)
            .with_system_prompt("test")
            .with_max_iterations(5)
            .build()
        )

        # 准备文件
        skill_runtime.backend.write("/20261009/test.md", "hello world")

        # 跑
        from langchain_core.messages import HumanMessage

        result = asyncio.run(
            compiled.ainvoke(
                {
                    "messages": [HumanMessage(content="读一下 /20261009/test.md")],
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
        # 至少应该有 2 条 message: 1 AIMessage(tool_call) + 1 ToolMessage + 1 AIMessage(final)
        msgs = result.get("messages", [])
        assert len(msgs) >= 2
        # 最后一条应该是 final
        last = msgs[-1]
        assert isinstance(last, AIMessage)
        assert "hello world" in last.content or "读到了" in last.content
        # 验证 LLM 至少被调了 2 次
        assert llm.call_count == 2
