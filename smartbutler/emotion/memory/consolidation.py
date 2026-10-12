"""Consolidation —— 按时段聚合的升格管道。

设计目标(2026-10-12 重写):
- **聚合而非逐条** —— N 条 fact → **1 条**时段摘要,压缩率 N:1
  - 旧实现是"1 条 fact → 1 条 daily",压缩率为 0,与 consolidation
    降低 Qdrant 负担的目的完全相反(实测:宠物 task 8 轮 fact 2678 字符,
    升格后仍是 2678 字符,一点没压)。
  - 现在:daily 汇总"昨天的所有 fact"、weekly 汇总"本周所有 daily"、
    monthly 汇总"本月所有 weekly",逐层收敛。
- **3 层**(用户决策 2026-10-12):daily / weekly / monthly。
  - permanent 层**不做** —— 月报已足够小且永久保留;
    真正的"永久层"(习惯/画像)留给后续 habit 模块。
- **LLM 真实总结** —— 通过 :class:`LLMSummarizer` 调 LLM 生成摘要,
  失败降级到 :class:`HeuristicSummarizer` 拼接。
- **源 fact 不立即删** —— 升格后源 fact **保留**(用户决策),
  由 forget 策略按时间窗决定何时删除。
  这样"第 4 个月删掉第 1 个月的 fact"能真正生效。
- **降级** —— 任一环节失败 → 记日志,不影响 recall 主流程

不变量:
- consolidate 永不阻塞调用方
- 每次 consolidate 调用最多写 1 条新记忆(每层一次)
- 源 fact 在升格期间**绝不软删**(用户决策:留给 forget 策略)
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, UTC
from enum import StrEnum, unique
from typing import Any

from smartbutler.capabilities.embedding.base import Embedder, EmbedderError
from smartbutler.emotion.memory.levels import (
    CONSOLIDATION_TTL_THRESHOLD,
    DAILY_TO_WEEKLY_DAYS,
    MemoryLevel,
    WEEKLY_TO_MONTHLY_DAYS,
    level_tag,
)
from smartbutler.emotion.memory.long_term import (
    LongTermStore,
    PAYLOAD_CREATED_AT,
    PAYLOAD_DELETED,
    PAYLOAD_IMPORTANCE,
    PAYLOAD_SOURCE_KEYS,
)
from smartbutler.emotion.memory.summarizer import (
    HeuristicSummarizer,
    LLMScorer,
    LLMSummarizer,
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


#: 每周 / 每月近似天数(用于把"本周期"换算成时间窗)
_DAYS_PER_WEEK = 7
_DAYS_PER_MONTH = 30


@dataclass
class ConsolidationReport:
    """单次 consolidation 报告。"""

    level: ConsolidationLevel
    candidates_scanned: int = 0
    #: 🆕 聚合后写入的摘要条数(每次最多 1)
    promoted: int = 0
    #: 本次汇总覆盖的源 fact 数
    source_count: int = 0
    skipped_low_importance: int = 0
    skipped_empty: int = 0
    #: 摘要生成方式:"llm" / "heuristic"
    summary_method: str = ""
    #: 本次覆盖的时间段描述(如 "昨天 2026-10-11")
    period: str = ""
    errors: list[str] = field(default_factory=list)
    new_keys: list[str] = field(default_factory=list)
    source_keys: list[str] = field(default_factory=list)
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None

    @property
    def compression_ratio(self) -> float:
        """压缩比:源 fact 数 / 产出摘要数。1.0 = 没压缩,10.0 = 10 条压成 1 条。"""
        if self.promoted <= 0:
            return 0.0
        return round(self.source_count / self.promoted, 2)


class Consolidator:
    """按时段聚合的升格管道(3 层:daily / weekly / monthly)。

    典型用法::

        c = Consolidator(
            store=long_term,
            embedder=embedder,
            llm_scorer=LLMScorer(llm),        # llm=None 则走启发式
            llm_summarizer=LLMSummarizer(llm),  # llm=None 则走拼接兜底
            hooks=hooks,
        )
        report = await c.consolidate(level="daily")
        # report.promoted == 1(一条日报)
        # report.source_count == N(覆盖 N 条 fact)
    """

    def __init__(
        self,
        *,
        store: LongTermStore,
        embedder: Embedder,
        llm_scorer: LLMScorer | None = None,
        llm_summarizer: LLMSummarizer | None = None,
        hooks: MemoryHooks | None = None,
        summarizer: HeuristicSummarizer | None = None,
        extractor: Any | None = None,
        min_importance: float = CONSOLIDATION_TTL_THRESHOLD,
    ) -> None:
        """Args:
            store: 长期存储(Qdrant)。
            embedder: 向量化(摘要入库前要 embed)。
            llm_scorer: LLM 评分器;None 或内部 llm=None 时走启发式 importance。
            llm_summarizer: LLM 摘要器;None 或内部 llm=None 时走拼接兜底。
            hooks: 记忆钩子(升格完成触发 ON_PROMOTE)。
            summarizer: 启发式摘要兜底(默认新建)。
            extractor: 保留兼容参数,新流程不再用 5W1H(不再逐条提炼)。
            min_importance: 低于此 importance 的 fact 不进汇总。
        """
        self._store = store
        self._embedder = embedder
        self._llm_scorer = llm_scorer or LLMScorer()
        self._llm_summarizer = llm_summarizer or LLMSummarizer()
        self._hooks = hooks
        self._summarizer = summarizer or HeuristicSummarizer()
        self._extractor = extractor  # 兼容保留
        self._min_importance = min_importance

    async def consolidate(
        self,
        *,
        level: str | ConsolidationLevel = ConsolidationLevel.DAILY,
        limit: int = 10_000,
        now: datetime | None = None,
    ) -> ConsolidationReport:
        """把一个时间段的 fact 聚合成 1 条摘要。

        Args:
            level: 目标层。
                - "daily"   → 汇总**昨天** 00:00-24:00 的未分层 fact → 1 条日报
                - "weekly"  → 汇总**最近 7 天**的 daily 摘要 → 1 条周报
                - "monthly" → 汇总**最近 30 天**的 weekly 摘要 → 1 条月报
            limit: 单次扫描上限。
            now: 当前时间(测试时可注入)。

        Returns:
            :class:`ConsolidationReport`,``promoted`` 最大为 1。
        """
        now = now or datetime.now(UTC)
        if isinstance(level, str):
            level = ConsolidationLevel(level)
        report = ConsolidationReport(level=level)

        # 1. 按层级确定"扫哪个 level + 时间窗"
        window = self._resolve_window(level, now)
        report.period = window["label"]

        source_level: str | None = None
        if level == ConsolidationLevel.DAILY:
            # 扫无 level 的 fact(auto_extract 写的原始 fact)
            source_level = None
        elif level == ConsolidationLevel.WEEKLY:
            source_level = MemoryLevel.DAILY.value
        else:
            source_level = MemoryLevel.WEEKLY.value

        candidates = self._scan(
            level=source_level,
            since=window["since"],
            now=now,
            limit=limit,
        )
        report.candidates_scanned = len(candidates)

        if not candidates:
            report.finished_at = datetime.now(UTC)
            logger.info(
                "consolidate.%s no_candidates period=%s",
                level.value, report.period,
            )
            return report

        # 2. importance 过滤(LLM 评分优先,失败降级启发式)
        candidates, skipped_low = await self._filter_by_importance(candidates)
        report.skipped_low_importance = skipped_low

        if not candidates:
            report.finished_at = datetime.now(UTC)
            logger.info(
                "consolidate.%s all_filtered scanned=%d skipped_low=%d",
                level.value, report.candidates_scanned, skipped_low,
            )
            return report

        # 3. LLM 总结成 1 条摘要(失败降级拼接)
        target_level = MemoryLevel(level.value)
        summary, method = await self._summarize(
            candidates,
            level=target_level,
            period=report.period,
        )
        report.summary_method = method

        if not summary:
            report.skipped_empty += 1
            report.finished_at = datetime.now(UTC)
            return report

        # 4. 入库:1 条新记忆,带 level tag + source_keys 溯源
        new_key = await self._write_summary(
            summary=summary,
            target_level=target_level,
            source_keys=[c["key"] for c in candidates],
            importance=self._aggregate_importance(candidates),
            period=report.period,
            source_count=len(candidates),
        )
        if new_key is None:
            report.errors.append("embed_failed")
            report.finished_at = datetime.now(UTC)
            return report

        report.promoted = 1
        report.source_count = len(candidates)
        report.new_keys.append(new_key)
        report.source_keys = [c["key"] for c in candidates]

        # 5. ON_PROMOTE 钩子
        if self._hooks:
            try:
                self._hooks.trigger(
                    MemoryEventType.ON_PROMOTE,
                    key=new_key,
                    source_keys=report.source_keys,
                )
            except Exception:  # pragma: no cover - hooks 自身已 try/except
                pass

        report.finished_at = datetime.now(UTC)
        logger.info(
            "consolidate.%s period=%s scanned=%d -> 1 %s summary "
            "(method=%s importance=%.2f compression=%sx)",
            level.value, report.period, report.source_count,
            level.value, method,
            self._aggregate_importance(candidates),
            report.compression_ratio,
        )
        return report

    # ------------------- 时间窗 -------------------

    def _resolve_window(
        self, level: ConsolidationLevel, now: datetime
    ) -> dict[str, Any]:
        """按层级算出"扫哪个时间窗" + 给人看的时段描述。"""
        if level == ConsolidationLevel.DAILY:
            # 🆕 用户决策:按**自然日**聚合 —— 昨天 00:00-24:00
            today = now.replace(hour=0, minute=0, second=0, microsecond=0)
            start = today - timedelta(days=1)
            end = today
            label = f"昨天 {start.date()} ~ {end.date()}"
        elif level == ConsolidationLevel.WEEKLY:
            # 最近 7 天(滚动)
            end = now
            start = now - timedelta(days=DAILY_TO_WEEKLY_DAYS)
            label = f"最近{_DAYS_PER_WEEK}天 {start.date()} ~ {end.date()}"
        else:
            # 最近 30 天(滚动)
            end = now
            start = now - timedelta(days=WEEKLY_TO_MONTHLY_DAYS)
            label = f"最近{_DAYS_PER_MONTH}天 {start.date()} ~ {end.date()}"

        return {"since": start, "until": end, "label": label}

    # ------------------- 扫描 -------------------

    def _scan(
        self,
        *,
        level: str | None,
        since: datetime,
        now: datetime,
        limit: int,
    ) -> list[dict[str, Any]]:
        """扫时间窗内的 fact(按 level 精确过滤)。

        Args:
            level: 源层级;"daily" 层级传 ``None`` 表示"无 level 字段"
                (即 auto_extract 刚写的原始 fact)。
            since: 窗口起(含)。
            now: 窗口止(不含 future)。
            limit: 扫描上限。
        """
        all_items = self._store.list_keys_by_level(
            level=level, include_deleted=False, limit=limit,
        )
        since_iso = since.isoformat()
        now_iso = now.isoformat()
        out: list[dict[str, Any]] = []
        for it in all_items:
            created = it.get("created_at", "") or it.get(PAYLOAD_CREATED_AT, "")
            if not created:
                continue
            # 窗口过滤:created_at ∈ [since, now)
            if created < since_iso or created >= now_iso:
                continue
            if it.get(PAYLOAD_DELETED):
                continue
            out.append({
                "key": it["key"],
                "content": it.get("content", ""),
                "importance": float(it.get("importance", 0.5)),
                "created_at": created,
            })
        return out

    # ------------------- 过滤 / 汇总 -------------------

    async def _filter_by_importance(
        self, candidates: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], int]:
        """按 importance 过滤(LLM 评分优先,降级用启发式值)。"""
        try:
            scored = await self._llm_scorer.score_batch(
                [{**c, "current_importance": c["importance"]} for c in candidates]
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("consolidate.llm_score_failed err=%s", e)
            scored = None

        if scored:
            # 用 LLM 分覆盖 importance
            for cand, s in zip(candidates, scored):
                if s.get("importance") is not None:
                    cand["importance"] = float(s["importance"])

        kept: list[dict[str, Any]] = []
        skipped = 0
        for c in candidates:
            if c["importance"] < self._min_importance:
                skipped += 1
                continue
            kept.append(c)
        return kept, skipped

    def _aggregate_importance(
        self, candidates: list[dict[str, Any]]
    ) -> float:
        """汇总条目的 importance = 源 fact 的**最大值**。

        为什么取 max 而不是均值:摘要代表这批记忆的整体价值,
        只要有一条高价值就不该被低估。取均值会被大量低价值闲聊拉低,
        导致重要事实所在的那天日报 importance 反而偏低。
        """
        if not candidates:
            return 0.5
        return max(c["importance"] for c in candidates)

    async def _summarize(
        self,
        candidates: list[dict[str, Any]],
        *,
        level: MemoryLevel,
        period: str,
    ) -> tuple[str, str]:
        """生成 1 条时段摘要。返回 (摘要文本, 方法:llm/heuristic)。"""
        summary: str | None = None
        try:
            summary = await self._llm_summarizer.summarize_period(
                candidates, level=level.value, period=period,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("consolidate.summarize_failed err=%s", e)
            summary = None

        if summary and summary.strip():
            return summary.strip(), "llm"

        # 降级:启发式拼接
        fallback = self._summarizer.summarize_text(
            [c["content"] for c in candidates]
        )
        return fallback, "heuristic"

    # ------------------- 写入 -------------------

    async def _write_summary(
        self,
        *,
        summary: str,
        target_level: MemoryLevel,
        source_keys: list[str],
        importance: float,
        period: str,
        source_count: int,
    ) -> str | None:
        """把 1 条摘要写进 Qdrant。失败返回 None。"""
        content = self._render_summary_content(
            summary=summary,
            target_level=target_level,
            period=period,
            source_count=source_count,
        )
        try:
            embedding = await self._embedder.embed(content)
        except EmbedderError as e:
            logger.warning("consolidate.embed_failed err=%s", e)
            return None

        new_key = f"consolidated:{target_level.value}:{uuid.uuid4().hex[:12]}"
        tags = [
            level_tag(target_level),
            f"period:{target_level.value}",
            f"src_count:{source_count}",
        ]
        self._store.store_memory(
            new_key,
            embedding,
            content,
            importance=importance,
            tags=tags,
            level=target_level.value,
            source_keys=source_keys,
        )
        return new_key

    @staticmethod
    def _render_summary_content(
        *,
        summary: str,
        target_level: MemoryLevel,
        period: str,
        source_count: int,
    ) -> str:
        """给摘要加个轻量头部,方便 recall 时 LLM 理解这是哪段时间的总结。"""
        header = {
            MemoryLevel.DAILY.value: "【日报】",
            MemoryLevel.WEEKLY.value: "【周报】",
            MemoryLevel.MONTHLY.value: "【月报】",
        }.get(target_level.value, "【总结】")
        return (
            f"{header}{period}(由 {source_count} 条记忆汇总)\n\n"
            f"{summary}"
        )


__all__ = [
    "Consolidator",
    "ConsolidationLevel",
    "ConsolidationReport",
]