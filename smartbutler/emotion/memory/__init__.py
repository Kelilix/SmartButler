"""记忆系统（按文档 §3.2.4 + README Phase 6.1 / 6.2 P0）。

子模块:
- :mod:`short_term` 短期记忆（langgraph-checkpointer 状态,SqliteSaver 持久化）
- :mod:`long_term`  长期记忆（Qdrant 向量 + langmem 抽象,Phase 6.2 接重要性评分）
- :mod:`embedder`   Embedder 业务层薄包装(capabilities/embedding re-export)
- :mod:`importance` 重要性评分(启发式,P0 不调 LLM)
- :mod:`retrieval`  MemoryRetriever(remember/recall + 时间衰减重排)
- :mod:`hooks`      MemoryHooks(4 事件 + register/trigger + 异常隔离)
- :mod:`facade`     MemoryFacade(thinking 层唯一入口,高级 API)

Phase 6.1 已交付:长短期存储落地。
Phase 6.2 P0 已交付:Embedder 抽象 + Retrieval + Hooks + Facade + thinking 接入。
Phase 6.3-6.5 P1 DEFERRED(consolidation / forgetting / summarizer / episodic / habit)。
"""

from smartbutler.emotion.memory.facade import MemoryContext, MemoryFacade
from smartbutler.emotion.memory.hooks import MemoryEventType, MemoryHooks
from smartbutler.emotion.memory.importance import (
    HeuristicScorer,
    ImportanceScorer,
)
from smartbutler.emotion.memory.long_term import (
    LongTermStore,
    create_long_term_store,
)
from smartbutler.emotion.memory.retrieval import MemoryRetriever, RetrievalHit
from smartbutler.emotion.memory.short_term import ShortTermMemory

__all__ = [
    # 6.1 已交付
    "LongTermStore",
    "ShortTermMemory",
    "create_long_term_store",
    # 6.2 P0 新增
    "MemoryFacade",
    "MemoryContext",
    "MemoryHooks",
    "MemoryEventType",
    "MemoryRetriever",
    "RetrievalHit",
    "HeuristicScorer",
    "ImportanceScorer",
]
