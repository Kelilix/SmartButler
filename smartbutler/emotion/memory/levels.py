"""Memory 4 层分片规则(Phase 6.3 P1)。

设计要点:
1. **同一 Qdrant collection,不同 level tag** —— 不分物理 collection,简单可靠。
2. **level tag 走 Qdrant tags 数组,与其他 tag(``source:xxx``)共存**。
3. **RecallLevel 枚举** —— recall() 默认只查 daily + weekly + permanent(跳 raw,跳 monthly 太粗)。
4. **压缩阈值集中管理** —— consolidation / forgetting / vacuum 都从这读。

为什么不分 collection:
- 多 collection 意味着多套向量索引,recall 跨 collection 要 union,复杂度 ↑,性能 ↓
- 4 层 tag 走 ``payload.tags`` 数组,Qdrant 服务端 ``MatchValue`` 单次过滤,跟现有 tag 过滤同路径
- 跨层 recall 用 ``level:daily OR level:weekly``(走 Qdrant 1.19 的 should/must)即可
"""

from __future__ import annotations

from enum import StrEnum, unique


@unique
class MemoryLevel(StrEnum):
    """4 层分片枚举(写时打 tag,查时过滤)。"""

    RAW = "raw"            # L0 短期,SqliteSaver 存,不进 Qdrant
    DAILY = "daily"        # L1 24h 内的已收尾 fact
    WEEKLY = "weekly"      # L2 7d daily 摘要
    MONTHLY = "monthly"    # L3 30d weekly 摘要
    PERMANENT = "permanent"  # L4 永久


@unique
class RecallLevel(StrEnum):
    """recall() 时的层选择(枚举值 = 逗号分隔的 level tag 列表)。"""

    # 默认:日 + 周 + 永久(跳 raw + monthly 太粗)
    DEFAULT = "daily,weekly,permanent"
    # 全量(含 raw 和 monthly,运维/调试用)
    ALL = "raw,daily,weekly,monthly,permanent"
    # 只永久(身份/偏好)
    PERMANENT_ONLY = "permanent"
    # 日+周(最近的活跃记忆)
    RECENT = "daily,weekly"


# Level 等级(int,用于比较)
LEVEL_RANK: dict[MemoryLevel, int] = {
    MemoryLevel.RAW: 0,
    MemoryLevel.DAILY: 1,
    MemoryLevel.WEEKLY: 2,
    MemoryLevel.MONTHLY: 3,
    MemoryLevel.PERMANENT: 4,
}


# 重要性分级压缩(2026-10-10 决策)
# importance ∈ [0.0, X) → 不进 consolidation,直接走 TTL
# importance ∈ [X, Y) → 进 L1 daily,30 天后被 L2 weekly 吸收(不写 L2 原条)
# importance ∈ [Y, Z) → 进 L1 + L2 + L3(逐级升格)
# importance ∈ [Z, 1.0] → 进 L1 + L2 + L3 + L4 永久
CONSOLIDATION_TTL_THRESHOLD = 0.2       # < 0.2 不进 consolidation
CONSOLIDATION_DAILY_THRESHOLD = 0.5     # 0.2-0.5 → 只进 L1
CONSOLIDATION_PERMANENT_THRESHOLD = 0.8  # ≥ 0.8 → 升 L4 候选

# 压缩时间窗(天)
DAILY_TO_WEEKLY_DAYS = 7                # L1 → L2 触发
WEEKLY_TO_MONTHLY_DAYS = 30            # L2 → L3 触发
MONTHLY_TO_PERMANENT_DAYS = 365         # L3 → L4 触发

# TTL 时间窗(双时间窗,见 §2.5.1.16)
DELETED_RECOVERABLE_DAYS = 7           # deleted_recoverable → archived
ARCHIVED_RETENTION_DAYS = 30           # archived → purged


def level_tag(level: MemoryLevel) -> str:
    """把 MemoryLevel 转成 Qdrant tag 字符串(``level:daily``)。"""
    return f"level:{level.value}"


def parse_recall_level(levels: RecallLevel | str | list[str] | None) -> list[str]:
    """把 RecallLevel / 字符串 / 列表统一解析成 ``level:xxx`` tag 列表。

    Args:
        levels: RecallLevel 枚举 / 逗号分隔字符串 / 单个字符串 / 列表 / None。
            None = RecallLevel.DEFAULT。

    Returns:
        ``["level:daily", "level:weekly", "level:permanent"]`` 形式的列表。
    """
    if levels is None:
        levels = RecallLevel.DEFAULT
    if isinstance(levels, RecallLevel):
        raw = levels.value
    elif isinstance(levels, str):
        raw = levels
    elif isinstance(levels, list):
        raw = ",".join(levels)
    else:
        raise TypeError(
            f"levels 必须是 RecallLevel/str/list,得到 {type(levels).__name__}"
        )
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    # 接受 "daily" 或 "level:daily",统一加前缀
    tags: list[str] = []
    for p in parts:
        if p.startswith("level:"):
            tags.append(p)
        else:
            tags.append(f"level:{p}")
    return tags


def importance_to_levels(importance: float) -> list[MemoryLevel]:
    """给定 importance 0-1,返回应该驻留的 level 列表(写入/升格用)。

    Examples:
        >>> importance_to_levels(0.1)
        []  # 低于 TTL 阈值,直接走 TTL
        >>> importance_to_levels(0.3)
        [<MemoryLevel.DAILY: 'daily'>]
        >>> importance_to_levels(0.6)
        [<MemoryLevel.DAILY: 'daily'>, <MemoryLevel.WEEKLY: 'weekly'>, <MemoryLevel.MONTHLY: 'monthly'>]
        >>> importance_to_levels(0.9)
        [<MemoryLevel.DAILY: 'daily'>, <MemoryLevel.WEEKLY: 'weekly'>, <MemoryLevel.MONTHLY: 'monthly'>, <MemoryLevel.PERMANENT: 'permanent'>]
    """
    if importance < CONSOLIDATION_TTL_THRESHOLD:
        return []
    if importance < CONSOLIDATION_DAILY_THRESHOLD:
        return [MemoryLevel.DAILY]
    if importance < CONSOLIDATION_PERMANENT_THRESHOLD:
        return [MemoryLevel.DAILY, MemoryLevel.WEEKLY, MemoryLevel.MONTHLY]
    return [
        MemoryLevel.DAILY,
        MemoryLevel.WEEKLY,
        MemoryLevel.MONTHLY,
        MemoryLevel.PERMANENT,
    ]


__all__ = [
    "MemoryLevel",
    "RecallLevel",
    "LEVEL_RANK",
    "CONSOLIDATION_TTL_THRESHOLD",
    "CONSOLIDATION_DAILY_THRESHOLD",
    "CONSOLIDATION_PERMANENT_THRESHOLD",
    "DAILY_TO_WEEKLY_DAYS",
    "WEEKLY_TO_MONTHLY_DAYS",
    "MONTHLY_TO_PERMANENT_DAYS",
    "DELETED_RECOVERABLE_DAYS",
    "ARCHIVED_RETENTION_DAYS",
    "level_tag",
    "parse_recall_level",
    "importance_to_levels",
]
