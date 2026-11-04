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

# 🆕 时区:Consolidation 的"自然日"以 Asia/Shanghai 为准。
# 所有窗口边界都在本地 00:00,再转 UTC 后传给 _scan 做字符串比较
# (Qdrant.created_at 也是 UTC,字符串可比较)。
# 测试可注入 _LOCAL_TZ=UTC 验证时区无关逻辑。
import os
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum, unique
from typing import Any
from zoneinfo import ZoneInfo

_LOCAL_TZ_NAME = os.environ.get("SMARTBUTLER_MEMORY_TZ", "Asia/Shanghai")
LOCAL_TZ = ZoneInfo(_LOCAL_TZ_NAME)

from smartbutler.capabilities.embedding.base import Embedder, EmbedderError
from smartbutler.emotion.memory.hooks import MemoryEventType, MemoryHooks
from smartbutler.emotion.memory.levels import (
    CONSOLIDATION_TTL_THRESHOLD,
    DAILY_TO_WEEKLY_DAYS,
    UNKNOWN_USER,
    WEEKLY_TO_MONTHLY_DAYS,
    MemoryLevel,
    level_tag,
    parse_participants_tag,
    parse_task_started_at,
    parse_task_tags,
    parse_user_tag,
    participants_tag,
    task_tag,
    user_tag,
)
from smartbutler.emotion.memory.long_term import (
    PAYLOAD_CREATED_AT,
    PAYLOAD_DELETED,
    LongTermStore,
)
from smartbutler.emotion.memory.summarizer import (
    HeuristicSummarizer,
    LLMScorer,
    LLMSummarizer,
)
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


# ---------------------------------------------------------------------------
# 🆕 2026-11-02: 归属保持 —— 分组数据结构
# ---------------------------------------------------------------------------
#
# 设计要点(**归档层级 ≠ 段落层级**):
#
# 类比:员工一天分做 2 个项目,但向领导汇报的是**一条日报**,
# 分别讲述这两个项目的进展和问题。
#
# 所以:
# - **归档层级**(日报/周报/月报)= 时间粒度 → 决定"产出几条"
# - **段落层级**(项目/task)      = 工作单元粒度 → 决定"每条里面怎么分段"
#
# 分桶键 = **主体(user)**,不是 task。
# 若按 task 分桶,一天有 3 个 task 就出 3 条日报 —— 拆得太散。
#
# 一个 task 被多人参与时,归给该 task 内**消息数最多的说话人**
# (主说话人),participants tag 记录全部参与人。
# 这样"两人聊一件事"不会被切成两条各说一半的日报。


@dataclass
class SubjectGroup:
    """一个主体(说话人)在一个时间窗内的记忆分组。

    这是升格的分桶单位 —— **一条日报 = 一个 SubjectGroup**。
    """

    #: 主归属 user_id(说话人)。归给谁,这条日报就是谁的。
    primary_user: str
    #: 组内涉及的 task_id 列表(日报内部的段落锚点)
    task_ids: list[str] = field(default_factory=list)
    #: 组内涉及的全部 user_id(含 primary_user)—— 参与者
    participants: list[str] = field(default_factory=list)
    #: 组内的 fact(每个 fact 是 {key, content, importance, tags, ...})
    facts: list[dict[str, Any]] = field(default_factory=list)
    #: 组内 fact 按 task 归组后的段落:[(task_id, [fact, ...]), ...]
    #: task_id 为 "" 表示无 task 归属的 fact(老数据)
    segments: list[tuple[str, list[dict[str, Any]]]] = field(default_factory=list)

    @property
    def fact_count(self) -> int:
        """组内 fact 数。"""
        return len(self.facts)


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
    #: 🆕 2026-11-02: 本次按主体分出的组数(= 产出的摘要条数)
    groups: int = 0
    #: 🆕 2026-11-02: 分桶依据(固定 "subject" —— 按说话人分桶)
    grouped_by: str = "subject"
    #: 🆕 2026-11-02: 各组的主归属(便于排查"归属有没有丢")
    group_users: list[str] = field(default_factory=list)

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
                - "daily"   → 汇总**本地昨日** 00:00-24:00 的未分层 fact → 1 条日报
                - "weekly"  → 汇总**本地最近 7 天**的 daily 摘要 → 1 条周报
                - "monthly" → 汇总**本地最近 30 天**的 weekly 摘要 → 1 条月报
            limit: 单次扫描上限。
            now: 当前时间(测试时可注入)。无 tz 时按 UTC 处理。

        Returns:
            :class:`ConsolidationReport`,``promoted`` 最大为 1。
        """
        if now is None:
            now = datetime.now(UTC)
        elif now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        if isinstance(level, str):
            level = ConsolidationLevel(level)
        report = ConsolidationReport(level=level)

        # 1. 按层级确定"扫哪个 level + 时间窗"
        #    🆕 2026-10-12:窗口用本地时区(默认 Asia/Shanghai)
        #    - daily 锁死到 [本地昨日 00:00, 本地今日 00:00)
        #      → 凌晨 3 点跑不会把"今天 0-3 点"的 fact 当成昨天的
        #    - weekly/monthly 用本地 0 点对齐,保持语义一致
        window = self._resolve_window(level, now)
        report.period = window["label"]

        # 🆕 窗口右边界统一用 window["until"](本地日 0 点),
        # 而不是 now——保证"昨天 0-24"完整,且跟标签一致。
        # CLI dry_run 走自己的 _scan,真跑用 consolidate() 这条路径。
        scan_now = window["until"]
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
            now=scan_now,
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

        # 3. 🆕 2026-11-02: 按主体分组,每组产出一条摘要
        #
        # 归档层级 = 时间粒度(这条日报覆盖哪段时间)
        # 段落层级 = task(日报内部按项目分段)
        # **分桶键是 user,不是 task** —— 否则一天 3 个 task 就出 3 条日报,太散。
        target_level = MemoryLevel(level.value)
        groups = self._group_by_subject(candidates)
        report.groups = len(groups)
        report.group_users = [g.primary_user for g in groups]

        if not groups:
            report.finished_at = datetime.now(UTC)
            return report

        all_new_keys: list[str] = []
        all_source_keys: list[str] = []
        methods: list[str] = []

        for group in groups:
            summary, method = await self._summarize(
                group,
                level=target_level,
                period=report.period,
            )
            methods.append(method)

            if not summary:
                report.skipped_empty += 1
                continue

            new_key = await self._write_summary(
                summary=summary,
                target_level=target_level,
                source_keys=[c["key"] for c in group.facts],
                importance=self._aggregate_importance(group.facts),
                period=report.period,
                source_count=len(group.facts),
                group=group,
            )
            if new_key is None:
                report.errors.append("embed_failed")
                continue

            report.promoted += 1
            all_new_keys.append(new_key)
            all_source_keys.extend(c["key"] for c in group.facts)

            # ON_PROMOTE 钩子(每组一次)
            if self._hooks:
                try:
                    self._hooks.trigger(
                        MemoryEventType.ON_PROMOTE,
                        key=new_key,
                        source_keys=[c["key"] for c in group.facts],
                    )
                except Exception:  # pragma: no cover - hooks 自身已 try/except
                    pass

        report.summary_method = methods[0] if methods else ""
        report.new_keys.extend(all_new_keys)
        report.source_keys.extend(all_source_keys)
        report.source_count = len(all_source_keys)

        report.finished_at = datetime.now(UTC)
        logger.info(
            "consolidate.%s period=%s scanned=%d groups=%d -> %d %s summaries "
            "(method=%s importance=%.2f compression=%sx)",
            level.value, report.period, report.candidates_scanned, report.groups,
            report.promoted, level.value,
            report.summary_method,
            self._aggregate_importance(candidates),
            report.compression_ratio,
        )
        return report

    # ------------------- 时间窗 -------------------

    def _resolve_window(
        self, level: ConsolidationLevel, now: datetime
    ) -> dict[str, Any]:
        """按层级算出"扫哪个时间窗" + 给人看的时段描述。

        🆕 2026-10-12:窗口以**本地时区(默认 Asia/Shanghai)**的自然日为准,
        daily 锁死到 [本地昨日 00:00, 本地今日 00:00)。

        返回的 ``since``/``until`` 是带 tz 的 datetime,转 iso 后与 Qdrant 里
        的 UTC ``created_at`` 字符串可比较。
        """
        # now 一定有 tz(consolidate() 已注入)
        local_now = now.astimezone(LOCAL_TZ)
        if level == ConsolidationLevel.DAILY:
            # 锁死到本地昨日整天
            today_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
            start = today_start - timedelta(days=1)        # 本地昨日 00:00
            end = today_start                              # 本地今日 00:00
            label = f"昨天 {start.date()} ~ {end.date()}"
        elif level == ConsolidationLevel.WEEKLY:
            # 滚动 7 天,以本地 0 点对齐右边界(避免跨日漂移)
            today_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
            end = today_start
            start = today_start - timedelta(days=DAILY_TO_WEEKLY_DAYS)
            label = f"最近{_DAYS_PER_WEEK}天 {start.date()} ~ {end.date()}"
        else:
            # 滚动 30 天,同上
            today_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
            end = today_start
            start = today_start - timedelta(days=WEEKLY_TO_MONTHLY_DAYS)
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
        # 🆕 2026-10-12:_resolve_window 返回的是本地时区(Asia/Shanghai)datetime,
        # 而 Qdrant.created_at 是 UTC iso 字符串(可能 +00:00 也可能 +08:00),
        # 直接字典序比较会跨时区乱套。统一转 UTC 后再 iso 化再比较。
        since_utc = since.astimezone(UTC)
        now_utc_t = now.astimezone(UTC)
        since_iso = since_utc.isoformat()
        now_iso = now_utc_t.isoformat()
        out: list[dict[str, Any]] = []
        for it in all_items:
            created = it.get("created_at", "") or it.get(PAYLOAD_CREATED_AT, "")
            if not created:
                continue
            # 🆕 2026-11-02: 跨天 task 归一化
            #
            # 一个 task 可能跨午夜(比如"订机票"聊了三天)。如果按 fact 自己的
            # created_at 切时间窗,这个 task 会被切成 3 段 —— task 断裂,
            # 日报里看不到"这件事还没完"。
            #
            # 解决:fact 的 tags 里带 `task_started_at:`(写入时记的 task 起始
            # 时间),窗口过滤用**双条件**:
            #
            #   fact 自身时间 ∈ 窗口  **或**  task 起始时间 ∈ 窗口
            #
            # 为什么是"或"而不是只看 task 起始时间:
            # - 只看 task 起始时间 → task 从 10/31 开始,11/01 的后续内容
            #   会落进 10/31 的日报,而 11/01 的日报里**看不到这件事还在推进**
            #   (漏掉当天真实发生的内容)
            # - 只看 fact 自身时间 → 跨天 task 被切成两段(任务视角断裂)
            #
            # "或"的语义:一个 fact 只要**当天有活动**,或**这个 task 当天开始**,
            # 就该进当天的日报。前者保证"当天发生的事不丢",后者保证
            # "新任务开启当天就有记录"。
            #
            # 老数据没有这个 tag → 退回用 created_at(行为不变)。
            tags = list(it.get("tags", []) or [])
            task_started = parse_task_started_at(tags)
            in_window = since_iso <= created < now_iso
            task_started_in_window = bool(
                task_started and since_iso <= task_started < now_iso
            )
            if not (in_window or task_started_in_window):
                continue
            if it.get(PAYLOAD_DELETED):
                continue
            out.append({
                "key": it["key"],
                "content": it.get("content", ""),
                "importance": float(it.get("importance", 0.5)),
                "created_at": created,
                # 🆕 2026-11-02: 归属信息 —— 升格按它分桶(user)与分段(task)
                "tags": tags,
            })
        return out

    # ------------------- 🆕 分组(归属保持) -------------------

    def _group_by_subject(
        self,
        candidates: list[dict[str, Any]],
    ) -> list[SubjectGroup]:
        """按**主体(说话人)**分桶,桶内按 task 分段。

        🆕 2026-11-02。**归属保持的核心。**

        归档层级(日报/周报/月报)= 时间粒度,决定"产出几条";
        段落层级(task)= 工作单元粒度,决定"每条里面怎么分段"。
        两者正交 —— 分桶键是 user,**不是** task。

        类比:员工一天做 2 个项目,向领导汇报**一条日报**,
        分别讲这两个项目的进展和问题。若按 task 分桶就变成 2 条日报,拆太散。

        分桶规则:
        1. 每条 fact 解析出 ``user:`` tag(主归属)和 ``task:`` tag(段落锚点)
        2. **共享 task 裁决**:同一 task 被多人参与时,归给该 task 内
           消息数最多的说话人(主说话人)—— 避免"两人聊一件事"
           被切成两条各说一半的日报
        3. 组内按 task 聚成段落(:func:`_build_segments`)

        Args:
            candidates: :meth:`_scan` 的结果(含 tags)。

        Returns:
            组列表(按 fact 数量倒序)。
        """
        # ── 步骤 1: 解析每条 fact 的归属 ──
        # fact → {"user": uid, "tasks": [tid...], "fact": {...}}
        parsed: list[dict[str, Any]] = []
        for c in candidates:
            tags = list(c.get("tags", []) or [])
            uid = parse_user_tag(tags) or UNKNOWN_USER
            tids = parse_task_tags(tags)
            parsed.append({
                "user": uid,
                "tasks": tids,
                "fact": c,
            })

        # ── 步骤 2: 共享 task 裁决 ──
        # 统计每个 task 内各说话人的 fact 数,选出"主说话人"。
        # 只有该 task 的**全部** fact 都归同一人时,才没有歧义。
        task_speaker_counts: dict[str, dict[str, int]] = {}
        for p in parsed:
            for tid in p["tasks"]:
                task_speaker_counts.setdefault(tid, {})
                counts = task_speaker_counts[tid]
                counts[p["user"]] = counts.get(p["user"], 0) + 1

        # task → 主说话人(消息数最多;并列时取 user_id 字典序最小,保证确定性)
        task_primary: dict[str, str] = {}
        for tid, counts in task_speaker_counts.items():
            task_primary[tid] = sorted(
                counts.items(), key=lambda kv: (-kv[1], kv[0])
            )[0][0]

        # ── 步骤 3: 按主体分桶 ──
        buckets: dict[str, list[dict[str, Any]]] = {}
        for p in parsed:
            user = p["user"]
            tids = p["tasks"]
            if tids:
                # 该 fact 属于一个或多个 task → 归属由**主说话人**决定
                # (多人共享 task 时,统一归主说话人,避免日报割裂)
                primaries = {task_primary.get(tid, user) for tid in tids}
                # 若该 fact 关联的 task 主说话人不一致(罕见:fact 带了多个 task),
                # 取其中"属于该 fact 说话人自己"的那个;否则取第一个,保持确定性
                user = user if user in primaries else sorted(primaries)[0]
            buckets.setdefault(user, []).append(p)

        # ── 步骤 4: 组装 SubjectGroup ──
        groups: list[SubjectGroup] = []
        for user, items in buckets.items():
            facts = [it["fact"] for it in items]
            task_ids: list[str] = []
            participants: set[str] = set()
            for it in items:
                for tid in it["tasks"]:
                    if tid not in task_ids:
                        task_ids.append(tid)
                # participants: 优先用 fact 上的 participants tag,
                # 没有则退回该 fact 的归属 user
                tag_p = parse_participants_tag(list(it["fact"].get("tags", []) or []))
                if tag_p:
                    participants.update(tag_p)
                else:
                    participants.add(it["user"])

            g = SubjectGroup(
                primary_user=user,
                task_ids=task_ids,
                participants=sorted(p for p in participants if p),
                facts=facts,
            )
            g.segments = self._build_segments(items)
            groups.append(g)

        # 排序:fact 多的组在前(稳定,同数量按 user_id 保证确定性)
        groups.sort(key=lambda g: (-g.fact_count, g.primary_user))
        logger.info(
            "consolidate.group_by_subject groups=%d users=%s facts=%d",
            len(groups), [g.primary_user for g in groups], len(candidates),
        )
        return groups

    @staticmethod
    def _build_segments(
        items: list[dict[str, Any]],
    ) -> list[tuple[str, list[dict[str, Any]]]]:
        """把组内 fact 按 task 聚成段落。

        段落 = 日报里的一个"项目小节"。无 task 归属的 fact(老数据)
        归到 task_id="" 的段落。

        Args:
            items: :meth:`_group_by_subject` 步骤 3 的分桶结果元素。

        Returns:
            ``[(task_id, [fact, ...]), ...]``,按 task 首次出现顺序。
        """
        seg_order: list[str] = []
        seg_map: dict[str, list[dict[str, Any]]] = {}
        for it in items:
            tids = it["tasks"]
            # 一条 fact 可能关联多个 task(理论上不该发生),取第一个作段落归属
            tid = tids[0] if tids else ""
            if tid not in seg_map:
                seg_map[tid] = []
                seg_order.append(tid)
            seg_map[tid].append(it["fact"])
        return [(tid, seg_map[tid]) for tid in seg_order]

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
        group: SubjectGroup,
        *,
        level: MemoryLevel,
        period: str,
    ) -> tuple[str, str]:
        """为一个主体组生成 1 条摘要。返回 (摘要文本, 方法:llm/heuristic)。

        🆕 2026-11-02:入参从"扁平 candidates"改为 :class:`SubjectGroup`,
        这样 LLM 拿到的是**已按 task 分好段的**输入 —— 让 LLM 只做
        "怎么讲清楚",不参与"怎么分组"(分组是代码的职责)。
        """
        summary: str | None = None
        try:
            summary = await self._llm_summarizer.summarize_group(
                group,
                level=level.value,
                period=period,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("consolidate.summarize_failed err=%s", e)
            summary = None

        if summary and summary.strip():
            return summary.strip(), "llm"

        # 降级:启发式拼接(仍按 task 分段,保证结构不塌)
        fallback = self._summarizer.summarize_text(
            [c["content"] for c in group.facts]
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
        group: SubjectGroup | None = None,
    ) -> str | None:
        """把 1 条摘要写进 Qdrant。失败返回 None。

        🆕 2026-11-02:**归属由代码在这里重新写一遍**。

        这是"归属跨三层不丢"不变式的实现方式 —— 不是靠 LLM 在正文里
        写上名字(那样每次升格都会漂移、无法断言),而是**每一层升格时
        由代码把归属 tag 重新写一遍**:

            fact  tags: user:X, task:Y
            日报  tags: user:X, task:Y      ← 这里写
            周报  tags: user:X, task:Y      ← 同上

        LLM 只负责正文措辞,归属永远由代码守。
        """
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

        tags = [
            level_tag(target_level),
            f"period:{target_level.value}",
            f"src_count:{source_count}",
        ]

        # 🆕 归属 tag —— 代码写,不是 LLM 生成
        if group is not None:
            tags.append(user_tag(group.primary_user))
            p_tag = participants_tag(group.participants)
            if p_tag:
                tags.append(p_tag)
            # task 锚点:一条日报可包含多个 task(日报内部按项目分段)
            for tid in group.task_ids:
                t = task_tag(tid)
                if t:
                    tags.append(t)

        # key 里带上归属,人工排查时一眼看出这条日报是谁的
        key_suffix = f"{group.primary_user}:" if group is not None else ""
        new_key = (
            f"consolidated:{target_level.value}:{key_suffix}{uuid.uuid4().hex[:12]}"
        )
        self._store.store_memory(
            new_key,
            embedding,
            content,
            importance=importance,
            tags=tags,
            level=target_level.value,
            source_keys=source_keys,
        )
        logger.info(
            "consolidate.write_summary key=%s level=%s user=%s tasks=%d",
            new_key, target_level.value,
            group.primary_user if group else "(none)",
            len(group.task_ids) if group else 0,
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
    "SubjectGroup",
]
