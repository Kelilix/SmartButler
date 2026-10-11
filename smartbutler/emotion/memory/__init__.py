"""记忆系统(按文档 §3.2.4 + README Phase 6.1 / 6.2 P0 / 6.3 P1)。

子模块:
- :mod:`short_term` 短期记忆(langgraph-checkpointer 状态,SqliteSaver 持久化)
- :mod:`long_term`  长期记忆(Qdrant 向量 + langmem 抽象 + 6.3 软删除)
- :mod:`embedder`   Embedder 业务层薄包装(capabilities/embedding re-export)
- :mod:`importance` 重要性评分(启发式,P0 不调 LLM)
- :mod:`retrieval`  MemoryRetriever(remember/recall + 时间衰减重排)
- :mod:`hooks`      MemoryHooks(4 事件 + register/trigger + 异常隔离)
- :mod:`facade`     MemoryFacade(thinking 层唯一入口,高级 API)

Phase 6.1 已交付:长短期存储落地。
Phase 6.2 P0 已交付:Embedder 抽象 + Retrieval + Hooks + Facade + thinking 接入。
Phase 6.3 P1 已交付:4 层分片(levels.py) + 5 类遗忘(forgetting.py) + 5W1H 提炼(summarizer.py) +
  4 层升格管道(consolidation.py) + task 边界检测(task_boundary.py) +
  archived SQLite 备份(archive_store.py) + 3 阶段状态机 vacuum(vacuum.py) +
  MemoryFacade 6 个新 API。
"""

from smartbutler.emotion.memory.archive_store import ArchiveStore, ArchivedMemory
from smartbutler.emotion.memory.consolidation import (
    Consolidator,
    ConsolidationLevel,
    ConsolidationReport,
)
from smartbutler.emotion.memory.facade import MemoryContext, MemoryFacade
from smartbutler.emotion.memory.forgetting import (
    ForgetReason,
    ForgetResult,
    ForgettingService,
)
from smartbutler.emotion.memory.hooks import MemoryEventType, MemoryHooks
from smartbutler.emotion.memory.importance import (
    HeuristicScorer,
    ImportanceScorer,
)
from smartbutler.emotion.memory.levels import (
    ARCHIVED_RETENTION_DAYS,
    CONSOLIDATION_DAILY_THRESHOLD,
    CONSOLIDATION_PERMANENT_THRESHOLD,
    CONSOLIDATION_TTL_THRESHOLD,
    DAILY_TO_WEEKLY_DAYS,
    DELETED_RECOVERABLE_DAYS,
    LEVEL_RANK,
    MemoryLevel,
    MONTHLY_TO_PERMANENT_DAYS,
    RecallLevel,
    WEEKLY_TO_MONTHLY_DAYS,
    importance_to_levels,
    level_tag,
    parse_recall_level,
)
from smartbutler.emotion.memory.long_term import (
    LongTermStore,
    create_long_term_store,
)
from smartbutler.emotion.memory.retrieval import MemoryRetriever, RetrievalHit
from smartbutler.emotion.memory.short_term import ShortTermMemory
from smartbutler.emotion.memory.summarizer import (
    FiveW1H,
    FiveW1HExtractor,
    HeuristicSummarizer,
    LLMScorer,
    LLMSummarizer,
    SummarizedFact,
)
from smartbutler.emotion.memory.task_boundary import (
    BoundaryResult,
    TaskBoundaryDetector,
    TurnInfo,
)
from smartbutler.emotion.memory.vacuum import (
    DeletedMemoryEntry,
    VacuumReport,
    VacuumService,
)

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
    # 6.3 P1 新增
    "MemoryLevel",
    "RecallLevel",
    "LEVEL_RANK",
    "level_tag",
    "parse_recall_level",
    "importance_to_levels",
    "CONSOLIDATION_TTL_THRESHOLD",
    "CONSOLIDATION_DAILY_THRESHOLD",
    "CONSOLIDATION_PERMANENT_THRESHOLD",
    "DAILY_TO_WEEKLY_DAYS",
    "WEEKLY_TO_MONTHLY_DAYS",
    "MONTHLY_TO_PERMANENT_DAYS",
    "DELETED_RECOVERABLE_DAYS",
    "ARCHIVED_RETENTION_DAYS",
    "ForgettingService",
    "ForgetReason",
    "ForgetResult",
    "FiveW1H",
    "FiveW1HExtractor",
    "LLMScorer",
    "LLMSummarizer",
    "HeuristicSummarizer",
    "SummarizedFact",
    "Consolidator",
    "ConsolidationLevel",
    "ConsolidationReport",
    "TaskBoundaryDetector",
    "TurnInfo",
    "BoundaryResult",
    "ArchiveStore",
    "ArchivedMemory",
    "VacuumService",
    "VacuumReport",
    "DeletedMemoryEntry",
]
