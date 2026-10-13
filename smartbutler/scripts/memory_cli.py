"""Memory 手动管理 CLI(Phase 6.3 P1,见 §2.5.1.16)。

7 个子命令:
- ``list-deleted`` —— 列出软删除的事实(运维/审计)
- ``age-summary`` —— 按软删除时长分桶统计
- ``stats`` —— 显示容量统计
- ``consolidate`` —— 跑一次 4 层升格管道(daily/weekly/monthly)
- ``vacuum`` —— 跑一次 vacuum(支持 dry_run)
- ``purge`` —— 立即物理删某条(需要 confirm=yes)
- ``purge-before`` / ``purge-archived`` —— 按时长批量删
- ``dump`` —— 全量翻页看库里到底存了什么(运维/排查)
- ``dump-fields`` —— payload 字段分布体检

短期记忆(短期,读 short_term.db,不是 Qdrant):
- ``stm-threads`` —— 列出所有 thread + checkpoint 数
- ``stm-dump`` —— 全量翻出对话消息流
- ``stm-stats`` —— 容量体检
- ``st-raw`` —— 打印某个 checkpoint 的原始 state(JSON)

设计要点:
1. **纯 stdlib + 项目内依赖** —— 无 typer / click,只用 argparse
2. **独立入口** —— ``python -m smartbutler.scripts.memory_cli <cmd> [args]``
3. **不阻塞管家主流程** —— CLI 是手动工具,跟 LongTermStore 共享 SQLite/Qdrant 文件
4. **dry_run 默认开启** —— 防止误操作

注意:CLI 当前是"用 Qdrant 嵌入式 + 默认 data/ 目录"。生产环境如有 custom path,改 env:
- ``SMARTBUTLER_STORAGE_QDRANT_PATH``
- ``SMARTBUTLER_STORAGE_QDRANT_URL``
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any

from smartbutler.emotion.memory.archive_store import ArchiveStore
from smartbutler.emotion.memory.consolidation import Consolidator
from smartbutler.emotion.memory.hooks import MemoryHooks
from smartbutler.emotion.memory.long_term import LongTermStore
from smartbutler.emotion.memory.summarizer import (
    FiveW1HExtractor,
    HeuristicSummarizer,
    LLMScorer,
    LLMSummarizer,
)
from smartbutler.emotion.memory.vacuum import VacuumService
from smartbutler.storage.qdrant import QdrantStorage


def _build_store(args: argparse.Namespace) -> LongTermStore:
    """从 args 建 LongTermStore(CLI 用)。"""
    from smartbutler.emotion.memory.long_term import create_long_term_store
    return create_long_term_store(
        qdrant_path=Path(args.qdrant_path) if args.qdrant_path else None,
        qdrant_url=args.qdrant_url,
        qdrant_api_key=args.qdrant_api_key,
        collection=args.collection,
        vector_size=args.vector_size,
        distance="Cosine",
    )


def _try_build_llm() -> Any | None:
    """尽力建一个 LLM 实例,失败返回 None(调用方降级)。

    consolidate 的摘要依赖 LLM,但 CLI 不能因为没配 api_key 就崩 ——
    返回 None 时 :class:`LLMSummarizer` 自动退化为拼接兜底。
    """
    try:
        from smartbutler.config.llm import load_llm_settings
        from smartbutler.capabilities.llm.factory import create_llm

        return create_llm(load_llm_settings())
    except Exception as e:  # noqa: BLE001
        print(f"⚠️  LLM 初始化失败({e}),将退化为无 LLM 模式。")
        return None


def cmd_list_deleted(args: argparse.Namespace) -> int:
    """list-deleted: 列出软删除的事实(支持 --by-age 分桶显示)。"""
    store = _build_store(args)
    archive = ArchiveStore(Path(args.archive_path))
    vs = VacuumService(store=store, archive=archive, embedder=None)
    # --by-age 优先走分桶
    if args.by_age:
        return _print_age_buckets(vs, args.level, header="(via list-deleted --by-age)")
    items = vs.list_deleted(
        level=args.level,
        limit=args.limit,
        older_than_days=args.older_than_days,
    )
    if not items:
        print("No deleted memories found.")
        return 0
    print(f"Found {len(items)} deleted memories:\n")
    for it in items:
        age = it.deleted_at[:19] if it.deleted_at else "?"
        snippet = it.content[:80] + "..." if len(it.content) > 80 else it.content
        print(
            f"  [{age}] level={it.level or '-':<8} reason={it.deleted_reason or '-':<12} "
            f"imp={it.importance:.2f} key={it.key}"
        )
        print(f"      content: {snippet}")
    if len(items) == args.limit:
        print(
            f"\n⚠️  Hit --limit={args.limit}. "
            f"Increase --limit or use --by-age to see totals."
        )
    else:
        print(
            f"\nTotal: {len(items)} items. "
            f"Run `memory_cli vacuum` to archive (>7 days) or purge (>30 days)."
        )
    return 0


def _print_age_buckets(vs: VacuumService, level: str, header: str = "") -> int:
    """按软删除时长分桶打印 + 行动建议。"""
    buckets = vs.count_deleted_by_age(level=level)
    print(f"Deleted memories by age (level={level}){': ' + header if header else ''}:\n")
    for label, n in buckets.items():
        if label == "_total":
            continue
        bar = "█" * min(n, 50)
        if n > 50:
            bar += f" (+{n - 50} more)"
        print(f"  {label:<10} {n:>6}  {bar}")
    print(f"  {'TOTAL':<10} {buckets['_total']:>6}\n")
    # 行动建议
    if buckets["7-30d"] > 0:
        print(
            f"💡 {buckets['7-30d']} items have been in 'deleted_recoverable' for 7-30 days.\n"
            f"   →  Run `memory_cli vacuum` to move them to archived SQLite (still recoverable)."
        )
    if buckets["30-90d"] > 0:
        print(
            f"💡 {buckets['30-90d']} items have been archived for 30-90 days (eligible for purge).\n"
            f"   →  Run `memory_cli vacuum --no-dry-run` to physical-delete them."
        )
    if buckets["90-180d"] + buckets["180-365d"] + buckets[">365d"] > 0:
        old_total = buckets["90-180d"] + buckets["180-365d"] + buckets[">365d"]
        print(
            f"⚠️  {old_total} items have been archived > 90 days.\n"
            f"   →  Run `memory_cli purge-archived --older-than-days 90 --confirm yes` to bulk delete."
        )
    return 0


def cmd_age_summary(args: argparse.Namespace) -> int:
    """age-summary: 按软删除时长分桶统计(快速看有多少"该清"的了)。"""
    store = _build_store(args)
    archive = ArchiveStore(Path(args.archive_path))
    vs = VacuumService(store=store, archive=archive, embedder=None)
    return _print_age_buckets(vs, args.level)


def cmd_purge_before(args: argparse.Namespace) -> int:
    """purge-before: 按软删除时长批量物理删(deleted_recoverable 里的)。"""
    if args.confirm != "yes":
        print(
            "ERROR: --confirm must be 'yes' to proceed.\n"
            "  This is a safety check to prevent accidental deletion.\n"
            "  Example: memory_cli purge-before --older-than-days 30 --confirm yes",
            file=sys.stderr,
        )
        return 2
    if args.older_than_days < 7:
        print(
            f"ERROR: --older-than-days must be >= 7 (反悔期保护).\n"
            f"  You passed {args.older_than_days}. For < 7 day items, use `purge --key` individually.",
            file=sys.stderr,
        )
        return 2
    store = _build_store(args)
    archive = ArchiveStore(Path(args.archive_path))
    vs = VacuumService(store=store, archive=archive, embedder=None)
    # 先 dry-run 报告
    if args.dry_run:
        items = vs.list_deleted(
            level=args.level, limit=50_000, older_than_days=args.older_than_days,
        )
        print(
            f"⚠️  DRY RUN: would purge {len(items)} items\n"
            f"   (level={args.level}, older_than_days={args.older_than_days})\n"
        )
        for it in items[:10]:
            age = it.deleted_at[:19] if it.deleted_at else "?"
            print(f"   [{age}] {it.key}: {it.content[:60]}")
        if len(items) > 10:
            print(f"   ... and {len(items) - 10} more")
        print(f"\nPass --no-dry-run --confirm yes to apply.")
        return 0
    # 真跑
    print(
        f"Purging all deleted_recoverable items older than {args.older_than_days} days "
        f"(level={args.level})..."
    )
    result = vs.bulk_purge_before(
        older_than_days=args.older_than_days, confirm="yes",
    )
    print(f"\n✅ Done. requested={result['requested']}, "
          f"purged={result['purged']}, skipped={result['skipped']}")
    if result.get("errors"):
        print(f"⚠️  {len(result['errors'])} errors:")
        for e in result["errors"][:5]:
            print(f"   - {e}")
    return 0


def cmd_purge_archived(args: argparse.Namespace) -> int:
    """purge-archived: 按 archived 时长批量物理删(SQLite 里的)。"""
    if args.confirm != "yes":
        print(
            "ERROR: --confirm must be 'yes' to proceed.\n"
            "  Example: memory_cli purge-archived --older-than-days 90 --confirm yes",
            file=sys.stderr,
        )
        return 2
    store = _build_store(args)
    archive = ArchiveStore(Path(args.archive_path))
    vs = VacuumService(store=store, archive=archive, embedder=None)
    if args.dry_run:
        items = archive.list_all(older_than_days=args.older_than_days, limit=50_000)
        print(
            f"⚠️  DRY RUN: would purge {len(items)} archived items\n"
            f"   (older_than_days={args.older_than_days})\n"
        )
        for it in items[:10]:
            arc_at = it.archived_at[:19] if it.archived_at else "?"
            print(f"   [{arc_at}] {it.key}: {it.content[:60]}")
        if len(items) > 10:
            print(f"   ... and {len(items) - 10} more")
        print(f"\nPass --no-dry-run --confirm yes to apply.")
        return 0
    print(
        f"Purging all archived items older than {args.older_than_days} days..."
    )
    result = vs.bulk_purge_archived(
        older_than_days=args.older_than_days, confirm="yes",
    )
    print(f"\n✅ Done. requested={result['requested']}, purged={result['purged']}")
    if result.get("errors"):
        print(f"⚠️  {len(result['errors'])} errors:")
        for e in result["errors"][:5]:
            print(f"   - {e}")
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    """stats: 显示容量统计。"""
    store = _build_store(args)
    archive = ArchiveStore(Path(args.archive_path))
    vs = VacuumService(store=store, archive=archive, embedder=None)
    stats = vs.vacuum_stats()
    print("Memory stats:\n")
    # 业务条目数
    print("  --- Business data (your actual memories) ---")
    print(f"  {'active':<25} {stats['active']:>12,} entries")
    print(f"  {'deleted_recoverable':<25} {stats['deleted_recoverable']:>12,} entries  (soft-deleted, 7-day grace)")
    print(f"  {'archived':<25} {stats['archived']:>12,} entries  (in SQLite backup, 30-day grace)")
    business_total = stats['active'] + stats['deleted_recoverable'] + stats['archived']
    print(f"  {'TOTAL business':<25} {business_total:>12,} entries")
    # 磁盘占用
    print()
    print("  --- Storage on disk ---")
    q_overhead = max(0, stats['qdrant_storage_bytes'] - stats['qdrant_user_data_bytes'])
    print(f"  {'qdrant_user_data_bytes':<25} {stats['qdrant_user_data_bytes']:>12,} bytes  (your vectors + payload)")
    if q_overhead > 0:
        print(f"  {'qdrant_metadata_overhead':<25} {q_overhead:>12,} bytes  (Qdrant internal index, ~12K even for empty)")
    print(f"  {'qdrant_storage_bytes':<25} {stats['qdrant_storage_bytes']:>12,} bytes  ({stats['qdrant_storage_bytes'] / 1024 / 1024:.2f} MB)")
    print(f"  {'archive_storage_bytes':<25} {stats['archive_storage_bytes']:>12,} bytes  ({stats['archive_storage_bytes'] / 1024 / 1024:.2f} MB)")
    print(f"  {'total_storage_bytes':<25} {stats['total_storage_bytes']:>12,} bytes  ({stats['total_storage_bytes'] / 1024 / 1024:.2f} MB)")
    # 友好提示
    print()
    if business_total == 0:
        print("  💡 No business data yet. The 12K+ disk usage is just Qdrant's empty")
        print("     collection overhead — not your data. Once you `remember()` something,")
        print("     it'll show up under 'active' and 'qdrant_user_data_bytes' will grow.")
    elif stats['deleted_recoverable'] > 0 or stats['archived'] > 0:
        to_clean = stats['deleted_recoverable'] + stats['archived']
        print(f"  💡 {to_clean} soft-deleted/archived items pending cleanup.")
        print(f"     Run `memory_cli age-summary` to see what to purge.")
    return 0


def cmd_vacuum(args: argparse.Namespace) -> int:
    """vacuum: 跑一次 vacuum。"""
    store = _build_store(args)
    archive = ArchiveStore(Path(args.archive_path))
    vs = VacuumService(store=store, archive=archive, embedder=None)
    dry = args.dry_run
    print(f"Running vacuum (dry_run={dry})...")
    report = vs.vacuum_once(dry_run=dry)
    print(f"\nVacuum report:")
    print(f"  scanned_total    {report.scanned_total:>6}")
    print(f"  to_archive       {report.to_archive:>6}")
    print(f"  to_purge         {report.to_purge:>6}")
    if not dry:
        print(f"  archived_now     {report.archived_now:>6}")
        print(f"  purged_now       {report.purged_now:>6}")
    if report.error_keys:
        print(f"  error_keys       {len(report.error_keys)}")
        for k in report.error_keys[:5]:
            print(f"    - {k}")
    if dry:
        print("\n⚠️  DRY RUN: no changes were made. Pass --no-dry-run to apply.")
    else:
        print(f"\n✅ Vacuum done. archived={report.archived_now}, purged={report.purged_now}")
    return 0


def cmd_purge(args: argparse.Namespace) -> int:
    """purge: 立即物理删一条。"""
    if args.confirm != "yes":
        print(
            "ERROR: --confirm must be 'yes' to proceed.\n"
            "  This is a safety check to prevent accidental deletion.\n"
            "  Example: memory_cli purge --key 'mem:user1:abc' --confirm yes",
            file=sys.stderr,
        )
        return 2
    store = _build_store(args)
    archive = ArchiveStore(Path(args.archive_path))
    vs = VacuumService(store=store, archive=archive, embedder=None)
    ok = vs.purge_now(args.key, confirm=args.confirm)
    if ok:
        print(f"✅ Purged: {args.key}")
        return 0
    print(f"❌ Purge failed: key '{args.key}' not found.", file=sys.stderr)
    return 1


def cmd_recall(args: argparse.Namespace) -> int:
    """recall: 语义检索 + 重要性过滤(直跑 MemoryRetriever.recall,不走 facade/hooks)。"""
    from smartbutler.emotion.memory.embedder import build_default_embedder
    from smartbutler.emotion.memory.retrieval import MemoryRetriever

    store = _build_store(args)
    embedder = build_default_embedder()
    retriever = MemoryRetriever(store=store, embedder=embedder)

    # retriever.recall 是 async,这里没 loop,起一个
    hits = asyncio.run(
        retriever.recall(
            args.query,
            k=args.k,
            min_importance=args.min_importance,
            tag=f"source:{args.source}" if args.source else None,
            min_score=args.min_score,
        )
    )
    print(
        f"Recall: query={args.query!r}  "
        f"k={args.k}  min_importance={args.min_importance}  min_score={args.min_score}"
        + (f"  source={args.source}" if args.source else "")
    )
    print(f"Hits: {len(hits)}\n")
    if not hits:
        print("  (no hits)")
        return 0
    for i, h in enumerate(hits, 1):
        score = h.get("score", 0.0)
        raw = h.get("raw_score", 0.0)
        imp = h.get("importance", 0.0)
        key = h.get("key", "?")
        content = h.get("content", "")
        created = h.get("created_at", "?")
        tags = h.get("tags") or []
        snippet = content if len(content) <= 200 else content[:200] + "..."
        print(
            f"  [{i}] score={score:.3f}  raw={raw:.3f}  "
            f"importance={imp:.2f}  key={key}"
        )
        print(f"      created_at={created}  tags={tags}")
        print(f"      content: {snippet}")
    return 0


def cmd_consolidate(args: argparse.Namespace) -> int:
    """consolidate: 把一个时间段的 fact 聚合成 1 条摘要(3 层:day/week/month)。

    走底层 :class:`Consolidator`(不绕 facade),所以 CLI 是独立可用,
    不依赖 facade 是否已 bind consolidator。

    🆕 2026-10-12 语义变化:旧实现是"N 条 fact → N 条 daily"(压缩率 0),
    现在是"N 条 fact → 1 条日报",weekly/monthly 同理逐层收敛。
    源 fact **不删除**(留给 forget 策略按时间窗处理)。
    """
    from datetime import UTC, datetime

    from smartbutler.emotion.memory.consolidation import ConsolidationLevel
    from smartbutler.emotion.memory.embedder import build_default_embedder
    from smartbutler.emotion.memory.levels import MemoryLevel

    level = args.level
    dry_run = args.dry_run
    store = _build_store(args)
    embedder = build_default_embedder()
    hooks = MemoryHooks()

    # 🆕 接真实 LLM(--no-llm 可强制降级到拼接兜底,便于对比/省钱)
    llm = None if getattr(args, "no_llm", False) else _try_build_llm()
    if llm is None:
        print("⚠️  未启用 LLM,摘要将退化为拼接兜底(不压缩)。")

    c = Consolidator(
        store=store,
        embedder=embedder,
        llm_scorer=LLMScorer(llm),
        llm_summarizer=LLMSummarizer(llm),
        hooks=hooks,
    )

    if dry_run:
        # 🆕 2026-10-12:CLI dry_run 与正式 schedule 行为分离
        # - schedule consolidate():[本地昨日 00:00, 本地今日 00:00) 锁死
        # - CLI dry_run          :[本地昨日 00:00, 当前) 扫到当下,方便手工测试
        from zoneinfo import ZoneInfo as _ZI
        _LOCAL_TZ = _ZI("Asia/Shanghai")
        now_utc = datetime.now(UTC)
        now_local = now_utc.astimezone(_LOCAL_TZ)
        lvl = ConsolidationLevel(level)
        window = c._resolve_window(lvl, now_utc)
        source_level = {
            "daily": None,
            "weekly": MemoryLevel.DAILY.value,
            "monthly": MemoryLevel.WEEKLY.value,
        }[level]
        # 扫到当前(本地时区),让手工调试能扫到刚写的 fact
        cand = c._scan(
            level=source_level,
            since=window["since"],
            now=now_local,
            limit=10_000,
        )
        print(f"\n🔍 DRY RUN consolidate --level {level}")
        print(f"  时间窗              {window['label']}(扫到当前 {now_local.strftime('%Y-%m-%d %H:%M:%S %Z')})")
        print(f"  源层级              {source_level or '(无 level 的原始 fact)'}")
        print(f"  candidates_scanned  {len(cand):>6} 条")
        low = sum(1 for it in cand if it.get("importance", 0.5) < 0.2)
        print(f"  → importance<0.2   {low:>6} 条(会被门控跳过)")
        print(f"  → 预计产出          {1 if len(cand) - low > 0 else 0:>6} 条摘要")
        if len(cand) - low > 0:
            print(f"  → 压缩比            {(len(cand) - low):>6}:1")
        print("\n  💡 加 --no-llm 可看降级效果;去掉 --dry-run 真跑(真跑按 schedule 规则:仅到本地今日 00:00)。")
        return 0

    # 真跑
    report = asyncio.run(c.consolidate(level=level))
    print(f"\n✅ Consolidate done (level={level})")
    print(f"  时间窗              {report.period}")
    print(f"  candidates_scanned  {report.candidates_scanned:>6} 条")
    print(f"  汇总源 fact         {report.source_count:>6} 条")
    print(f"  promoted            {report.promoted:>6} 条摘要")
    print(f"  压缩比              {report.compression_ratio:>6}x")
    print(f"  摘要方式            {report.summary_method or '(未生成)':>6}")
    print(f"  skipped_low_import  {report.skipped_low_importance:>6} 条(<0.2)")
    print(f"  源 fact            {'保留(由 forget 策略处理)':>6}")
    if report.errors:
        print(f"  errors               {len(report.errors):>6} 条(见 logs)")
    if report.new_keys:
        print(f"\n  new_keys:")
        for nk in report.new_keys:
            print(f"    - {nk}")
    return 0


# ===========================================================================
# clear-all: 一键清空所有长短期记忆(危险操作!)
#
# 范围:
#   1. 长期记忆 active + soft-deleted points(Qdrant collection)
#   2. 长期记忆 archived SQLite 备份表(deleted_archive.db)
#   3. 短期记忆(LangGraph checkpoints + writes,short_term.db)
#
# 实现策略(为什么用 delete_collection 而不是 scroll 全部 id 再 delete):
#   - 一条条删 / 分批 delete 都依赖 scroll 拿全 id(>10K 还要翻页),
#     慢、占内存、还要对每条算 point_id(hash)再 delete,容易漏
#   - 嵌入式 Qdrant 的 collection 本质是一个目录,删了重建是 OS 级最干净
#   - 重建是 _client.delete_collection + 下次 upsert 时按 vector_size/distance
#     自动建(见 QdrantStorage 的"集合自动创建"逻辑)
#
# 短期记忆:SqliteSaver 的存储表是 ``checkpoints`` + ``writes``(LangGraph
#   标准 schema),直接 ``DELETE FROM`` 两个表 + VACUUM 释放空间。
#
# 可选 [4]:data/task_index.db(6.5 task scheduler 状态)——
#   跟"长短期记忆"是不同子系统,所以默认不动。
#   加 ``--include-task-index`` 才一并清(用户决策:
#   "管家所有内部状态全清"才传这个开关)。
# ===========================================================================


def cmd_clear_all(args: argparse.Namespace) -> int:
    """clear-all: 一键清空所有长短期记忆。

    危险操作 —— 三重安全检查:
    1. ``--confirm yes`` 必须显式传
    2. ``--yes-i-know-this-deletes-everything`` 必须显式传
    3. ``--dry-run`` 默认开启(只打印会做什么,不动数据)
    """
    if args.confirm != "yes":
        print(
            "ERROR: --confirm must be 'yes' to proceed.\n"
            "  Example: memory_cli clear-all --confirm yes --yes-i-know-this-deletes-everything",
            file=sys.stderr,
        )
        return 2
    if not getattr(args, "yes_i_know", False):
        print(
            "ERROR: 必须显式传 --yes-i-know-this-deletes-everything 作为最后一道防线。\n"
            "  防止误操作删除全部记忆。",
            file=sys.stderr,
        )
        return 2

    dry = args.dry_run

    # ----- 1) 长期记忆:Qdrant collection -----
    long_path = Path(args.qdrant_path) if args.qdrant_path else None
    long_url = args.qdrant_url
    collection = args.collection

    print("=" * 60)
    print(" clear-all: 计划清空的项")
    print("=" * 60)
    print(f"  [1] 长期记忆 Qdrant collection")
    print(f"      collection = {collection!r}")
    print(f"      mode       = {'server' if long_url else 'embedded'}")
    if long_path is not None:
        print(f"      path       = {long_path}")
    if long_url is not None:
        print(f"      url        = {long_url}")
    print()
    print(f"  [2] 长期记忆 archived SQLite 备份表")
    archive_path = Path(args.archive_path)
    print(f"      path       = {archive_path}")
    print()
    print(f"  [3] 短期记忆 short-term SQLite")
    stm_path = Path(args.stm_path)
    print(f"      path       = {stm_path}")
    if getattr(args, "include_task_index", False):
        print()
        print(f"  [4] Task 切分索引 SQLite(6.5 task scheduler 状态)")
        ti_path = Path(args.task_index_path)
        print(f"      path       = {ti_path}")
    else:
        print()
        print(f"  [ℹ️ ] Task 切分索引 (data/task_index.db) 默认不动。")
        print(f"        加 --include-task-index 一并清空(同时清空 tasks / task_messages / _processed_cursor)。")
    print()

    if dry:
        print("⚠️  DRY RUN: 上面是要清的内容,实际未执行。")
        print("   去掉 --dry-run 并加 --yes-i-know-this-deletes-everything 真跑。")
        return 0

    # ----- 1) 真跑:Qdrant collection -----
    print("[1/3] 清空 Qdrant collection ...")
    try:
        store = _build_store(args)
        q = store.qdrant
        client = q._client  # noqa: SLF001 —— CLI 工具用,明确接受私有访问
        collection_name = q.collection

        # 存在性检查:不存在的 collection 走 recreate 反而要算 vector_size,
        # 直接吞 not-found 异常更简单
        try:
            client.delete_collection(collection_name=collection_name)
            print(f"      ✅ deleted collection {collection_name!r}")
        except Exception as e:  # noqa: BLE001
            # 服务模式下 collection 不存在会抛 UnexpectedResponse;嵌入式不存在会静默
            err_name = type(e).__name__
            if "NotFound" in err_name or "404" in str(e):
                print(f"      ℹ️  collection {collection_name!r} 不存在,跳过")
            else:
                raise
        # 重建空的 collection,这样下次 store_memory() 不用 lazy create,
        # 避免冷启动首次 remember() 报"collection 不存在"
        try:
            from qdrant_client.models import Distance, VectorParams
            client.create_collection(
                collection_name=collection_name,
                vectors_config=VectorParams(
                    size=q._vector_size,  # noqa: SLF001
                    distance=Distance.COSINE,
                ),
            )
            print(f"      ✅ recreated empty collection {collection_name!r}")
        except Exception as e:  # noqa: BLE001
            # 已存在也不致命(可能 service 模式有 race),让 store_memory 兜底
            print(f"      ⚠️  recreate 跳过({type(e).__name__}: {e})")
    except Exception as e:  # noqa: BLE001
        # 关键:dev_chat 还在跑时,_build_store 会抛 AlreadyLocked
        # 这种情况必须给清晰报错
        err_name = type(e).__name__
        if "AlreadyLocked" in err_name or "Permission" in err_name:
            print(
                f"      ⚠️  嵌入式 Qdrant 被另一个进程锁着(可能是 dev_chat 还活着)。\n"
                f"          退出 dev_chat 再跑 clear-all 才安全。\n"
                f"          ({err_name}: {e})",
                file=sys.stderr,
            )
            return 1
        print(f"      ❌ Qdrant 清空失败:{err_name}: {e}", file=sys.stderr)
        return 1

    # ----- 1b) 物理兜底:嵌入式 Qdrant 残留 storage.sqlite 不会被 delete_collection 真正回收
    # delete_collection 只改内存视图(嵌入式是 RAM 索引),磁盘 storage.sqlite 留着旧 points。
    # 重建 collection 不会覆盖旧文件。QdrantClient 析构会刷盘,但 CLI 进程退出时 Windows
    # portalocker 析构会报 NoneType 告警(已知 Qdrant 1.19 bug)——关闭 client 后再 rmtree
    # 物理子目录,下次启动会让 QdrantClient 重建空 collection。
    if not args.qdrant_url:
        # 嵌入式模式才需要这个兜底
        try:
            qdrant_path = Path(str(q._path))  # noqa: SLF001
            coll_dir = qdrant_path / "collection" / collection_name
            # 先显式关 client(否则 portalocker 锁住无法删)
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass
            if coll_dir.exists():
                import shutil
                shutil.rmtree(coll_dir)
                print(f"      🧹 物理清理残留 collection 目录:{coll_dir}")
        except Exception as e:  # noqa: BLE001
            # 兜底失败也不致命(delete_collection + create_collection 已经在内存层面生效)
            print(f"      ⚠️  物理清理失败(不影响逻辑清空):{type(e).__name__}: {e}", file=sys.stderr)

    # ----- 2) 真跑:archived SQLite -----
    print("[2/3] 清空 archived SQLite 备份表 ...")
    if not archive_path.exists():
        print(f"      ℹ️  备份文件不存在,跳过:{archive_path}")
    else:
        n = _wipe_sqlite(archive_path)
        if n is None:
            return 1
        print(f"      ✅ 删了 {n} 行")

    # ----- 3) 真跑:short-term SQLite -----
    print("[3/3] 清空短期记忆 SQLite ...")
    if not stm_path.exists():
        print(f"      ℹ️  短期记忆文件不存在,跳过:{stm_path}")
    else:
        n = _wipe_sqlite(stm_path)
        if n is None:
            return 1
        print(f"      ✅ 删了 {n} 行")

    # ----- 可选 [4]:Task 切分索引 -----
    if getattr(args, "include_task_index", False):
        ti_path = Path(args.task_index_path)
        print("[4/4] 清空 Task 切分索引 SQLite ...")
        if not ti_path.exists():
            print(f"      ℹ️  索引文件不存在,跳过:{ti_path}")
        else:
            n = _wipe_sqlite(ti_path)
            if n is None:
                return 1
            print(f"      ✅ 删了 {n} 行")

    print()
    print("=" * 60)
    if getattr(args, "include_task_index", False):
        print(" ✅ clear-all 完成。所有长短期记忆 + Task 索引已清空。")
    else:
        print(" ✅ clear-all 完成。所有长短期记忆已清空(Task 索引未动)。")
    print("=" * 60)
    return 0


def _wipe_sqlite(path: Path) -> int | None:
    """清空一个 SQLite 文件里的所有表(全 DELETE + VACUUM)。

    Args:
        path: SQLite 文件路径(只读模式不能 DELETE,所以这里是读写)。

    Returns:
        删掉的行数;文件不存在返回 None(不致命);异常也返回 None 并打 stderr(致命)。
    """
    import sqlite3
    try:
        conn = sqlite3.connect(str(path), timeout=5.0)
    except Exception as e:  # noqa: BLE001
        print(f"      ❌ 打开失败:{type(e).__name__}: {e}", file=sys.stderr)
        return None
    try:
        cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = [r[0] for r in cur.fetchall()]
        if not tables:
            print(f"      ℹ️  没有表(可能文件是空的):{path}")
            return 0
        n_total = 0
        for tbl in tables:
            n = conn.execute(f"SELECT COUNT(*) FROM \"{tbl}\"").fetchone()[0]
            conn.execute(f"DELETE FROM \"{tbl}\"")
            n_total += n
        conn.commit()
        # VACUUM 释放磁盘空间(失败不致命,数据已清)
        try:
            conn.execute("VACUUM")
        except Exception as ve:  # noqa: BLE001
            print(f"      ⚠️  VACUUM 失败(数据已清):{ve}")
        return n_total
    except Exception as e:  # noqa: BLE001
        print(f"      ❌ 清空失败:{type(e).__name__}: {e}", file=sys.stderr)
        return None
    finally:
        conn.close()


def cmd_dump(args: argparse.Namespace) -> int:
    """dump: 翻出 Qdrant 里的原始 points(看"到底存了什么")。

    与 ``recall`` 的区别:recall 走向量相似度,只返回 top-k;
    dump 走 scroll,**全量翻页**,不排序不看分数,用来回答
    "库里现在到底有哪些条目"。
    """
    store = _build_store(args)
    q = store.qdrant

    # 过滤条件:level / tag / importance 下限
    payload_filters: dict[str, Any] = {}
    if args.level != "any":
        payload_filters["level"] = args.level
    if args.tag:
        payload_filters["tags"] = args.tag

    # 分页 scroll 到 limit / --all
    rows: list[dict[str, Any]] = []
    offset: Any = None
    while True:
        page, offset = q.scroll_points(
            limit=args.page_size,
            offset=offset,
            payload_filters=payload_filters or None,
            include_deleted=not args.no_deleted,
        )
        for r in page:
            pl = r["payload"]
            if pl.get("importance", 0.0) < args.min_importance:
                continue
            rows.append(r)
        if offset is None:
            break
        if not args.all and len(rows) >= args.limit:
            break

    if not args.all:
        rows = rows[: args.limit]

    # 头部说明
    print(f"Qdrant dump: collection={q.collection!r} mode={store.mode}")
    print(
        f"  filter: level={args.level} tag={args.tag or '-'} "
        f"min_importance={args.min_importance} "
        f"include_deleted={not args.no_deleted}"
    )
    print(f"\n{len(rows)} point(s):\n")
    for i, r in enumerate(rows, 1):
        pl = r["payload"]
        deleted = bool(pl.get("deleted", False))
        snippet = pl.get("content", "")
        if len(snippet) > args.width:
            snippet = snippet[: args.width] + "..."
        print(
            f"  [{i}] {r['key']}"
            f"{'  [DELETED]' if deleted else ''}"
            f"  id={r['id']}"
        )
        print(
            f"      importance={pl.get('importance', 0.0):.2f} "
            f"level={pl.get('level', '-') or '-'} "
            f"created_at={pl.get('created_at', '?')}"
        )
        tags = pl.get("tags") or []
        if tags:
            print(f"      tags={tags}")
        if deleted:
            print(
                f"      deleted_at={pl.get('deleted_at', '?')} "
                f"reason={pl.get('deleted_reason', '-')}"
            )
        print(f"      content: {snippet}")
        print()

    if not args.all and len(rows) >= args.limit:
        print(
            f"⚠️  Hit --limit={args.limit}. Pass --all to dump everything."
        )
    return 0


def cmd_dump_fields(args: argparse.Namespace) -> int:
    """dump-fields: 汇总 payload 里的字段分布(快速体检)。"""
    store = _build_store(args)
    q = store.qdrant
    offset: Any = None
    total = 0
    n_deleted = 0
    imp_sum = 0.0
    levels: dict[str, int] = {}
    tag_counter: dict[str, int] = {}
    key_prefix: dict[str, int] = {}

    while True:
        page, offset = q.scroll_points(
            limit=args.page_size, offset=offset, include_deleted=True
        )
        for r in page:
            pl = r["payload"]
            total += 1
            if pl.get("deleted", False):
                n_deleted += 1
            imp_sum += float(pl.get("importance", 0.0) or 0.0)
            lv = pl.get("level") or "(none)"
            levels[lv] = levels.get(lv, 0) + 1
            for t in pl.get("tags") or []:
                tag_counter[t] = tag_counter.get(t, 0) + 1
            # key 前缀:mem:xxx → mem,mem:b86424be → mem:b86424be
            k = r["key"]
            prefix = ":".join(k.split(":")[:2]) if ":" in k else k
            key_prefix[prefix] = key_prefix.get(prefix, 0) + 1
        if offset is None:
            break

    print(f"Qdrant field summary: collection={q.collection!r} mode={store.mode}\n")
    print(f"  total_points      {total:>8}")
    print(f"  deleted           {n_deleted:>8}")
    print(f"  active            {total - n_deleted:>8}")
    if total:
        print(f"  avg_importance    {imp_sum / total:>8.3f}")
    print("\n  by level:")
    for lv, n in sorted(levels.items(), key=lambda kv: -kv[1]):
        bar = "█" * min(n, 50)
        print(f"    {lv:<14} {n:>6}  {bar}")
    if tag_counter:
        print("\n  by tag (top 20):")
        for t, n in sorted(tag_counter.items(), key=lambda kv: -kv[1])[:20]:
            print(f"    {t:<30} {n:>6}")
    if key_prefix:
        print("\n  by key prefix (top 20):")
        for pfx, n in sorted(key_prefix.items(), key=lambda kv: -kv[1])[:20]:
            print(f"    {pfx:<30} {n:>6}")
    return 0


# ===========================================================================
# 短期记忆(LangGraph checkpoint / short_term.db)
#
# 与上面所有长期记忆命令的区别:
#   长期 = Qdrant(points,向量)  → 事实/fact
#   短期 = SQLite(checkpoints)  → 对话消息流 + 工具调用中间态
#
# 直接读 SQLite 而不走 ShortTermMemory.get_async_saver():
#   saver 走 LangGraph 的 msgpack 编解码 + 需要 aiosqlite 异步连接,
#   而 CLI 是同步工具;更重要的是 saver 会持有连接与 run 中的管家进程
#   抢 SQLite 写锁。原始 blob + 自建解码器更安全,也更能暴露原始数据。
# ===========================================================================


def _open_stm_db(path: Path) -> Any:
    """只读打开 short_term.db(不复用 saver,避免抢连接/锁)。"""
    import sqlite3

    if not path.exists():
        raise FileNotFoundError(f"short-term DB 不存在: {path}")
    # file:...?mode=ro —— 纯只读,即使有别的进程在写也不影响我们读
    uri = f"file:{path.as_posix()}?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=5.0)


_SERDE: Any = None


def _get_serde() -> Any:
    """懒加载 JsonPlusSerializer(模块级单例,构造有开销)。"""
    global _SERDE
    if _SERDE is None:
        from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

        _SERDE = JsonPlusSerializer()
    return _SERDE


def _decode_blob(blob: Any, type_name: str = "msgpack") -> Any:
    """解 LangGraph checkpoint 的 blob。

    必须用 LangGraph 自己的 ``JsonPlusSerializer.loads_typed`` ——
    它与写入时是同一个 codec,解出来的东西和图运行时看到的一致。

    ⚠️ 两个坑(都踩过):
    1. ``loads_typed`` 签名是**单参数** ``(type_str, blob)`` 的 tuple,
       不是 ``(data, type)``;传错顺序会静默解出错的东西或直接抛。
    2. ``type`` 是 checkpoints 表里的一列,不同版本可能是 ``msgpack``/
       ``json``,必须从 DB 读出来传进去,别硬编码。

    另外本项目环境没装 ``msgpack`` 这个独立包(LangGraph 走
    ``ormsgpack``),自己 ``import msgpack`` 会 ImportError。
    """
    if blob is None:
        return None
    try:
        return _get_serde().loads_typed((type_name, blob))
    except Exception as e:  # noqa: BLE001 - 单条解不开不该让整个 CLI 崩
        return {"__decode_error__": f"{type(e).__name__}: {e}"}


def _as_dict(msg: Any) -> dict[str, Any] | None:
    """把一条消息转成 plain dict,拿不到就返回 None。

    ⚠️ 坑:反序列化出来的消息是 **LangChain pydantic 对象**
    (``HumanMessage`` / ``AIMessage`` / ``ToolMessage``),**不是 dict**。
    早期版本这里按 ``isinstance(m, dict)`` 过滤,结果 30 条消息被筛成 0 条,
    而 ``stm-dump`` 静默输出"没有含 messages 的 checkpoint"——
    看起来像库是空的,实际是类型判断错了。
    所以统一走 duck-typing:dict 直接用,对象走 ``model_dump()``/``dict()``。
    """
    if isinstance(msg, dict):
        return msg
    for attr in ("model_dump", "dict"):
        fn = getattr(msg, attr, None)
        if callable(fn):
            try:
                out = fn()
                if isinstance(out, dict):
                    return out
            except Exception:  # noqa: BLE001,PERF203 - 逐个降级
                continue
    return None


def _extract_messages(state: Any) -> list[dict[str, Any]]:
    """从 checkpoint state 里挖出 messages 列表(兼容多种形态)。"""
    if not isinstance(state, dict):
        return []
    # LangGraph 的结构:state["channel_values"]["messages"]
    # 但直连 saver 存的老版本可能直接平铺,所以几种形态都兜一下。
    candidates: list[Any] = []
    cv = state.get("channel_values")
    if isinstance(cv, dict):
        candidates.append(cv.get("messages"))
    elif isinstance(cv, list):
        candidates.append(cv)
    candidates.append(state.get("messages"))
    candidates.append(state.get("values"))

    for val in candidates:
        if not isinstance(val, list):
            continue
        out = [d for d in (_as_dict(m) for m in val) if d is not None]
        if out:
            return out
    return []


def _msg_preview(msg: dict[str, Any], width: int) -> str:
    """把一条消息渲染成一行可读文本。"""
    role = (
        msg.get("type")
        or msg.get("role")
        or msg.get("_type")
        or msg.get("class")
        or "?"
    )
    content = msg.get("content", "")
    if isinstance(content, list):
        # 多模态:content 可能是 [{type:text,text:...}, ...]
        parts = []
        for blk in content:
            if isinstance(blk, dict):
                parts.append(str(blk.get("text") or blk.get("type") or ""))
        content = " ".join(p for p in parts if p)
    if not isinstance(content, str):
        content = str(content)
    content = content.replace("\n", " ⏎ ")
    return f"{role}: {content[:width]}"


def _msg_meta(msg: dict[str, Any]) -> str:
    """消息的附加元信息(id / user_id / spoken_at / tool_calls)。"""
    bits = []
    mid = msg.get("id")
    if mid:
        bits.append(f"id={mid}")
    extra = msg.get("additional_kwargs") or {}
    if isinstance(extra, dict):
        if extra.get("user_id"):
            bits.append(f"user={extra['user_id']}")
        if extra.get("spoken_at"):
            bits.append(f"at={str(extra['spoken_at'])[:19]}")
    tc = msg.get("tool_calls")
    if tc:
        bits.append(f"tool_calls={len(tc)}")
    # AI 消息带模型元信息,便于排查"是哪次调用生成的"
    meta = msg.get("response_metadata") or {}
    if isinstance(meta, dict):
        if meta.get("model_name"):
            bits.append(f"model={meta['model_name']}")
        usage = meta.get("token_usage") or meta.get("usage_metadata")
        if isinstance(usage, dict) and usage:
            bits.append(f"tokens={usage.get('total_tokens', '?')}")
    return "  ".join(bits)


def cmd_st_threads(args: argparse.Namespace) -> int:
    """stm-threads: 列出短期记忆里所有 thread + 各自的 checkpoint 数。"""
    conn = _open_stm_db(Path(args.stm_path))
    try:
        rows = conn.execute(
            "SELECT thread_id, checkpoint_ns, COUNT(*) AS n, "
            "       MIN(checkpoint_id), MAX(checkpoint_id) "
            "FROM checkpoints GROUP BY thread_id, checkpoint_ns "
            "ORDER BY n DESC"
        ).fetchall()
        wr = conn.execute(
            "SELECT thread_id, COUNT(*) FROM writes GROUP BY thread_id"
        ).fetchall()
        write_counts = {t: n for t, n in wr}

        total_ck = sum(r[2] for r in rows)
        total_wr = sum(write_counts.values())
        print(f"Short-term memory: {args.stm_path}")
        print(f"  threads(checkpoint groups)={len(rows)}  "
              f"checkpoints={total_ck}  writes={total_wr}\n")
        print(f"  {'thread_id':<44} {'ns':<10} {'ckpt':>6} {'writes':>7}")
        for tid, ns, n, _min, _max in rows:
            print(f"  {tid:<44} {ns or '-':<10} {n:>6} "
                  f"{write_counts.get(tid, 0):>7}")
        if args.detail:
            print("\n  checkpoint_id range (每 thread 最新/最旧):")
            for tid, ns, _n, mn, mx in rows:
                print(f"    {tid:<44} {mn[:24]} … {mx[:24]}")
        return 0
    finally:
        conn.close()


def cmd_st_dump(args: argparse.Namespace) -> int:
    """stm-dump: 全量翻出短期记忆里的对话消息流。"""
    conn = _open_stm_db(Path(args.stm_path))
    try:
        sql = (
            "SELECT thread_id, checkpoint_ns, checkpoint_id, type, checkpoint "
            "FROM checkpoints "
        )
        params: list[Any] = []
        where = []
        if args.thread:
            where.append("thread_id = ?")
            params.append(args.thread)
        if args.checkpoint_ns:
            where.append("checkpoint_ns = ?")
            params.append(args.checkpoint_ns)
        if where:
            sql += "WHERE " + " AND ".join(where) + " "
        # DESC = 最新的在前;LangGraph 的 checkpoint_id 是单调递增的 uuid6
        sql += "ORDER BY rowid DESC LIMIT ?"
        params.append(args.limit)

        rows = conn.execute(sql, params).fetchall()
        if not rows:
            print("No checkpoints matched.")
            return 0

        print(f"Short-term dump: {args.stm_path}")
        print(f"  matched={len(rows)} checkpoint(s)\n")

        shown_msgs = 0
        shown_ck = 0
        for tid, ns, ckid, ctype, blob in rows:
            state = _decode_blob(blob, ctype)
            msgs = _extract_messages(state)
            if not msgs and not args.all_checkpoints:
                continue
            shown_ck += 1
            shown_msgs += len(msgs)
            print(f"── thread={tid}  ns={ns or '-'}  ckpt={ckid[:24]}  "
                  f"msgs={len(msgs)}")
            if args.verbose:
                print(f"   state keys: {sorted(state.keys()) if isinstance(state, dict) else type(state).__name__}")
            for i, m in enumerate(msgs, 1):
                line = _msg_preview(m, args.width)
                meta = _msg_meta(m)
                print(f"   [{i:>2}] {line}")
                if meta:
                    print(f"        {meta}")
            print()

        print(f"\nTotal: {shown_ck} checkpoint(s) shown, {shown_msgs} message(s).")
        if len(rows) >= args.limit and not args.all_checkpoints:
            print(
                f"⚠️  Hit --limit={args.limit} "
                f"(scanned {len(rows)} rows). Raise --limit for more."
            )
        if shown_ck == 0 and not args.all_checkpoints:
            print("💡 没有含 messages 的 checkpoint。试试 --all-checkpoints。")
        return 0
    finally:
        conn.close()


def cmd_st_stats(args: argparse.Namespace) -> int:
    """stm-stats: 短期记忆容量体检(表/thread/消息数)。"""
    import os

    conn = _open_stm_db(Path(args.stm_path))
    try:
        path = Path(args.stm_path)
        size = path.stat().st_size if path.exists() else 0
        n_ck = conn.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0]
        n_wr = conn.execute("SELECT COUNT(*) FROM writes").fetchone()[0]
        n_thr = conn.execute(
            "SELECT COUNT(DISTINCT thread_id) FROM checkpoints"
        ).fetchone()[0]

        print(f"Short-term memory stats: {path}\n")
        print(f"  {'file_size':<26} {size:>12,} bytes  ({size / 1024:.1f} KB)")
        print(f"  {'checkpoints':<26} {n_ck:>12,}")
        print(f"  {'writes':<26} {n_wr:>12,}")
        print(f"  {'distinct_threads':<26} {n_thr:>12,}")

        # 消息总数:逐个解 blob(只统计含 messages 的)
        total_msgs = 0
        ck_with_msgs = 0
        role_counter: dict[str, int] = {}
        for ctype, blob in conn.execute("SELECT type, checkpoint FROM checkpoints"):
            msgs = _extract_messages(_decode_blob(blob, ctype))
            if msgs:
                ck_with_msgs += 1
                total_msgs += len(msgs)
                for m in msgs:
                    r = m.get("type") or m.get("role") or "?"
                    role_counter[r] = role_counter.get(r, 0) + 1

        print(f"  {'checkpoints_with_msgs':<26} {ck_with_msgs:>12,}")
        print(f"  {'messages(total,含历史重复)':<26} {total_msgs:>12,}")
        if role_counter:
            print("\n  by role(注意:每个 checkpoint 含全量历史,故累加值偏大):")
            for r, n in sorted(role_counter.items(), key=lambda kv: -kv[1]):
                print(f"    {r:<20} {n:>6}")

        # 单 thread 最新 checkpoint 的消息数 = 真实对话长度
        print("\n  latest checkpoint per thread(=真实对话长度):")
        for tid, in conn.execute(
            "SELECT DISTINCT thread_id FROM checkpoints"
        ).fetchall():
            row = conn.execute(
                "SELECT type, checkpoint FROM checkpoints "
                "WHERE thread_id = ? ORDER BY rowid DESC LIMIT 1",
                (tid,),
            ).fetchone()
            if row is None:
                continue
            msgs = _extract_messages(_decode_blob(row[1], row[0]))
            print(f"    {tid:<44} {len(msgs):>4} msgs")
        return 0
    finally:
        conn.close()


def _jsonable(obj: Any) -> Any:
    """把任意对象转成 ``json.dumps`` 能吃的结构。

    默认 ``json.dumps(default=str)`` 会把 LangChain 消息整个变成一行 repr
    (``"content='你是谁' id='...'"``),可读性差。递归转一遍:
    消息对象走 ``_as_dict``,其他对象退化到 repr。
    """
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    d = _as_dict(obj)
    if d is not None:
        return _jsonable(d)
    return repr(obj)


def cmd_st_raw(args: argparse.Namespace) -> int:
    """st-raw: 打印某个 checkpoint 的原始 state(排查用,深)。"""
    import json as _json

    conn = _open_stm_db(Path(args.stm_path))
    try:
        row = conn.execute(
            "SELECT thread_id, checkpoint_ns, checkpoint_id, type, checkpoint, metadata "
            "FROM checkpoints WHERE checkpoint_id LIKE ? "
            "ORDER BY rowid DESC LIMIT 1",
            (f"{args.checkpoint_prefix}%",),
        ).fetchone()
        if row is None:
            print(f"No checkpoint matching {args.checkpoint_prefix!r}")
            return 1
        tid, ns, ckid, ctype, blob, meta_blob = row
        print(f"thread_id      : {tid}")
        print(f"checkpoint_ns  : {ns or '-'}")
        print(f"checkpoint_id  : {ckid}")
        print(f"serde type     : {ctype}")
        state = _decode_blob(blob, ctype)
        print("\n--- decoded state ---")
        print(_json.dumps(_jsonable(state), ensure_ascii=False,
                          indent=2)[: args.width])
        if args.verbose:
            print("\n--- metadata ---")
            print(_json.dumps(_jsonable(_decode_blob(meta_blob, ctype)),
                              ensure_ascii=False, indent=2)[: args.width])
        return 0
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="memory_cli",
        description="SmartButler Memory 管理 CLI(Phase 6.3)",
    )
    parser.add_argument(
        "--qdrant-path", default=None, help="Qdrant 嵌入式数据目录"
    )
    parser.add_argument(
        "--qdrant-url", default=None, help="Qdrant 服务模式 URL"
    )
    parser.add_argument(
        "--qdrant-api-key", default=None, help="Qdrant 服务模式 API key"
    )
    parser.add_argument(
        "--collection", default="smartbutler_memory", help="Qdrant collection 名"
    )
    parser.add_argument(
        "--vector-size", type=int, default=1024, help="向量维度"
    )
    parser.add_argument(
        "--archive-path", default="data/deleted_archive.db",
        help="archived SQLite 备份表路径",
    )
    parser.add_argument(
        "--stm-path", default="data/short_term.db",
        help="短期记忆 SQLite 路径(短期记忆子命令用)",
    )
    parser.add_argument(
        "--task-index-path", default="data/task_index.db",
        help="Task 切分索引 SQLite 路径(clear-all --include-task-index 用)",
    )
    subparsers = parser.add_subparsers(dest="cmd", required=True)

    # list-deleted
    p_list = subparsers.add_parser("list-deleted", help="列出软删除的事实")
    p_list.add_argument(
        "--level", default="any",
        choices=["any", "daily", "weekly", "monthly", "permanent"],
    )
    p_list.add_argument("--limit", type=int, default=100)
    p_list.add_argument(
        "--older-than-days", type=int, default=0,
        help="只列软删除时间早于 N 天的(0=全部)",
    )
    p_list.add_argument(
        "--by-age", action="store_true",
        help="按软删除时长分桶显示(<7d / 7-30d / 30-90d / 90-180d / 180-365d / >365d)",
    )
    p_list.set_defaults(func=cmd_list_deleted)

    # age-summary(快查:按软删除时长分桶)
    p_age = subparsers.add_parser(
        "age-summary", help="按软删除时长分桶统计(快速看有多少该清的)",
    )
    p_age.add_argument(
        "--level", default="any",
        choices=["any", "daily", "weekly", "monthly", "permanent"],
    )
    p_age.set_defaults(func=cmd_age_summary)

    # stats
    p_stats = subparsers.add_parser("stats", help="显示容量统计")
    p_stats.set_defaults(func=cmd_stats)

    # vacuum
    p_vac = subparsers.add_parser("vacuum", help="跑一次 vacuum")
    p_vac.add_argument(
        "--dry-run", dest="dry_run", action="store_true", default=True,
        help="(默认) 只统计,不动数据",
    )
    p_vac.add_argument(
        "--no-dry-run", dest="dry_run", action="store_false",
        help="真跑(慎用)",
    )
    p_vac.set_defaults(func=cmd_vacuum)

    # purge(单条)
    p_purge = subparsers.add_parser("purge", help="立即物理删一条(慎用)")
    p_purge.add_argument("--key", required=True, help="要删的 key")
    p_purge.add_argument(
        "--confirm", default="no", choices=["yes", "no"],
        help="必须是 'yes' 才执行",
    )
    p_purge.set_defaults(func=cmd_purge)

    # purge-before(按软删除时长批量删,Qdrant 主体)
    p_pb = subparsers.add_parser(
        "purge-before",
        help="按软删除时长批量物理删 deleted_recoverable(必须 older_than_days >= 7)",
    )
    p_pb.add_argument(
        "--older-than-days", type=int, required=True,
        help="只删 deleted_at < now - N 天的(必须 >= 7,反悔期保护)",
    )
    p_pb.add_argument(
        "--level", default="any",
        choices=["any", "daily", "weekly", "monthly", "permanent"],
    )
    p_pb.add_argument(
        "--confirm", default="no", choices=["yes", "no"],
    )
    p_pb.add_argument(
        "--dry-run", dest="dry_run", action="store_true", default=True,
        help="(默认) 只显示会删哪些,不动数据",
    )
    p_pb.add_argument(
        "--no-dry-run", dest="dry_run", action="store_false",
    )
    p_pb.set_defaults(func=cmd_purge_before)

    # purge-archived(按 archived 时长批量删,SQLite)
    p_pa = subparsers.add_parser(
        "purge-archived",
        help="按 archived 时长批量物理删 archived SQLite(默认 30 天)",
    )
    p_pa.add_argument(
        "--older-than-days", type=int, default=30,
        help="archived_at < now - N 天的全清(默认 30)",
    )
    p_pa.add_argument(
        "--confirm", default="no", choices=["yes", "no"],
    )
    p_pa.add_argument(
        "--dry-run", dest="dry_run", action="store_true", default=True,
    )
    p_pa.add_argument(
        "--no-dry-run", dest="dry_run", action="store_false",
    )
    p_pa.set_defaults(func=cmd_purge_archived)

    # recall(语义检索,Phase 6.2 P0 facade.recall)
    p_rec = subparsers.add_parser(
        "recall",
        help="语义检索长期记忆(facade.recall 包装)",
    )
    p_rec.add_argument(
        "query", help="检索 query 字符串(中文/英文都可)",
    )
    p_rec.add_argument(
        "--k", type=int, default=5,
        help="最多返回几条(默认 5)",
    )
    p_rec.add_argument(
        "--min-importance", type=float, default=0.3,
        help="重要性阈值,低于此分的会被过滤(默认 0.3)",
    )
    p_rec.add_argument(
        "--source", default=None,
        help="可选,只召回指定 source 的记忆(ctx 过滤)",
    )
    p_rec.add_argument(
        "--min-score", type=float, default=0.55,
        help="相似度下限(归一化 0-1),低于此分丢弃(默认 0.55,设 0 关闭)",
    )
    p_rec.set_defaults(func=cmd_recall)

    # consolidate(3 层聚合管道)
    p_con = subparsers.add_parser(
        "consolidate",
        help="把一个时间段的 fact 聚合成 1 条摘要(daily/weekly/monthly)",
    )
    p_con.add_argument(
        "--level", default="daily",
        choices=["daily", "weekly", "monthly"],
        help=(
            "目标层。daily=汇总昨天的 fact 成 1 条日报;"
            "weekly=汇总最近 7 天的 daily 成 1 条周报;"
            "monthly=汇总最近 30 天的 weekly 成 1 条月报"
        ),
    )
    p_con.add_argument(
        "--dry-run", dest="dry_run", action="store_true", default=False,
        help="只统计候选数与时间窗,不实际写摘要",
    )
    p_con.add_argument(
        "--no-llm", dest="no_llm", action="store_true", default=False,
        help="不调 LLM,摘要退化为拼接兜底(对比效果 / 省 token)",
    )
    p_con.set_defaults(func=cmd_consolidate)

    # dump(全量翻页看库里有什么)
    p_dump = subparsers.add_parser(
        "dump",
        help="全量翻出 Qdrant 里的 point(看库里到底存了什么)",
    )
    p_dump.add_argument(
        "--level", default="any",
        choices=["any", "daily", "weekly", "monthly", "permanent"],
        help="只列某个 level",
    )
    p_dump.add_argument("--tag", default=None, help="只列含此 tag 的")
    p_dump.add_argument(
        "--min-importance", type=float, default=0.0,
        help="只列 importance 不低于此值的",
    )
    p_dump.add_argument(
        "--no-deleted", action="store_true",
        help="排除软删除条目(默认含 soft-deleted,便于审计)",
    )
    p_dump.add_argument(
        "--limit", type=int, default=20, help="最多列几条(默认 20)",
    )
    p_dump.add_argument(
        "--all", action="store_true", help="翻完所有页(不分页截断)",
    )
    p_dump.add_argument(
        "--width", type=int, default=200, help="content 截断宽度(默认 200)",
    )
    p_dump.add_argument(
        "--page-size", type=int, default=256, help="每页 scroll 条数(内部用)",
    )
    p_dump.set_defaults(func=cmd_dump)

    # dump-fields(payload 字段分布体检)
    p_df = subparsers.add_parser(
        "dump-fields",
        help="汇总 payload 字段分布(总量/删除数/level/tag 分布)",
    )
    p_df.add_argument(
        "--page-size", type=int, default=256, help="每页 scroll 条数(内部用)",
    )
    p_df.set_defaults(func=cmd_dump_fields)

    # ===== 短期记忆(LangGraph checkpoint / short_term.db)=====

    # stm-threads
    p_stt = subparsers.add_parser(
        "stm-threads",
        help="列出短期记忆里的所有 thread + checkpoint 数",
    )
    p_stt.add_argument(
        "--detail", action="store_true",
        help="额外显示每个 thread 的 checkpoint_id 范围",
    )
    p_stt.set_defaults(func=cmd_st_threads)

    # stm-dump(全量看消息流)
    p_std = subparsers.add_parser(
        "stm-dump",
        help="全量翻出短期记忆里的对话消息流(按 checkpoint)",
    )
    p_std.add_argument("--thread", default=None, help="只看这个 thread_id")
    p_std.add_argument(
        "--checkpoint-ns", default=None, help="只看这个 checkpoint_ns",
    )
    p_std.add_argument(
        "--limit", type=int, default=10, help="最多看几个 checkpoint(默认 10)",
    )
    p_std.add_argument(
        "--all-checkpoints", action="store_true",
        help="连不含 messages 的中间步一起显示(默认跳过)",
    )
    p_std.add_argument(
        "--width", type=int, default=120, help="content 截断宽度(默认 120)",
    )
    p_std.add_argument(
        "--verbose", "-v", action="store_true",
        help="额外打印 state 的字段名",
    )
    p_std.set_defaults(func=cmd_st_dump)

    # stm-stats
    p_sts = subparsers.add_parser(
        "stm-stats",
        help="短期记忆容量体检(表大小/thread 数/消息数)",
    )
    p_sts.set_defaults(func=cmd_st_stats)

    # st-raw
    p_str = subparsers.add_parser(
        "st-raw",
        help="打印某个 checkpoint 的原始 state JSON(深排查)",
    )
    p_str.add_argument(
        "checkpoint_prefix", help="checkpoint_id 前缀(定位用)",
    )
    p_str.add_argument(
        "--width", type=int, default=4000, help="输出截断宽度(默认 4000)",
    )
    p_str.add_argument(
        "--verbose", "-v", action="store_true", help="连 metadata 一起打",
    )
    p_str.set_defaults(func=cmd_st_raw)

    # clear-all(危险:一键清空所有长短期记忆)
    p_clear = subparsers.add_parser(
        "clear-all",
        help="⚠️  一键清空所有长短期记忆(长期 active+soft-deleted+archived,短期全部)",
    )
    p_clear.add_argument(
        "--confirm", default="no", choices=["yes", "no"],
        help="必须是 'yes' 才执行(第一道确认)",
    )
    p_clear.add_argument(
        "--yes-i-know-this-deletes-everything",
        dest="yes_i_know", action="store_true", default=False,
        help="显式确认(第二道防线,防止误操作)",
    )
    p_clear.add_argument(
        "--dry-run", dest="dry_run", action="store_true", default=True,
        help="(默认) 只显示会清什么,不动数据",
    )
    p_clear.add_argument(
        "--no-dry-run", dest="dry_run", action="store_false",
        help="真跑(慎用!清空后无法恢复)",
    )
    p_clear.add_argument(
        "--include-task-index", dest="include_task_index",
        action="store_true", default=False,
        help=(
            "额外清空 data/task_index.db(6.5 task scheduler 状态:"
            "tasks / task_messages / _processed_cursor)。"
            "默认不动,需显式 opt-in"
        ),
    )
    p_clear.set_defaults(func=cmd_clear_all)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
