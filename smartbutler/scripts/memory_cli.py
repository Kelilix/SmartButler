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
    """consolidate: 跑一次 4 层升格管道(Phase 6.3 P1)。

    走底层 :class:`Consolidator`(不绕 facade),所以 CLI 是独立可用,
    不依赖 facade 是否已 bind consolidator。
    """
    from smartbutler.emotion.memory.embedder import build_default_embedder
    from smartbutler.emotion.memory.levels import (
        DAILY_TO_WEEKLY_DAYS,
        MONTHLY_TO_PERMANENT_DAYS,
        WEEKLY_TO_MONTHLY_DAYS,
    )

    level = args.level
    dry_run = args.dry_run
    store = _build_store(args)
    embedder = build_default_embedder()
    hooks = MemoryHooks()
    c = Consolidator(
        store=store,
        embedder=embedder,
        extractor=FiveW1HExtractor(),
        summarizer=HeuristicSummarizer(),
        llm_scorer=LLMScorer(),
        llm_summarizer=LLMSummarizer(),
        hooks=hooks,
    )
    if dry_run:
        # dry-run:只统计候选数,不动数据
        from smartbutler.emotion.memory.levels import MemoryLevel
        from smartbutler.emotion.memory.consolidation import ConsolidationLevel
        if level == "daily":
            cand = c._scan_raw_or_unlevel(limit=10_000)
        elif level == "weekly":
            from datetime import datetime, UTC
            cand = c._scan_level(
                MemoryLevel.DAILY,
                older_than_days=DAILY_TO_WEEKLY_DAYS,
                now=datetime.now(UTC),
                limit=10_000,
            )
        else:  # monthly
            from datetime import datetime, UTC
            cand = c._scan_level(
                MemoryLevel.WEEKLY,
                older_than_days=WEEKLY_TO_MONTHLY_DAYS,
                now=datetime.now(UTC),
                limit=10_000,
            )
        print(f"\n🔍 DRY RUN consolidate --level {level}")
        print(f"  candidates_scanned   {len(cand):>6} 条")
        # 按 importance 分级预估
        low = sum(1 for it in cand if it.get("importance", 0.5) < 0.2)
        l1_only = sum(1 for it in cand if 0.2 <= it.get("importance", 0.5) < 0.5)
        l1_l2 = sum(1 for it in cand if 0.5 <= it.get("importance", 0.5) < 0.8)
        l1_l2_l3 = sum(1 for it in cand if it.get("importance", 0.5) >= 0.8)
        print(f"  → importance<0.2 跳过:    {low:>4} 条(走 TTL)")
        print(f"  → L1 daily only:          {l1_only:>4} 条")
        print(f"  → L1+L2:                 {l1_l2:>4} 条")
        print(f"  → L1+L2+L3 permanent:    {l1_l2_l3:>4} 条")
        if level == "daily":
            print("\n  💡 配合阈值(当前 qdrant 是 0 条)建议先用 /remember 写一些 fact 再跑。")
        return 0
    # 真跑
    report = asyncio.run(c.consolidate(level=level))
    print(f"\n✅ Consolidate done (level={level})")
    print(f"  scanned              {report.candidates_scanned:>6} 条")
    print(f"  promoted             {report.promoted:>6} 条(升格成功)")
    print(f"  forgotten            {report.forgotten:>6} 条(源 fact 软删)")
    print(f"  skipped_low_importance{report.skipped_low_importance:>5} 条(<0.2)")
    print(f"  skipped_invalid_5w1h {report.skipped_invalid_5w1h:>6} 条(5W1H 不完整)")
    if report.errors:
        print(f"  errors               {len(report.errors):>6} 条(见 logs)")
    if report.new_keys:
        print(f"\n  new_keys(前 5):")
        for nk in report.new_keys[:5]:
            print(f"    - {nk}")
    return 0


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

    # consolidate(4 层升格管道)
    p_con = subparsers.add_parser(
        "consolidate",
        help="跑一次 4 层升格管道(daily/weekly/monthly,Phase 6.3 P1)",
    )
    p_con.add_argument(
        "--level", default="daily",
        choices=["daily", "weekly", "monthly"],
        help="目标层(daily=升 L1;weekly=7d+ daily→L2;monthly=30d+ weekly→L3)",
    )
    p_con.add_argument(
        "--dry-run", dest="dry_run", action="store_true", default=False,
        help="只统计候选数,不实际写新条目/软删源条目",
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

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
