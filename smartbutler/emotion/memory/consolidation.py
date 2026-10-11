"""Consolidation —— 4 层分片升格管道(Phase 6.3 P1)。

设计目标:
- **升格 = 写新条目 + 软删除源条目**(永不物理删,见 vacuum.py)
- **4 层分片** L1 daily / L2 weekly / L3 monthly / L4 permanent(同 collection,不同 tag)
- **重要性分级压缩**:
  - [0.0, 0.2) → 不进 consolidation,走 TTL
  - [0.2, 0.5) → 只进 L1 daily
  - [0.5, 0.8) → 进 L1 + L2 weekly + L3 monthly
  - [0.8, 1.0] → 进 L1 + L2 + L3 + L4 permanent
- **LLM 评分降级** —— 调 LLM 失败 → 走启发式重要性评分
- **触发 ON_PROMOTE 钩子** —— 6.3 MemoryHooks ON_PROMOTE 接入
- **降级** —— 任一环节失败 → 记 ERROR 日志,不影响 recall 主流程

不变量:
- consolidate 永不阻塞调用方(虽然现在是同步,后续可改 async 不破签名)
- L4 permanent 必须有 importance ≥ 0.8
- 升格产生的 source_keys 永远不空(记追溯链)
- 5W1H 缺必填字段时该 fact 不进 consolidation(降级为 raw)
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, UTC
from enum import StrEnum, unique
from typing import Any

from smartbutler.capabilities.embedding.base import Embedder, EmbedderError
from smartbutler.emotion.memory.levels import (
    CONSOLIDATION_DAILY_THRESHOLD,
    CONSOLIDATION_PERMANENT_THRESHOLD,
    CONSOLIDATION_TTL_THRESHOLD,
    DAILY_TO_WEEKLY_DAYS,
    MemoryLevel,
    MONTHLY_TO_PERMANENT_DAYS,
    WEEKLY_TO_MONTHLY_DAYS,
    importance_to_levels,
    level_tag,
)
from smartbutler.emotion.memory.long_term import (
    LongTermStore,
    PAYLOAD_DELETED,
    PAYLOAD_LEVEL,
    PAYLOAD_SOURCE_KEYS,
)
from smartbutler.emotion.memory.summarizer import (
    FiveW1H,
    FiveW1HExtractor,
    HeuristicSummarizer,
    LLMScorer,
    LLMSummarizer,
    SummarizedFact,
)
from smartbutler.emotion.memory.hooks import MemoryEventType, MemoryHooks
from smartbutler.utils.logging import get_logger

logger = get_logger(__name__)


@unique
class ConsolidationLevel(StrEnum):
    """触发 consolidation 的目标层。"""

    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"


@dataclass
class ConsolidationReport:
    """单次 consolidation 报告。"""

    level: ConsolidationLevel
    candidates_scanned: int = 0
    promoted: int = 0
    forgotten: int = 0
    skipped_low_importance: int = 0
    skipped_invalid_5w1h: int = 0
    errors: list[str] = field(default_factory=list)
    new_keys: list[str] = field(default_factory=list)
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None


class Consolidator:
    """4 层分片升格管道(Phase 6.3 P1)。

    典型用法::

        c = Consolidator(
            store=long_term,
            embedder=embedder,
            extractor=FiveW1HExtractor(),
            summarizer=HeuristicSummarizer(),
            llm_scorer=LLMScorer(),       # P0 阶段返 None
            llm_summarizer=LLMSummarizer(),  # P0 阶段返 None
            hooks=hooks,
        )
        report = c.consolidate(level="daily")
    """

    def __init__(
        self,
        *,
        store: LongTermStore,
        embedder: Embedder,
        extractor: FiveW1HExtractor,
        summarizer: HeuristicSummarizer,
        llm_scorer: LLMScorer,
        llm_summarizer: LLMSummarizer,
        hooks: MemoryHooks | None = None,
    ) -> None:
        self._store = store
        self._embedder = embedder
        self._extractor = extractor
        self._summarizer = summarizer
        self._llm_scorer = llm_scorer
        self._llm_summarizer = llm_summarizer
        self._hooks = hooks

    async def consolidate(
        self,
        *,
        level: str | ConsolidationLevel = ConsolidationLevel.DAILY,
        limit: int = 10_000,
        now: datetime | None = None,
    ) -> ConsolidationReport:
        """触发一次 consolidation。

        Args:
            level: 目标层("daily"/"weekly"/"monthly")。
                - "daily"  → 扫"无 level" 或 "level:raw" 的 fact,提炼后写 L1
                - "weekly" → 扫 L1 daily,7 天前的,提炼后写 L2,软删 L1
                - "monthly" → 扫 L2 weekly,30 天前的,提炼后写 L3,软删 L2
            limit: 单次扫描上限(Qdrant scroll 默认 10000)。
            now: 当前时间(测试时可注入)。

        Returns:
            :class:`ConsolidationReport`。
        """
        now = now or datetime.now(UTC)
        if isinstance(level, str):
            level = ConsolidationLevel(level)
        report = ConsolidationReport(level=level, candidates_scanned=0)

        # 1. 扫候选
        if level == ConsolidationLevel.DAILY:
            candidates = self._scan_raw_or_unlevel(limit=limit)
            target_levels = None  # 由 importance 决定
        elif level == ConsolidationLevel.WEEKLY:
            candidates = self._scan_level(
                MemoryLevel.DAILY,
                older_than_days=DAILY_TO_WEEKLY_DAYS,
                now=now,
                limit=limit,
            )
            target_levels = [MemoryLevel.WEEKLY]
        else:  # MONTHLY
            candidates = self._scan_level(
                MemoryLevel.WEEKLY,
                older_than_days=WEEKLY_TO_MONTHLY_DAYS,
                now=now,
                limit=limit,
            )
            target_levels = [MemoryLevel.MONTHLY]
        report.candidates_scanned = len(candidates)

        if not candidates:
            report.finished_at = datetime.now(UTC)
            return report

        # 2. 遍历候选 → 评 importance → 选 target levels → 提炼 5W1H → 升格
        for cand in candidates:
            try:
                # 2a. LLM 评分(降级 → 用现有 importance)
                importance = cand["importance"]
                llm_score = None
                try:
                    llm_score_list = await self._llm_scorer.score_batch([cand])
                except Exception as e:  # noqa: BLE001
                    logger.warning("consolidate.llm_score_failed err=%s", e)
                    llm_score_list = None
                if llm_score_list:
                    llm_score = llm_score_list[0].get("importance", importance)
                    importance = float(llm_score)

                # 2b. importance < 0.2 跳过
                if importance < CONSOLIDATION_TTL_THRESHOLD:
                    report.skipped_low_importance += 1
                    continue

                # 2c. 决定 target levels
                if target_levels is None:
                    levels_to_write = importance_to_levels(importance)
                    # daily 触发时,只写 L1(weekly/monthly 由对应 trigger 写)
                    levels_to_write = [
                        lv for lv in levels_to_write if lv == MemoryLevel.DAILY
                    ]
                else:
                    levels_to_write = target_levels

                if not levels_to_write:
                    report.skipped_low_importance += 1
                    continue

                # 2d. 5W1H 提炼 + 摘要
                fw1h = self._extractor.extract(cand["content"])
                if not fw1h.is_valid():
                    report.skipped_invalid_5w1h += 1
                    continue

                # 2e. 升格 = 写新条目 + 软删除源条目
                new_keys = await self._promote(
                    source=cand,
                    fw1h=fw1h,
                    importance=importance,
                    target_levels=levels_to_write,
                )
                for nk in new_keys:
                    report.new_keys.append(nk)
                report.promoted += 1
                report.forgotten += 1  # 源 fact 软删

            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "consolidate.promote_failed key=%s err=%s",
                    cand.get("key", "?"), e,
                )
                report.errors.append(cand.get("key", "?"))

        # 3. 触发 ON_PROMOTE 钩子
        if self._hooks and report.promoted > 0:
            for nk in report.new_keys:
                try:
                    self._hooks.trigger(
                        MemoryEventType.ON_PROMOTE,
                        key=nk,
                        source_keys=report.new_keys,
                    )
                except Exception:  # pragma: no cover - hooks 自身已 try/except
                    pass

        report.finished_at = datetime.now(UTC)
        logger.info(
            "consolidate.%s scanned=%d promoted=%d skipped_low=%d skipped_invalid=%d errors=%d",
            level.value,
            report.candidates_scanned,
            report.promoted,
            report.skipped_low_importance,
            report.skipped_invalid_5w1h,
            len(report.errors),
        )
        return report

    # ------------------- 扫描 -------------------

    def _scan_raw_or_unlevel(self, *, limit: int) -> list[dict[str, Any]]:
        """daily 触发:扫无 level 字段 或 level:raw 的 fact(短期升 L1 的入口)。

        Qdrant 端:用 level 字段 not exists 模拟 —— 退而求其次,客户端过滤。
        """
        all_items = self._store.list_keys_by_level(include_deleted=False, limit=limit)
        out: list[dict[str, Any]] = []
        for it in all_items:
            if it.get("level"):  # 已有 level,跳过
                continue
            # 转化为 promote 用的字典
            pl = self._store.get_payload(it["key"])
            if pl is None or pl.get(PAYLOAD_DELETED):
                continue
            out.append({
                "key": it["key"],
                "content": it.get("content", ""),
                "importance": it.get("importance", 0.5),
                "created_at": it.get("created_at", ""),
            })
        return out

    def _scan_level(
        self,
        level: MemoryLevel,
        *,
        older_than_days: int,
        now: datetime,
        limit: int,
    ) -> list[dict[str, Any]]:
        """weekly/monthly 触发:扫指定 level 且 age > older_than_days 的 fact。"""
        from datetime import timedelta
        all_items = self._store.list_keys_by_level(
            level=level.value, include_deleted=False, limit=limit,
        )
        cutoff = (now - timedelta(days=older_than_days)).isoformat()
        out: list[dict[str, Any]] = []
        for it in all_items:
            if it.get("created_at", "") >= cutoff:
                continue  # 不够老
            pl = self._store.get_payload(it["key"])
            if pl is None or pl.get(PAYLOAD_DELETED):
                continue
            out.append({
                "key": it["key"],
                "content": it.get("content", ""),
                "importance": it.get("importance", 0.5),
                "created_at": it.get("created_at", ""),
            })
        return out

    # ------------------- 升格 -------------------

    async def _promote(
        self,
        *,
        source: dict[str, Any],
        fw1h: FiveW1H,
        importance: float,
        target_levels: list[MemoryLevel],
    ) -> list[str]:
        """升格 = 写新 fact (每 target level 一条) + 软删除源 fact。

        Returns:
            新写入的 keys(每个 level 一个)。
        """
        # 1. 写新 fact(每 target level 一条,level tag 各自不同)
        # 摘要 = 5W1H 渲染
        summary = fw1h.render()
        try:
            embedding = await self._embedder.embed(summary)
        except EmbedderError as e:
            logger.warning("consolidate.embed_failed source=%s err=%s", source["key"], e)
            return []

        new_keys: list[str] = []
        for lv in target_levels:
            new_key = f"promoted:{lv.value}:{uuid.uuid4().hex[:12]}"
            tags = [
                level_tag(lv),
                "source_fact:" + source["key"],
            ]
            self._store.store_memory(
                new_key,
                embedding,
                summary,
                importance=importance,
                tags=tags,
                level=lv.value,
                source_keys=[source["key"]],
            )
            new_keys.append(new_key)

        # 2. 软删除源 fact(调 forgetting.ForgettingService 但这里简化,直接打 tag)
        await self._store.soft_delete(source["key"], reason="promote")

        return new_keys


__all__ = [
    "Consolidator",
    "ConsolidationLevel",
    "ConsolidationReport",
]
