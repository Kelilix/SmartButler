"""Phase 6.6: ButlerOrchestrator 接入 Personality 的单元测试。

策略(跟 test_orchestrator.py 里的 memory 测试一致):
- 替换 compiled graph,不让真实 LangGraph 跑
- 只测 orchestrator 接入 personality 后的行为:
  * 接受 personality 参数
  * 渲染 personality block 拼到 system_prompt
  * reload_personality 触发 graph lazy rebuild
  * None 时走老行为
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from langchain_core.messages import AIMessage
from langchain_core.tools import StructuredTool
from pydantic import BaseModel

from smartbutler.emotion.personality.traits import (
    TRAIT_HUMOR,
    Personality,
)
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
    """orchestrator 单元测试用的 duck-type LLM。"""

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
    provider = "openai"
    openai_api_key = "sk-fake"
    openai_base_url = "https://api.openai.com/v1"
    model = "gpt-4o-mini"
    temperature = 0.7
    max_tokens = 1024
    timeout = 60.0
    max_retries = 2


class _FakeCompiledGraph:
    def __init__(self) -> None:
        self.invocations: list[dict[str, Any]] = []

    async def ainvoke(
        self, state: dict[str, Any], config: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        self.invocations.append({"state": state, "config": config})
        return {
            "messages": state["messages"] + [AIMessage(content="ok")],
        }


def _make_orch(
    *,
    personality: Personality | None = None,
    tool_name: str = "dummy_tool",
) -> ButlerOrchestrator:
    """构造 orchestrator(不触发 LangGraph 真实编译,只测 _build_graph_builder)。

    重要:ToolRegistry.get_default() 是单例,跨测试会污染。
    这里给 orchestrator 注入一个**新建**的空 ToolRegistry,避免污染别的测试。
    """
    from smartbutler.capabilities.tools.registry import ToolRegistry

    orch = ButlerOrchestrator(
        llm=_FakeBaseLLM(),  # type: ignore[arg-type]
        llm_settings=_FakeSettings(),
        personality=personality,
        tool_registry=ToolRegistry(),  # 🆕 独立 registry,不污染 default
    )
    # 不主动注册 tool —— 大部分测试不依赖 tool,_collect_tools 会自然返空
    return orch


class TestPersonalityInjection:
    def test_accepts_personality_param(self) -> None:
        p = Personality(name="X")
        orch = _make_orch(personality=p)
        assert orch._personality is p  # type: ignore[attr-defined]

    def test_default_personality_is_none(self) -> None:
        orch = _make_orch(personality=None)
        assert orch._personality is None  # type: ignore[attr-defined]

    def test_personality_block_none_returns_empty(self) -> None:
        """无 personality → 返空字符串,prompt builder 走默认人设。"""
        orch = _make_orch(personality=None)
        block = orch._build_personality_block_sync()  # type: ignore[attr-defined]
        assert block == ""

    def test_personality_block_with_default_personality(self) -> None:
        """有 personality → 渲染后包含名字 + trait 字段。"""
        orch = _make_orch(personality=Personality(name="我的管家"))
        block = orch._build_personality_block_sync()  # type: ignore[attr-defined]
        assert "我的管家" in block
        assert "formality" in block
        assert "humor" in block

    def test_personality_block_cached(self) -> None:
        """第二次调用走缓存,不再重新渲染。"""
        orch = _make_orch(personality=Personality(name="缓存测试"))
        block1 = orch._build_personality_block_sync()  # type: ignore[attr-defined]
        block2 = orch._build_personality_block_sync()  # type: ignore[attr-defined]
        assert block1 == block2
        # 验证缓存确实生效(引用相等)
        assert orch._current_personality_block == block1  # type: ignore[attr-defined]

    def test_personality_block_render_failure_returns_empty(self) -> None:
        """渲染失败 → 返空,不崩。"""
        # 构造一个让 render_persona_block 内部炸的对象
        # (用 model_construct 绕过 Pydantic Field 约束)
        broken = Personality.model_construct(
            name="X",
            version=1,
            traits={"humor": object()},  # type: ignore[arg-type]
            guidelines=[],
            updated_at=None,
            updated_by="default",
        )
        orch = _make_orch(personality=broken)
        block = orch._build_personality_block_sync()  # type: ignore[attr-defined]
        # 失败 → 降级返空字符串(降级链闭合)
        assert block == ""


class TestReloadPersonality:
    def test_reload_clears_cache_and_compiled(self) -> None:
        orch = _make_orch(personality=Personality(name="v1"))
        # 触发一次渲染,让缓存非空
        orch._build_personality_block_sync()  # type: ignore[attr-defined]
        # 模拟一个 compiled graph 缓存
        orch._compiled = _FakeCompiledGraph()  # type: ignore[attr-defined]

        new_p = Personality(name="v2")
        new_p.adjust_trait(TRAIT_HUMOR, new_value=0.9, source="user_explicit")
        orch.reload_personality(new_p)

        # 1. 内部 _personality 已更新
        assert orch._personality is new_p  # type: ignore[attr-defined]
        # 2. _current_personality_block 已清空(下次重渲染)
        assert orch._current_personality_block == ""  # type: ignore[attr-defined]
        # 3. _compiled 已清空(graph lazy rebuild)
        assert orch._compiled is None  # type: ignore[attr-defined]
        # 4. 再次渲染会拿到新名字
        block2 = orch._build_personality_block_sync()  # type: ignore[attr-defined]
        assert "v2" in block2
        assert "v1" not in block2

    def test_reload_without_compiled_still_clears_cache(self) -> None:
        orch = _make_orch(personality=Personality(name="v1"))
        orch._build_personality_block_sync()  # type: ignore[attr-defined]
        # 没有 compiled 也照常工作
        new_p = Personality(name="v2")
        orch.reload_personality(new_p)
        assert orch._current_personality_block == ""  # type: ignore[attr-defined]


class TestPersonalityPromptBuilderIntegration:
    def test_personality_block_passes_through_to_prompt(self) -> None:
        """Personality 渲染结果透传到 ButlerPromptBuilder,覆盖默认 _BUTLER_PERSONA。

        不真跑 LangGraph,只调 ``_build_personality_block_sync`` 拿渲染结果,
        再喂给 ButlerPromptBuilder 验证拼接顺序/优先级。
        """
        from smartbutler.thinking.prompt.builder import ButlerPromptBuilder

        orch = _make_orch(personality=Personality(name="注入测试"))
        personality_block = orch._build_personality_block_sync()  # type: ignore[attr-defined]
        # 模拟 _build_graph_builder 内部用 ButlerPromptBuilder().build(...) 拼 system_prompt
        system_prompt = ButlerPromptBuilder().build(
            tool_specs=[],
            memory_block="",
            personality_block=personality_block,
        )
        # Personality block 应出现
        assert "注入测试" in system_prompt
        # 默认内置 _BUTLER_PERSONA 不应出现(被覆盖)
        assert "SmartButler 管家" not in system_prompt

    def test_no_personality_uses_builtin_persona(self) -> None:
        """无 personality → system_prompt 用内置 _BUTLER_PERSONA。"""
        from smartbutler.thinking.prompt.builder import ButlerPromptBuilder

        orch = _make_orch(personality=None)
        personality_block = orch._build_personality_block_sync()  # type: ignore[attr-defined]
        system_prompt = ButlerPromptBuilder().build(
            tool_specs=[],
            memory_block="",
            personality_block=personality_block,
        )
        # personality_block 为空 → ButlerPromptBuilder 走默认
        assert "SmartButler 管家" in system_prompt

    def test_personality_and_memory_coexist(self) -> None:
        """Personality 和 memory 段都注入,互不干扰。"""
        from smartbutler.thinking.prompt.builder import ButlerPromptBuilder

        orch = _make_orch(personality=Personality(name="双注入"))
        personality_block = orch._build_personality_block_sync()  # type: ignore[attr-defined]
        memory_block = "## 相关历史\n- 用户喜欢猫"
        system_prompt = ButlerPromptBuilder().build(
            tool_specs=[],
            memory_block=memory_block,
            personality_block=personality_block,
        )
        assert "双注入" in system_prompt
        assert "用户喜欢猫" in system_prompt
