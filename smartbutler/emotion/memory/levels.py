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
    """recall() 时的层选择(枚举值 = 逗号分隔的 level tag 列表)。

    🆕 2026-10-12:permanent 层已停用,默认改为"全层级"。
    聚合后每层条目都很少(月报可能只有几条),全查的成本可接受,
    反而能避免"漏掉某层"导致答不出来的��况(实测踩过:
    monthly 被默认跳过,导致月报内容召不回来)。
    """

    # 默认:三层全查(daily + weekly + monthly)
    DEFAULT = "daily,weekly,monthly"
    # 全量(含 raw,运维/调试用)
    ALL = "raw,daily,weekly,monthly"
    # 🆕 已废弃(permanent 层停用),保留供向后兼容
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


# 重要性分级(2026-10-12 重写)
#
# 🆕 聚合模型下,importance **不再决定"进哪几层"** —— 每条 fact 都先进
# 当期日报,压缩交给 LLM 总结决定。importance 现在只做两件事:
#   1. 门控:低于 CONSOLIDATION_TTL_THRESHOLD 的 fact 不进汇总
#   2. 摘要条目的 importance = 源 fact 的最大值(见 Consolidator._aggregate_importance)
#
# 旧的三级分级(0.5 → L1+L2,0.8 → L1+L2+L3+permanent)已废弃:
# 它是为"逐条升格"设计的,在聚合模型下没有意义 —— 一批 fact 无论
# importance 高低都汇进同一条日报。
CONSOLIDATION_TTL_THRESHOLD = 0.2       # < 0.2 不进汇总

# 🆕 3 层压缩时间窗(天)
# daily 汇总"昨天"→ weekly 扫最近 7 天的 daily → monthly 扫最近 30 天的 weekly
DAILY_TO_WEEKLY_DAYS = 7                # L1 → L2 触发
WEEKLY_TO_MONTHLY_DAYS = 30            # L2 → L3 触发
# ⚠️ permanent 层已不做(用户决策 2026-10-12):月报已足够小且永久保留,
# 真正的"永久层"(习惯/画像)留给后续 habit 模块。
# 下面两个常量仅保留供 CLI / 向后兼容 import,新流程不使用。
CONSOLIDATION_DAILY_THRESHOLD = 0.5     # 废弃:聚合模型下不按 importance 分层
CONSOLIDATION_PERMANENT_THRESHOLD = 0.8  # 废弃:同上
MONTHLY_TO_PERMANENT_DAYS = 365         # 废弃:permanent 层未启用

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
    """给定 importance 0-1,返回该 fact 应驻留的 level 列表(写入/升格用)。

    🆕 2026-10-12 重写为**单层门控**。旧实现按 importance 分三级
    (0.5 → L1+L2,0.8 → L1+L2+L3+permanent),那是为"逐条升格"设计的。
    聚合模型下每条 fact 都先进当期日报,后续 weekly/monthly 由
    **摘要条目**收敛,不再对单条 fact 分层。

    Examples:
        >>> importance_to_levels(0.1)
        []  # 低于门控阈值,不进汇总
        >>> importance_to_levels(0.3)
        [<MemoryLevel.DAILY: 'daily'>]
        >>> importance_to_levels(0.9)
        [<MemoryLevel.DAILY: 'daily'>]
    """
    if importance < CONSOLIDATION_TTL_THRESHOLD:
        return []
    return [MemoryLevel.DAILY]


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
