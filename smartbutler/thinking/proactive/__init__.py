"""Proactive 主动循环专属逻辑(Phase 5+,参考 ADR-009)。

子模块:
- decision: ProactiveDecision / ProactiveResult / ProactiveUrgency 模型
- reasoning: ProactiveReasoning(判断该不该主动)
- triggers : LoopType 枚举 + route() 路由函数

注意:ProactiveLoop 本身在 ``thinking/loop/proactive_loop.py``(与 ReactiveLoop 同级),
不在本包内。
"""
from smartbutler.thinking.proactive.decision import (
    ProactiveDecision,
    ProactiveResult,
    ProactiveUrgency,
)
from smartbutler.thinking.proactive.reasoning import (
    ProactiveReasoning,
    ProactiveReasoningFactory,
    RuleBasedProactiveReasoning,
)
from smartbutler.thinking.proactive.triggers import LoopType, route

__all__ = [
    # decision
    "ProactiveDecision",
    "ProactiveResult",
    "ProactiveUrgency",
    # reasoning
    "ProactiveReasoning",
    "ProactiveReasoningFactory",
    "RuleBasedProactiveReasoning",
    # triggers
    "LoopType",
    "route",
]
