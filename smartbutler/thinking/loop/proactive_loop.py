"""ProactiveLoop —— 管家主动循环(Phase 5+,参考 ADR-009)。

## 职责

接收一个归一化后的事件 ``BaseEvent``:

1. **触发判断** —— 调 ``ProactiveReasoning.should_respond(event)`` 决定该不该主动
2. **沉默分支** —— 70%+ 情况返 ``ProactiveResult.silent()``
3. **主动分支** —— 调 ReAct 链生成消息,返 ``ProactiveResult.acted_with(...)``
4. **错误兜底** —— 内部任何异常都降级到 silent(不打扰用户)

## 与 ReactiveLoop 的关系

| 维度 | ReactiveLoop | ProactiveLoop |
|------|--------------|---------------|
| 入口 | ``ainvoke(user_input)`` | ``tick(event)`` |
| 触发判断 | 无(用户问就答)| ``ProactiveReasoning.should_respond`` |
| 沉默支持 | ❌(必须答)| ✅(默认 silent) |
| 工具集 | Reactive 全集 | Reactive 子集(只读,防循环) |
| 输出 | ``str`` | ``ProactiveResult`` |

## Phase 5 简化

- 工具集**默认用 Reactive 的全集**(Phase 7.3 改造:从白名单过滤)
- ReAct 链**直接复用** ButlerGraphBuilder 编译产物(共享底层)
- 消息生成失败 → silent(不打扰)

## 不变量

1. **永不抛异常给上游** —— 内部 try/except 全部转 silent
2. **默认 silent** —— 触发判断失败 / ReAct 失败 / 任何意外 → silent
3. **只读工具** —— Proactive 触发的工具白名单(Phase 7.3 落地,Phase 5 先记 doc)
4. **不进 EventBus** —— ProactiveLoop 不发事件,避免循环触发
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

import structlog
from langchain_core.messages import AIMessage, HumanMessage

from smartbutler.events.core.event import BaseEvent
from smartbutler.thinking.proactive.decision import ProactiveResult
from smartbutler.thinking.proactive.reasoning import (
    ProactiveReasoning,
    RuleBasedProactiveReasoning,
)
from smartbutler.thinking.proactive.triggers import LoopType

_logger = structlog.get_logger(__name__)


class _GraphLike(Protocol):
    """ProactiveLoop 依赖的图最小契约。

    实际是 LangGraph 的 ``CompiledStateGraph``,
    本 Protocol 只是为了在单测里塞 FakeGraph。
    """

    async def ainvoke(
        self,
        input: dict[str, Any],
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...


class ProactiveLoop:
    """管家主动循环。

    用法::

        reasoning = RuleBasedProactiveReasoning()
        loop = ProactiveLoop(reasoning=reasoning, graph=compiled_graph)
        result = await loop.tick(event)
        if result.acted:
            await answer_router.push(result)
        # else: silent,不做任何事
    """

    def __init__(
        self,
        *,
        reasoning: ProactiveReasoning | None = None,
        graph: _GraphLike | None = None,
        user_id: str = "user",
        session_id: str = "default",
        max_iterations: int = 5,
    ) -> None:
        """构造 ProactiveLoop。

        Args:
            reasoning: 触发判断逻辑(默认 ``RuleBasedProactiveReasoning``,
                Phase 6 换 LLM 版本)。
            graph: 已编译的 LangGraph(由 ``ButlerGraphBuilder.build()`` 产出)。
                如果为 None,ProactiveLoop 退化为"只判断不生成"——只返
                ProactiveDecision.silent()(单测 / 早期阶段用)。
            user_id: 默认 user_id(透传到 graph state)。
            session_id: 默认 session_id(每个 event 独立,默认 "default")。
            max_iterations: Proactive 循环上限,默认 5(比 Reactive 的 10 小,
                因为主动建议不应该太长)。
        """
        self._reasoning: ProactiveReasoning = reasoning or RuleBasedProactiveReasoning()
        self._graph: _GraphLike | None = graph
        self._user_id = user_id
        self._session_id = session_id
        self._max_iterations = max_iterations

    # ---------- BaseLoop 协议 ----------

    @property
    def loop_type(self) -> LoopType:
        return LoopType.PROACTIVE

    def get_tools(self) -> list[Any]:
        """Proactive 循环可用的工具列表。

        Phase 5 简化:直返 graph 的工具集(完整)。
        Phase 7.3:从白名单过滤,只保留只读工具。
        """
        if self._graph is None:
            return []
        # CompiledStateGraph 不直接暴露 tools;走 orchestrator 缓存
        # (这里拿不到就走空列表)
        return []

    def get_system_prompt(self) -> str:
        """Proactive 循环的 system prompt(目前直接复用 graph 内部,无暴露)。"""
        return ""

    # ---------- 主动建议通道(Reactive 调用) ----------

    async def advise(
        self,
        *,
        context: str,
        current_answer: str = "",
    ) -> str | None:
        """Reactive → Proactive 内部通道(参考 ADR-009 §5.9.6)。

        Reactive 链尾可调本方法拿"主动建议"——返回 ``str | None``:
        - ``None`` —— 沉默
        - ``str`` —— 主动建议文本(调用方决定是否拼到答案里)

        当前实现:基于文本相似度 + 规则判定是否值得给建议。
        Phase 6 接 LLM 后,这里调 ``ProactiveReasoning.should_respond``
        之外,还要让 LLM 生成建议文本。
        """
        # Phase 5 占位:返回 None(沉默)
        # Phase 6 实现:用 LLM 决定该不该给建议 + 生成建议文本
        return None

    # ---------- 主入口 ----------

    async def tick(self, event: BaseEvent) -> ProactiveResult:
        """单次主动循环入口。

        流程:
        1. 调 ``ProactiveReasoning.should_respond(event)`` 拿决策
        2. silent → 返 ``ProactiveResult.silent()``
        3. 主动 → 调 graph 生成消息 → 返 ``ProactiveResult.acted_with(...)``
        4. 任何异常 → 降级 silent
        """
        try:
            # 1. 触发判断
            decision = await self._reasoning.should_respond(event)
            if not decision.should_respond:
                return ProactiveResult.silent(
                    reason=decision.reason,
                    related_event_id=event.event_id,
                )

            # 2. 主动:调 ReAct 生成消息
            if self._graph is None:
                # 没有 graph 时降级 silent(单测 / 早期阶段)
                _logger.warning(
                    "proactive_loop.no_graph_fallback_silent",
                    event_id=event.event_id,
                )
                return ProactiveResult.silent(
                    reason="no_graph_configured",
                    related_event_id=event.event_id,
                )

            message = await self._generate_message(event=event, decision=decision)
            if not message or not message.strip():
                return ProactiveResult.silent(
                    reason="empty_message_generated",
                    related_event_id=event.event_id,
                )

            # 3. 主动推送
            _logger.info(
                "proactive_loop.acted",
                event_id=event.event_id,
                topic=event.topic,
                urgency=decision.urgency.value,
                message_len=len(message),
            )
            return ProactiveResult.acted_with(
                message=message,
                urgency=decision.urgency,
                related_event_id=event.event_id,
            )

        except Exception as exc:  # noqa: BLE001
            # 任何异常都降级 silent(不打扰用户)
            _logger.error(
                "proactive_loop.error_fallback_silent",
                event_id=event.event_id,
                error=str(exc),
                exc_type=type(exc).__name__,
            )
            return ProactiveResult.silent(
                reason=f"error_fallback: {type(exc).__name__}",
                related_event_id=event.event_id,
            )

    # ---------- 内部 ----------

    async def _generate_message(
        self,
        *,
        event: BaseEvent,
        decision: Any,  # ProactiveDecision
    ) -> str:
        """调 ReAct 链生成主动建议文本。

        Phase 5 实现:把事件 + 决策拼成 user message 喂给 graph,取最后一条 AIMessage。
        Phase 6 实现:可注入 personality / memory context。
        """
        if self._graph is None:  # 防御(实际 tick 已检查)
            return ""

        # 构造输入:user 消息 = 事件描述 + 决策提示
        user_msg_text = self._format_event_input(event=event, decision=decision)
        input_state: dict[str, Any] = {
            "messages": [HumanMessage(content=user_msg_text)],
            "user_id": self._user_id,
            "session_id": self._session_id,
            "parent_agent": f"trigger:{event.source.value}",
            "skill_prompt_snippets": [],
            "iteration_count": 0,
            "max_iterations": self._max_iterations,
        }
        config: dict[str, Any] = {
            "configurable": {"thread_id": f"proactive-{event.event_id}"},
        }

        result = await self._graph.ainvoke(input_state, config=config)
        messages = result.get("messages", [])
        if not messages:
            return ""

        # ⚠️ 只取 **AIMessage** 的 content(不取 HumanMessage / SystemMessage)
        # 否则反向遍历可能拿到 user 自己输入的文本,当 AI 回复返回
        for msg in reversed(messages):
            if not isinstance(msg, AIMessage):
                continue
            content = getattr(msg, "content", None)
            if isinstance(content, str) and content.strip():
                return content.strip()
        return ""

    def _format_event_input(
        self,
        *,
        event: BaseEvent,
        decision: Any,  # ProactiveDecision
    ) -> str:
        """把事件 + 决策转成 ReAct 能消化的 user message 文本。"""
        hints_text = ""
        if getattr(decision, "context_hints", None):
            hints_text = "\n[上下文提示] " + " | ".join(decision.context_hints)

        tone_text = f"\n[语气] {getattr(decision, 'suggested_tone', '温柔提醒')}"

        return (
            f"[系统事件触发主动建议]\n"
            f"事件 topic: {event.topic}\n"
            f"事件 user: {event.user_id}\n"
            f"事件 priority: {event.priority.value}\n"
            f"事件 payload: {event.payload}\n"
            f"事件时间: {event.timestamp.isoformat()}\n"
            f"主动理由: {getattr(decision, 'reason', '')}\n"
            f"{tone_text}{hints_text}\n\n"
            f"请生成一条简短、自然、符合'主动服务'语气的回复。"
        )


# 类型别名:可注入的 ProactiveLoop 工厂(便于单测)
ProactiveLoopFactory = Callable[..., ProactiveLoop]


__all__ = [
    "ProactiveLoop",
    "ProactiveLoopFactory",
]
