"""levels.py 单元测试(Phase 6.3 P1)。"""

from __future__ import annotations

import pytest

from smartbutler.emotion.memory.levels import (
    CONSOLIDATION_DAILY_THRESHOLD,
    CONSOLIDATION_PERMANENT_THRESHOLD,
    CONSOLIDATION_TTL_THRESHOLD,
    DAILY_TO_WEEKLY_DAYS,
    LEVEL_RANK,
    MemoryLevel,
    RecallLevel,
    WEEKLY_TO_MONTHLY_DAYS,
    importance_to_levels,
    level_tag,
    parse_recall_level,
)


def test_level_tag_format() -> None:
    """level_tag 输出 ``level:<value>``。"""
    assert level_tag(MemoryLevel.DAILY) == "level:daily"
    assert level_tag(MemoryLevel.PERMANENT) == "level:permanent"
    assert level_tag(MemoryLevel.RAW) == "level:raw"


def test_parse_recall_level_default() -> None:
    """None → RecallLevel.DEFAULT(daily + weekly + permanent)。"""
    tags = parse_recall_level(None)
    assert "level:daily" in tags
    assert "level:weekly" in tags
    assert "level:permanent" in tags
    assert "level:raw" not in tags
    assert "level:monthly" not in tags


def test_parse_recall_level_all() -> None:
    """RecallLevel.ALL 含 5 层。"""
    tags = parse_recall_level(RecallLevel.ALL)
    assert len(tags) == 5
    assert "level:raw" in tags
    assert "level:monthly" in tags


def test_parse_recall_level_string() -> None:
    """接受逗号分隔字符串。"""
    tags = parse_recall_level("daily,permanent")
    assert "level:daily" in tags
    assert "level:permanent" in tags


def test_parse_recall_level_list_with_prefix() -> None:
    """接受已带 level: 前缀的列表。"""
    tags = parse_recall_level(["level:daily", "level:weekly"])
    assert tags == ["level:daily", "level:weekly"]


def test_parse_recall_level_invalid_type() -> None:
    """int 等非法类型抛 TypeError。"""
    with pytest.raises(TypeError):
        parse_recall_level(123)  # type: ignore[arg-type]


def test_importance_to_levels_below_ttl() -> None:
    """importance < 0.2 → 空列表(走 TTL,不进 consolidation)。"""
    assert importance_to_levels(0.0) == []
    assert importance_to_levels(0.1) == []
    assert importance_to_levels(CONSOLIDATION_TTL_THRESHOLD - 0.01) == []


def test_importance_to_levels_daily_only() -> None:
    """0.2-0.5 → 只进 L1 daily。"""
    levels = importance_to_levels(0.3)
    assert levels == [MemoryLevel.DAILY]
    levels = importance_to_levels(CONSOLIDATION_DAILY_THRESHOLD - 0.01)
    assert levels == [MemoryLevel.DAILY]


def test_importance_to_levels_daily_weekly_monthly() -> None:
    """0.5-0.8 → L1 + L2 + L3。"""
    levels = importance_to_levels(0.6)
    assert levels == [
        MemoryLevel.DAILY, MemoryLevel.WEEKLY, MemoryLevel.MONTHLY,
    ]
    levels = importance_to_levels(CONSOLIDATION_PERMANENT_THRESHOLD - 0.01)
    assert MemoryLevel.PERMANENT not in levels


def test_importance_to_levels_includes_permanent() -> None:
    """≥ 0.8 → L1 + L2 + L3 + L4 permanent。"""
    levels = importance_to_levels(0.9)
    assert MemoryLevel.PERMANENT in levels
    assert levels == [
        MemoryLevel.DAILY, MemoryLevel.WEEKLY, MemoryLevel.MONTHLY,
        MemoryLevel.PERMANENT,
    ]


def test_level_rank_ordering() -> None:
    """LEVEL_RANK 顺序 raw < daily < weekly < monthly < permanent。"""
    assert LEVEL_RANK[MemoryLevel.RAW] < LEVEL_RANK[MemoryLevel.DAILY]
    assert LEVEL_RANK[MemoryLevel.DAILY] < LEVEL_RANK[MemoryLevel.WEEKLY]
    assert LEVEL_RANK[MemoryLevel.WEEKLY] < LEVEL_RANK[MemoryLevel.MONTHLY]
    assert LEVEL_RANK[MemoryLevel.MONTHLY] < LEVEL_RANK[MemoryLevel.PERMANENT]


def test_thresholds_consistent() -> None:
    """阈值常量合规:0.2 < 0.5 < 0.8。"""
    assert CONSOLIDATION_TTL_THRESHOLD < CONSOLIDATION_DAILY_THRESHOLD
    assert CONSOLIDATION_DAILY_THRESHOLD < CONSOLIDATION_PERMANENT_THRESHOLD
    assert 0 < CONSOLIDATION_TTL_THRESHOLD
    assert CONSOLIDATION_PERMANENT_THRESHOLD <= 1.0


def test_compression_windows() -> None:
    """压缩时间窗:7 < 30 < 365。"""
    assert DAILY_TO_WEEKLY_DAYS < WEEKLY_TO_MONTHLY_DAYS
    assert DAILY_TO_WEEKLY_DAYS == 7
    assert WEEKLY_TO_MONTHLY_DAYS == 30
