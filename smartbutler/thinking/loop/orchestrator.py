"""ButlerOrchestrator —— 面向业务的高层 API。

职责:
1. 集成 ``ButlerPromptBuilder`` + ``ButlerGraphBuilder`` + ToolRegistry + AgentManager。
2. 暴露简单 ``ainvoke(user_input) -> str`` 接口,业务方不用关心 LangGraph 细节。
3. 支持 skill_prompt_snippets 注入(Phase 5 占位,Phase 4 接 list[str])。
4. 支持 per-call config 覆盖(temperature / max_iterations 等)。
5. **Phase 5+**:暴露 ``proactive_tick(event) -> ProactiveResult`` 主动循环入口
   (参考 ADR-009)。

设计原则(参考 TECHNICAL_DESIGN.md §3.2.5):
- **业务层零 LangChain**: 业务方只看到 ``ainvoke`` / ``astream`` / ``proactive_tick``,
  不 import ``StateGraph`` / ``ToolNode``。
- **可单测**: orchestrator 接受 ``ButlerGraphBuilder`` 注入,
  单测里换成 mock 编译图。
- **可演进**: Phase 5/6 在这里追加 Skill / Personality 注入,不动 Graph。
- **Proactive 不污染 Reactive** (ADR-009 §5.9.7):
  ``ainvoke()`` 签名零修改(新增可选参数 ``enable_advice``),
  ``proactive_tick()`` 是新方法,两条入口独立。
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
from smartbutler.events.core.event import BaseEvent
from smartbutler.thinking.loop.graph import ButlerGraphBuilder
from smartbutler.thinking.loop.proactive_loop import ProactiveLoop
from smartbutler.thinking.loop.state import (
    DEFAULT_MAX_ITERATIONS,
    ButlerState,
)
from smartbutler.thinking.proactive import ProactiveReasoning, ProactiveResult
from smartbutler.thinking.prompt.builder import ButlerPromptBuilder
from smartbutler.thinking.skills.runtime import SkillRuntime

_logger = structlog.get_logger(__name__)

# Reactive → Proactive 内部调用的规则触发模式(参考 ADR-009 §5.9.6)
# Phase 5 简化:字符串前缀匹配,后续可换 LLM 判定
_PROACTIVE_TRIGGER_PREFIXES: tuple[str, ...] = (
    "该吃",
    "该做",
    "该喝",
    "吃什么",
    "做什么",
    "喝什么",
    "怎么办",
    "我该",
)


def _should_request_proactive(user_msg: str) -> bool:
    """Reactive 链尾是否应该追加 Proactive 建议(参考 ADR-009 §5.9.6)。

    规则(不调 LLM,避免开销):
    - 用户消息以"该吃/该做/吃什么/怎么办"等开头 → 触发
    - 其他 → 不触发

    Phase 6 emotion 上线后,这里可升级为 Persona 驱动判断。
    """
    if not user_msg or not user_msg.strip():
        return False
    msg = user_msg.strip()
    return any(msg.startswith(prefix) for prefix in _PROACTIVE_TRIGGER_PREFIXES)


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
        proactive_reasoning: ProactiveReasoning | None = None,
        enable_proactive_advice: bool = False,
        skill_runtime: SkillRuntime | None = None,
        memory_facade: Any | None = None,
        short_term: Any | None = None,
    ) -> None:
        """构造 orchestrator。

        Args:
            llm: Phase 1 ``BaseLLM`` 实例(必填,双后端兼容)。
            tool_registry: 工具注册表,默认 ``ToolRegistry.get_default()``。
            agent_manager: Sub-Agent 管理器,默认 ``AgentManager.get_default()``。
            llm_settings: 可选,用于构造 ``ButlerChatModelAdapter`` 时读 model/api_key/base_url。
                允许 None,此时会从 ``llm`` 实例反射。
            graph_builder: 可选,注入自定义 builder(测试用)。
            proactive_reasoning: 🆕 Phase 5+,主动循环的触发判断逻辑。
                None → ProactiveLoop 内部默认 ``RuleBasedProactiveReasoning``。
            enable_proactive_advice: 🆕 Phase 5+,是否在 ``ainvoke`` 链尾追加
                Proactive 主动建议。**默认 False**——保持现有 e2e 测试零修改。
                调成 True 后,Reactive 链尾会按规则触发 Proactive 建议追加。
            skill_runtime: 🆕 Phase 5,Skill 子系统运行时句柄。
                注入后,管家自动获得 6 个文件工具(read_file/write_file/edit_file/
                delete_file/ls/grep/glob)+ skill list 注入 system prompt。
                None → 不注入 skill 能力(老行为,零影响)。
            memory_facade: 🆕 Phase 6.2 P0,``MemoryFacade`` 实例(thinking 接入层)。
                注入后,管家在 ``decide_node`` 召回相关历史并注入 system prompt。
                None → 不注入 memory 能力(老行为,零影响)。
            short_term: 🆕 Phase 6.2 P0,``ShortTermMemory`` 实例。
                注入后,LangGraph checkpointer 改用 SqliteSaver,重启不丢短期上下文。
                None → 用默认 InMemorySaver(重启即丢,Phase 6.2 P0 前行为)。
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

        # Phase 5+: Proactive 主动循环
        self._proactive_reasoning = proactive_reasoning
        self._proactive_loop: ProactiveLoop | None = None  # 懒构建
        self._enable_proactive_advice = enable_proactive_advice

        # Phase 5+: Skill 子系统
        self._skill_runtime = skill_runtime
        if skill_runtime is not None:
            # 注册 6 个文件工具到 default registry(幂等)
            from smartbutler.capabilities.tools.types import ToolAlreadyRegisteredError

            for tool in skill_runtime.build_file_tools():
                try:
                    self._tool_registry.register(tool)
                except ToolAlreadyRegisteredError:
                    # 单测 / 重入场景:跳过
                    pass

        # Phase 6.2 P0: Memory 接入层
        self._memory_facade = memory_facade
        self._current_memory_block: str = ""  # 每次 ainvoke 时刷新

        # Phase 6.2 P0: Short-term memory checkpointer(SqliteSaver)
        self._short_term = short_term
        self._short_term_saver_cache: Any | None = None  # 缓存已初始化的 saver 实例

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
        """收集所有可用工具:管家可见 tool + Sub-Agent delegate tool。

        返回值已是 LangChain ``StructuredTool`` 列表(统一形态),
        不暴露内部 ``BaseTool`` / ``FunctionTool``。

        范围说明(2026-10-09 改):
        之前用 get_global() 只拿 scope=GLOBAL/COMMON 的 tool,
        会漏掉 BUTLER scope 的 tool(如 reload_skills)。
        改成 get_butler() → 覆盖 GLOBAL + COMMON + BUTLER + SKILL,
        排除 AGENT(子 agent 私有,管家通过 delegate_to_* 调用)。
        """
        from smartbutler.capabilities.tools.langchain_adapter import collect_langchain_tools

        # 1. 管家可见的普通 tool:BaseTool → StructuredTool
        tools: list[Any] = collect_langchain_tools(
            list(self._tool_registry.get_butler()),
        )
        # 2. Sub-Agent delegate tool:BaseAgent.to_langchain_tool() 直接返回 StructuredTool
        tools.extend(self._agent_manager.get_delegate_tools())
        return tools

    # ---------- Phase 6.2 P0 : Memory 注入 ----------

    def _build_memory_block_sync(self) -> str:
        """读缓存的 ``_current_memory_block``。

        缓存由 ``ainvoke`` 在调 LLM 前用 ``format_for_prompt`` 刷新。
        任何失败/无 facade → 返回空字符串(thinking 继续工作)。
        """
        if self._memory_facade is None:
            return ""
        return self._current_memory_block or ""

    # ---------- Graph 懒构建 ----------

    def _ensure_graph(self) -> Any:
        """首次 ainvoke / astream 时构造 graph,后续复用(同步路径)。

        仅在无 event loop 的场景下使用(如同步单测),或着 short_term 为 None 的场景。
        真实 ainvoke / astream 走 :meth:`_ensure_graph_async`。
        """
        if self._compiled is not None:
            return self._compiled
        compiled = self._build_graph_builder(saver=self._get_sync_saver())
        self._compiled = compiled.build()
        return self._compiled

    async def _ensure_graph_async(self) -> Any:
        """异步版 lazy 图构建(Phase 6.2 P0)。

        与 :meth:`_ensure_graph` 的区别:
        - 短记忆用 ``get_async_saver()`` 拿到 ``AsyncSqliteSaver``(LangGraph async 路径唯一
          可用的 saver,同步 ``SqliteSaver.aget_tuple`` 会抛 ``NotImplementedError``)。
        - 缓存到 ``self._short_term_saver_cache``,避免每次 ``ainvoke`` 都重建 saver。
        """
        if self._compiled is not None:
            # 已有 compiled —— 但要确认它绑定的是 async saver(避免 sync 路径污染)。
            cache = self._short_term_saver_cache
            if cache is None or hasattr(cache, "aget_tuple"):
                return self._compiled
            # 否则:缓存的 compiled 绑了 sync saver,需要重建
        saver = await self._get_async_saver()
        compiled = self._build_graph_builder(saver=saver)
        self._compiled = compiled.build()
        return self._compiled

    def _get_sync_saver(self) -> Any | None:
        """同步 saver 获取器。short_term 为 None 时返回 None,走 InMemorySaver。"""
        if self._short_term is None:
            return None
        if self._short_term_saver_cache is None:
            self._short_term_saver_cache = self._short_term.get_sync_saver()
        return self._short_term_saver_cache

    async def _get_async_saver(self) -> Any | None:
        """异步 saver 获取器(Phase 6.2 P0)。

        防御性:如果缓存里是同步 ``SqliteSaver``(同步路径留下的脏数据),
        重新走 ``get_async_saver()`` 拿异步版本,否则 ainvoke 会撞
        ``NotImplementedError``。
        """
        if self._short_term is None:
            return None
        cache = self._short_term_saver_cache
        # AsyncSqliteSaver 有 aget_tuple / aput;同步 SqliteSaver 没有。
        if cache is not None and hasattr(cache, "aget_tuple"):
            return cache
        if cache is not None and not hasattr(cache, "aget_tuple"):
            # 同步缓存(可能是同步路径留下的,或是 InMemorySaver 之外的 sync 类)
            # 重置,走 async 路径
            self._short_term_saver_cache = None
            await self._short_term.aclose()  # 释放旧 held(若有)
        self._short_term_saver_cache = await self._short_term.get_async_saver()
        return self._short_term_saver_cache

    def _build_graph_builder(self, *, saver: Any | None = None) -> Any:
        """构造 graph builder,统一 LLM 装配 + 工具收集 + prompt 拼装。

        Args:
            saver: 已实例化的 checkpointer(同步或异步),None → 走 InMemorySaver。
                同步路径传 ``self._get_sync_saver()`` 的结果,异步路径传
                ``await self._get_async_saver()`` 的结果。
        """
        # 1. 构造 LangChain 适配器
        self._chat_adapter = self._make_chat_adapter()
        # 2. 收集工具
        self._all_tools = self._collect_tools()
        # 3. 拼 system prompt(含 skill list 注入 + memory 注入)
        skill_snippets = list(self._skill_prompt_snippets)
        if self._skill_runtime is not None:
            skill_list_prompt = self._skill_runtime.render_prompt_snippet()
            if skill_list_prompt:
                skill_snippets.append(skill_list_prompt)
        # 3a. 🆕 Phase 6.2 P0: 召回相关历史并格式化为 system prompt 片段
        memory_block = self._build_memory_block_sync()
        system_prompt = ButlerPromptBuilder().build(
            tool_specs=self._all_tools,
            skill_prompt_snippets=skill_snippets,
            memory_block=memory_block,
        )
        compiled = (
            self._graph_builder_factory.with_llm(self._chat_adapter)
            .with_tools(self._all_tools)
            .with_system_prompt(system_prompt)
            .with_max_iterations(DEFAULT_MAX_ITERATIONS)
        )
        if saver is not None:
            compiled = compiled.with_checkpointer(saver)
        return compiled

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
        enable_advice: bool | None = None,
    ) -> str:
        """同步阻塞调用,返回最终回复的 content 字符串。

        Args:
            user_input: 用户消息文本。
            user_id: 用户 ID(透传到 state)。
            session_id: 会话 ID(checkpointer 用)。
            parent_agent: 父调用方标识(默认 ``"user"``)。
            enable_advice: 🆕 本次调用是否允许追加 Proactive 建议。
                ``None`` → 用构造时的 ``enable_proactive_advice`` 默认值;
                ``True/False`` → 显式覆盖。

        Returns:
            最终回复文本(可能包含 Proactive 追加的建议)。
        """
        if not user_input or not user_input.strip():
            return ""

        # 🆕 Phase 6.2 P0: 刷新 memory_block 缓存
        if self._memory_facade is not None:
            try:
                from smartbutler.emotion.memory.facade import MemoryContext

                ctx = MemoryContext(user_id=user_id, session_id=session_id)
                self._current_memory_block = await self._memory_facade.format_for_prompt(
                    query=user_input.strip(), ctx=ctx, k=5, max_chars=2000,
                )
                # 缓存失效:让 _ensure_graph_async 重新拼 system_prompt
                self._compiled = None
            except Exception:  # noqa: BLE001
                self._current_memory_block = ""
                # 缓存不变,无需重建

        compiled = await self._ensure_graph_async()
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
        base_answer = _extract_final_content(result)

        # 链尾挂载点:Proactive 主动建议(默认 disabled,零影响)
        effective_enable = (
            enable_advice if enable_advice is not None else self._enable_proactive_advice
        )
        if effective_enable and _should_request_proactive(user_input):
            advice = await self._request_proactive_advice(
                user_input=user_input,
                base_answer=base_answer,
                user_id=user_id,
            )
            if advice:
                return f"{base_answer}\n\n💡 {advice}"

        return base_answer

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

        # 🆕 Phase 6.2 P0: 同 ainvoke
        if self._memory_facade is not None:
            try:
                from smartbutler.emotion.memory.facade import MemoryContext

                ctx = MemoryContext(user_id=user_id, session_id=session_id)
                self._current_memory_block = await self._memory_facade.format_for_prompt(
                    query=user_input.strip(), ctx=ctx, k=5, max_chars=2000,
                )
                self._compiled = None
            except Exception:  # noqa: BLE001
                self._current_memory_block = ""

        compiled = await self._ensure_graph_async()
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

    # ---------- Phase 5+ : Proactive 主动循环入口 ----------

    async def proactive_tick(
        self,
        event: BaseEvent,
        *,
        user_id: str | None = None,
    ) -> ProactiveResult:
        """Proactive 主动循环入口(参考 ADR-009 §5.9.4)。

        接收一个归一化后的事件,产出 ``ProactiveResult``:
        - ``acted=False`` —— 沉默
        - ``acted=True`` —— 主动推送,message 字段填推送内容

        Args:
            event: 归一化后的事件(来自 EventNormalizer)。
            user_id: 覆盖默认 user_id(默认从 event.user_id 拿)。

        Returns:
            ProactiveResult(不可变,带 acted / message / urgency / silence_reason)。
        """
        effective_user_id = user_id or event.user_id
        loop = await self._get_or_build_proactive_loop(
            user_id=effective_user_id,
            session_id=f"proactive-{event.event_id}",
        )
        return await loop.tick(event)

    async def _get_or_build_proactive_loop(
        self,
        *,
        user_id: str,
        session_id: str,
    ) -> ProactiveLoop:
        """懒构建 ProactiveLoop(首次调用时构造,后续复用)。"""
        if self._proactive_loop is None:
            # ProactiveLoop 需要 graph;优先用 _ensure_graph_async 出来的 compiled
            await self._ensure_graph_async()
            graph = self._compiled
            self._proactive_loop = ProactiveLoop(
                reasoning=self._proactive_reasoning,
                graph=graph,
                user_id=user_id,
                session_id=session_id,
            )
        return self._proactive_loop

    async def _request_proactive_advice(
        self,
        *,
        user_input: str,
        base_answer: str,
        user_id: str,
    ) -> str | None:
        """Reactive → Proactive 内部通道(参考 ADR-009 §5.9.6)。

        规则触发时调 ProactiveLoop 拿"主动建议"文本。
        Phase 5 占位:构造一个虚拟的 BaseEvent 给 ProactiveLoop。
        Phase 6 emotion 上线后,可基于 user_input / base_answer 拼更丰富的 context。
        """
        from datetime import UTC, datetime

        from smartbutler.events.core.event import EventPriority, EventSource

        # 构造一个虚拟的 BaseEvent 走 ProactiveLoop 触发判断
        # 注意:user/interface 不在 EventBus 里,这里是 Reactive 内部调用,
        # 所以走 ProactiveLoop 拿"主动建议"——这个 event 是合成事件。
        # Phase 5:用 DEVICE 兜底(ProactiveLoop 关心的是 payload,不是 source)。
        synthetic_event = BaseEvent(
            event_id=f"advice-{datetime.now(UTC).strftime('%Y%m%d_%H%M%S_%f')}",
            source=EventSource.DEVICE,
            topic="internal.reactive.advice_request",
            user_id=user_id,
            timestamp=datetime.now(UTC),
            priority=EventPriority.NORMAL,
            payload={"user_input": user_input, "base_answer": base_answer},
        )
        loop = await self._get_or_build_proactive_loop(
            user_id=user_id,
            session_id=f"advice-{synthetic_event.event_id}",
        )
        result = await loop.tick(synthetic_event)
        if result.acted and result.message:
            return result.message
        return None


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


__all__ = [
    "ButlerOrchestrator",
    "_should_request_proactive",
    "_PROACTIVE_TRIGGER_PREFIXES",
]
