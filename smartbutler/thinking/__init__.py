"""Thinking 层 — 管家认知与决策。"""
from smartbutler.thinking.loop import (
    DEFAULT_MAX_ITERATIONS,
    BaseLoop,
    ButlerGraphBuilder,
    ButlerOrchestrator,
    ButlerState,
    ButlerStateDelta,
    ProactiveLoop,
    ReactiveLoopHandle,
    make_decide_node,
)
from smartbutler.thinking.proactive import (
    LoopType,
    ProactiveDecision,
    ProactiveReasoning,
    ProactiveReasoningFactory,
    ProactiveResult,
    ProactiveUrgency,
    RuleBasedProactiveReasoning,
    route,
)
from smartbutler.thinking.prompt.builder import ButlerPromptBuilder

__all__ = [
    # 循环 (Reactive)
    "ButlerState",
    "ButlerStateDelta",
    "DEFAULT_MAX_ITERATIONS",
    "make_decide_node",
    "ButlerGraphBuilder",
    "ButlerOrchestrator",
    # 循环 (Phase 5+ 双循环)
    "BaseLoop",
    "ReactiveLoopHandle",
    "ProactiveLoop",
    # Proactive 模型 / 路由
    "LoopType",
    "ProactiveDecision",
    "ProactiveResult",
    "ProactiveUrgency",
    "ProactiveReasoning",
    "ProactiveReasoningFactory",
    "RuleBasedProactiveReasoning",
    "route",
    # Prompt
    "ButlerPromptBuilder",
]
