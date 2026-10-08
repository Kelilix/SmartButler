"""Thinking 层 — 管家 LangGraph 循环包。

Phase 4 任务一览:
- state:        ButlerState
- nodes:        make_decide_node(工厂)
- graph:        ButlerGraphBuilder
- orchestrator: ButlerOrchestrator (面向业务的高层 API)
"""
from smartbutler.thinking.loop.graph import ButlerGraphBuilder
from smartbutler.thinking.loop.nodes.decide import make_decide_node
from smartbutler.thinking.loop.orchestrator import ButlerOrchestrator
from smartbutler.thinking.loop.state import (
    DEFAULT_MAX_ITERATIONS,
    ButlerState,
    ButlerStateDelta,
)

__all__ = [
    "ButlerState",
    "ButlerStateDelta",
    "DEFAULT_MAX_ITERATIONS",
    "make_decide_node",
    "ButlerGraphBuilder",
    "ButlerOrchestrator",
]
