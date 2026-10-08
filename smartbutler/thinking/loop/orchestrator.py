"""ButlerOrchestrator —— 面向业务的高层 API。

职责:
1. 集成 ``ButlerPromptBuilder`` + ``ButlerGraphBuilder`` + ToolRegistry + AgentManager。
2. 暴露简单 ``ainvoke(user_input) -> str`` 接口,业务方不用关心 LangGraph 细节。
3. 支持 skill_prompt_snippets 注入(Phase 5 占位,Phase 4 接 list[str])。
4. 支持 per-call config 覆盖(temperature / max_iterations 等)。

设计原则(参考 TECHNICAL_DESIGN.md §3.2.5):
- **业务层零 LangChain**: 业务方只看到 ``ainvoke`` / ``astream``,
  不 import ``StateGraph`` / ``ToolNode``。
- **可单测**: orchestrator 接受 ``ButlerGraphBuilder`` 注入,
  单测里换成 mock 编译图。
- **可演进**: Phase 5/6 在这里追加 Skill / Personality 注入,不动 Graph。
"""
from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any

import structlog
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from smartbutler.agents.manager.manager import AgentManager
from smartbutler.capabilities.llm.base import BaseLLM
from smartbutler.capabilities.llm.langgraph_adapter import ButlerChatModelAdapter
from smartbutler.capabilities.tools.registry import ToolRegistry
from smartbutler.thinking.loop.graph import ButlerGraphBuilder
from smartbutler.thinking.loop.state import (
    DEFAULT_MAX_ITERATIONS,
    ButlerState,
)
from smartbutler.thinking.prompt.builder import ButlerPromptBuilder

_logger = structlog.get_logger(__name__)


class ButlerOrchestrator:
    """管家编排器:面向业务的高层 API。

    用法::

        llm = create_llm(load_llm_settings())
        orch = ButlerOrchestrator(llm=llm)
        answer = await orch.ainvoke("现在北京时间几点?")
        print(answer)

    默认行为:
    - 从 ``ToolRegistry.get_default()`` 收集 global tool,
      再从 ``AgentManager.get_default()`` 收集 delegate tool。
    - 用 ``ButlerPromptBuilder`` 拼 system prompt。
    - 启动时一次性 build graph(避免每次调用重复编译)。
    """

    def __init__(
        self,
        *,
        llm: BaseLLM,
        tool_registry: ToolRegistry | None = None,
        agent_manager: AgentManager | None = None,
        llm_settings: Any = None,
        graph_builder: ButlerGraphBuilder | None = None,
    ) -> None:
        """构造 orchestrator。

        Args:
            llm: Phase 1 ``BaseLLM`` 实例(必填,双后端兼容)。
            tool_registry: 工具注册表,默认 ``ToolRegistry.get_default()``。
            agent_manager: Sub-Agent 管理器,默认 ``AgentManager.get_default()``。
            llm_settings: 可选,用于构造 ``ButlerChatModelAdapter`` 时读 model/api_key/base_url。
                允许 None,此时会从 ``llm`` 实例反射。
            graph_builder: 可选,注入自定义 builder(测试用)。
        """
        self._llm = llm
        self._tool_registry = tool_registry or ToolRegistry.get_default()
        self._agent_manager = agent_manager or AgentManager.get_default()
        self._llm_settings = llm_settings
        self._graph_builder_factory = graph_builder or ButlerGraphBuilder()

        # 工具 + agent 视图缓存
        self._all_tools: list[Any] = []
        self._skill_prompt_snippets: list[str] = []
        self._compiled: Any = None  # CompiledStateGraph,首次 ainvoke 时 lazy 构建
        self._chat_adapter: ButlerChatModelAdapter | None = None

    # ---------- Skill 注入(Phase 5 占位) ----------

    def add_skill_prompt_snippet(self, snippet: str) -> None:
        """Phase 5 入口:由 SkillRuntime 调用,把 Skill body 追加进 system prompt。

        Phase 4 用法::

            orch.add_skill_prompt_snippet("# pdf-summary\\n...步骤...")
        """
        if not snippet or not snippet.strip():
            return
        self._skill_prompt_snippets.append(snippet.strip())
        # 标记需要重建 graph
        self._compiled = None

    def clear_skill_prompt_snippets(self) -> None:
        self._skill_prompt_snippets.clear()
        self._compiled = None

    # ---------- 工具视图 ----------

    def _collect_tools(self) -> list[Any]:
        """收集所有可用工具:global tool + Sub-Agent delegate tool。

        返回值已是 LangChain ``StructuredTool`` 列表(统一形态),
        不暴露内部 ``BaseTool`` / ``FunctionTool``。
        """
        from smartbutler.capabilities.tools.langchain_adapter import collect_langchain_tools

        # 1. 普通 tool:BaseTool → StructuredTool
        tools: list[Any] = collect_langchain_tools(
            list(self._tool_registry.get_global()),
        )
        # 2. Sub-Agent delegate tool:BaseAgent.to_langchain_tool() 直接返回 StructuredTool
        tools.extend(self._agent_manager.get_delegate_tools())
        return tools

    # ---------- Graph 懒构建 ----------

    def _ensure_graph(self) -> Any:
        """首次 ainvoke / astream 时构造 graph,后续复用。"""
        if self._compiled is not None:
            return self._compiled

        # 1. 构造 LangChain 适配器
        self._chat_adapter = self._make_chat_adapter()
        # 2. 收集工具
        self._all_tools = self._collect_tools()
        # 3. 拼 system prompt
        system_prompt = ButlerPromptBuilder().build(
            tool_specs=self._all_tools,
            skill_prompt_snippets=self._skill_prompt_snippets,
        )
        # 4. build graph
        self._compiled = (
            self._graph_builder_factory.with_llm(self._chat_adapter)
            .with_tools(self._all_tools)
            .with_system_prompt(system_prompt)
            .with_max_iterations(DEFAULT_MAX_ITERATIONS)
            .build()
        )
        return self._compiled

    def _make_chat_adapter(self) -> ButlerChatModelAdapter:
        """从 ``BaseLLM`` + settings 构造 ``ButlerChatModelAdapter``。

        Provider 字段映射(``LLMSettings`` 按 provider 拆开存,这里统一抽到通用字段):
        - openai_*  →  api_key / base_url
        - anthropic_*  →  暂不映射(Phase 4 优先支持 OpenAI 协议)
        - ollama_*  →  暂不映射
        """
        settings = self._llm_settings
        if settings is None:
            # 反射:从 LLM 实例拿 settings
            settings = self._reflect_settings_from_llm()

        # 解析 api_key / base_url:按 provider 选对应字段,缺则报错
        provider = getattr(settings, "provider", "openai")
        if provider == "openai":
            api_key = getattr(settings, "openai_api_key", None)
            base_url = getattr(settings, "openai_base_url", None) or "https://api.openai.com/v1"
        else:
            # Phase 4 暂不支持非 openai 协议
            msg = (
                f"ButlerOrchestrator 当前只支持 openai 协议 provider,"
                f"收到 provider={provider!r}。Phase 4 仅做 Phase 4 范围,"
                f"多 provider 适配留到后续阶段。"
            )
            raise NotImplementedError(msg)

        if not api_key:
            msg = (
                f"未配置 {provider} api_key(LLMSettings.{provider}_api_key)。"
                "请在 .env 里设 SMARTBUTLER_LLM_OPENAI_API_KEY 等。"
            )
            raise ValueError(msg)

        return ButlerChatModelAdapter(
            base_llm=self._llm,
            api_key=api_key,
            base_url=base_url,
            model=getattr(settings, "model", "gpt-4o-mini"),
            temperature=getattr(settings, "temperature", 0.7) or 0.7,
            max_tokens=getattr(settings, "max_tokens", None),
            timeout=getattr(settings, "timeout", 60.0) or 60.0,
            max_retries=getattr(settings, "max_retries", 2) or 2,
        )

    def _reflect_settings_from_llm(self) -> Any:
        """从 ``BaseLLM`` 实例反射出 settings(LangChainLLMAdapter 一定有 _settings 字段)。"""
        for attr in ("_settings", "settings"):
            s = getattr(self._llm, attr, None)
            if s is not None:
                return s
        msg = (
            "ButlerOrchestrator 无法反射 LLM settings。"
            "请显式传 llm_settings=...,或在 BaseLLM 子类上加 _settings 属性。"
        )
        raise RuntimeError(msg)

    # ---------- 高层 API ----------

    async def ainvoke(
        self,
        user_input: str,
        *,
        user_id: str = "user",
        session_id: str = "default",
        parent_agent: str = "user",
    ) -> str:
        """同步阻塞调用,返回最终回复的 content 字符串。"""
        if not user_input or not user_input.strip():
            return ""

        compiled = self._ensure_graph()
        initial_state: ButlerState = {
            "messages": [HumanMessage(content=user_input.strip())],
            "user_id": user_id,
            "session_id": session_id,
            "parent_agent": parent_agent,
            "skill_prompt_snippets": list(self._skill_prompt_snippets),
            "iteration_count": 0,
            "max_iterations": DEFAULT_MAX_ITERATIONS,
        }
        config: dict[str, Any] = {"configurable": {"thread_id": session_id}}
        result = await compiled.ainvoke(initial_state, config=config)
        return _extract_final_content(result)

    async def astream(
        self,
        user_input: str,
        *,
        user_id: str = "user",
        session_id: str = "default",
        parent_agent: str = "user",
    ) -> AsyncIterator[BaseMessage]:
        """流式:每个 token chunk 走 ``BaseMessage``。"""
        if not user_input or not user_input.strip():
            return

        compiled = self._ensure_graph()
        initial_state: ButlerState = {
            "messages": [HumanMessage(content=user_input.strip())],
            "user_id": user_id,
            "session_id": session_id,
            "parent_agent": parent_agent,
            "skill_prompt_snippets": list(self._skill_prompt_snippets),
            "iteration_count": 0,
            "max_iterations": DEFAULT_MAX_ITERATIONS,
        }
        config: dict[str, Any] = {"configurable": {"thread_id": session_id}}
        async for event in compiled.astream(initial_state, config=config, stream_mode="values"):
            # event 是 state 字典
            messages: Sequence[BaseMessage] = event.get("messages", [])
            if messages:
                yield messages[-1]


def _extract_final_content(result: dict[str, Any]) -> str:
    """从 LangGraph 返回的 state 字典里提取最后一条 AIMessage 的 content。"""
    messages: Sequence[BaseMessage] = result.get("messages", [])
    if not messages:
        return ""
    # 反向找最后一条 AIMessage(可能有 ToolMessage 跟在其后)
    for msg in reversed(messages):
        if isinstance(msg, AIMessage):
            return msg.content if isinstance(msg.content, str) else ""
    return ""


__all__ = ["ButlerOrchestrator"]
