"""summarizer.py + archive_store.py + task_boundary.py + consolidation.py 集成测试(Phase 6.3 P1)。"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from smartbutler.capabilities.embedding import StubEmbedder
from smartbutler.emotion.memory.archive_store import ArchiveStore, ArchivedMemory
from smartbutler.emotion.memory.consolidation import (
    Consolidator,
    ConsolidationLevel,
)
from smartbutler.emotion.memory.hooks import MemoryHooks
from smartbutler.emotion.memory.levels import MemoryLevel
from smartbutler.emotion.memory.long_term import LongTermStore
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
from smartbutler.storage import QdrantStorage


# ------------------- FiveW1HExtractor -------------------

def test_extractor_basic_what() -> None:
    """what 必填,取首句或截断。"""
    ex = FiveW1HExtractor()
    fw = ex.extract("我要买咖啡")
    assert fw.what
    assert "咖啡" in fw.what


def test_extractor_extracts_time() -> None:
    """when: 抽日期/时间模式。"""
    ex = FiveW1HExtractor()
    fw = ex.extract("明天 8 点我要开会")
    assert "明天" in fw.when or "8" in fw.when


def test_extractor_extracts_quote_as_why() -> None:
    """why: 抽原话引述。"""
    ex = FiveW1HExtractor()
    fw = ex.extract('用户说:"我最近很焦虑"')
    assert "焦虑" in fw.why


def test_extractor_empty_returns_invalid() -> None:
    """空内容 → is_valid()=False。"""
    ex = FiveW1HExtractor()
    fw = ex.extract("")
    assert not fw.is_valid()


def test_extractor_where_extracts_location() -> None:
    """where: 抽"在XXX 开会/住"等模式。"""
    ex = FiveW1HExtractor()
    fw = ex.extract("我在上海住")
    # 启发式抽地点
    assert fw.where or not fw.is_valid()  # 启发式可能抽不到,只 smoke test


# ------------------- LLMSummarizer.validate_why -------------------

def test_llm_validate_why_drops_inference() -> None:
    """含推断语的 why 字段被丢弃(返空)。"""
    s = LLMSummarizer()
    assert s.validate_why("用户可能因为压力大") == ""
    assert s.validate_why("他大概是喜欢这个") == ""


def test_llm_validate_why_keeps_direct_quote() -> None:
    """直接引述保留。"""
    s = LLMSummarizer()
    assert s.validate_why("我最近很焦虑") == "我最近很焦虑"


def test_llm_validate_why_empty() -> None:
    """空 why → 返空。"""
    s = LLMSummarizer()
    assert s.validate_why("") == ""


# ------------------- HeuristicSummarizer -------------------

def test_heuristic_summarizer_dedup() -> None:
    """summarize_text 去重。"""
    s = HeuristicSummarizer()
    out = s.summarize_text(["内容A", "内容A", "内容B"])
    assert "- 内容A" in out
    assert "- 内容B" in out
    assert out.count("- 内容A") == 1


# ------------------- TaskBoundaryDetector -------------------

def test_task_boundary_first_turn_is_boundary() -> None:
    """第一条消息永远是 task 起点。"""
    det = TaskBoundaryDetector(threshold_minutes=30)
    turn = TurnInfo(role="user", content="hi", timestamp=datetime.now(UTC))
    r = det.detect(prev_turn=None, current_turn=turn)
    assert r.is_boundary is True
    assert r.reason == "none"


def test_task_boundary_interval_triggers() -> None:
    """消息间隔 > 30 分钟 → 新 task。"""
    det = TaskBoundaryDetector(threshold_minutes=30)
    now = datetime.now(UTC)
    prev = TurnInfo(role="user", content="hi", timestamp=now)
    cur = TurnInfo(role="user", content="new task", timestamp=now + timedelta(minutes=45))
    r = det.detect(prev_turn=prev, current_turn=cur)
    assert r.is_boundary is True
    assert r.reason == "interval"


def test_task_boundary_trigger_phrase() -> None:
    """触发词"另外"等 → 新 task。"""
    det = TaskBoundaryDetector(threshold_minutes=30)
    now = datetime.now(UTC)
    prev = TurnInfo(role="user", content="帮我查天气", timestamp=now)
    cur = TurnInfo(role="user", content="另外,帮我订外卖", timestamp=now + timedelta(seconds=10))
    r = det.detect(prev_turn=prev, current_turn=cur)
    assert r.is_boundary is True
    assert r.reason == "trigger"


def test_task_boundary_no_boundary_within_window() -> None:
    """30 分钟内 + 无触发词 → 不是新 task。"""
    det = TaskBoundaryDetector(threshold_minutes=30)
    now = datetime.now(UTC)
    prev = TurnInfo(role="user", content="hi", timestamp=now)
    cur = TurnInfo(role="user", content="好的", timestamp=now + timedelta(minutes=5))
    r = det.detect(prev_turn=prev, current_turn=cur)
    assert r.is_boundary is False


def test_task_boundary_butler_done_marks_new_task() -> None:
    """管家说"完成"后用户首次发言 → 新 task。"""
    det = TaskBoundaryDetector(threshold_minutes=30)
    now = datetime.now(UTC)
    prev = TurnInfo(role="assistant", content="任务完成,还有别的吗?", timestamp=now)
    cur = TurnInfo(role="user", content="帮我订机票", timestamp=now + timedelta(seconds=5))
    r = det.detect(prev_turn=prev, current_turn=cur)
    assert r.is_boundary is True
    assert r.reason == "butler_done"


def test_task_boundary_invalid_threshold() -> None:
    """threshold <= 0 抛错。"""
    with pytest.raises(ValueError):
        TaskBoundaryDetector(threshold_minutes=0)
    with pytest.raises(ValueError):
        TaskBoundaryDetector(threshold_minutes=-1)


# ------------------- ArchiveStore -------------------

def test_archive_store_add_get_remove(tmp_path: Path) -> None:
    """ArchiveStore 增删查 smoke test。"""
    path = tmp_path / "arc.db"
    store = ArchiveStore(path)
    item = ArchivedMemory(
        key="k1", content="c1", importance=0.7, level="daily",
        tags=["a"], created_at="2026-01-01T00:00:00+00:00",
        deleted_at="2026-01-02T00:00:00+00:00",
        deleted_reason="ttl",
    )
    store.add(item)
    got = store.get("k1")
    assert got is not None
    assert got.key == "k1"
    assert got.content == "c1"
    assert store.count() == 1
    assert store.remove("k1") is True
    assert store.get("k1") is None
    store.close()


def test_archive_store_idempotent_add(tmp_path: Path) -> None:
    """同 key 多次 add → 覆盖不报错。"""
    path = tmp_path / "arc.db"
    store = ArchiveStore(path)
    store.add(ArchivedMemory(key="k", content="v1", importance=0.5))
    store.add(ArchivedMemory(key="k", content="v2", importance=0.5))
    assert store.count() == 1
    assert store.get("k").content == "v2"
    store.close()


def test_archive_store_list_older_than(tmp_path: Path) -> None:
    """list_all(older_than_days=N) 过滤。"""
    path = tmp_path / "arc.db"
    store = ArchiveStore(path)
    # 加 3 条,人为改 archived_at
    for i, days_ago in enumerate([1, 10, 40]):
        item = ArchivedMemory(
            key=f"k{i}", content=f"c{i}", importance=0.5,
            archived_at=(datetime.now(UTC) - timedelta(days=days_ago)).isoformat(),
        )
        store.add(item)
    # 查 older_than_days=20 → 应只返 1 条(40 天前)
    items = store.list_all(older_than_days=20)
    assert len(items) == 1
    assert items[0].key == "k2"
    store.close()


# ------------------- Consolidator 端到端 -------------------

@pytest.fixture
def consolidator_setup(tmp_path: Path) -> tuple[Consolidator, LongTermStore, MemoryHooks]:
    q = QdrantStorage(path=tmp_path / "q", collection="t", vector_size=64)
    s = LongTermStore(q)
    embedder = StubEmbedder(dimension=64)
    hooks = MemoryHooks()
    # llm=None → 评分走启发式、摘要走拼接兜底(测试不该依赖真实 LLM)
    c = Consolidator(
        store=s,
        embedder=embedder,
        llm_scorer=LLMScorer(None),
        llm_summarizer=LLMSummarizer(None),
        hooks=hooks,
    )
    return c, s, hooks


def _yesterday(now: datetime | None = None) -> datetime:
    """本地昨日 12:00 转 UTC(daily 汇总窗口用)。

    🆕 2026-10-12:配合 Consolidator 锁死 [本地昨日 00:00, 本地今日 00:00)。
    用 12:00 而不是 now-1d,确保无论何时跑都稳稳落在昨日整天里。
    """
    from zoneinfo import ZoneInfo
    tz = ZoneInfo("Asia/Shanghai")
    base = now.astimezone(tz) if now and now.tzinfo else (
        (now or datetime.now(UTC)).astimezone(tz)
    )
    yesterday_noon_local = (base - timedelta(days=1)).replace(
        hour=12, minute=0, second=0, microsecond=0,
    )
    return yesterday_noon_local.astimezone(UTC)


def test_consolidator_daily_aggregates_into_one(
    consolidator_setup: tuple[Consolidator, LongTermStore, MemoryHooks],
) -> None:
    """🆕 daily:N 条 fact → **1 条**日报(旧实现是 N 条 daily)。"""
    import asyncio
    c, store, _ = consolidator_setup
    emb = asyncio.run(StubEmbedder(dimension=64).embed("x"))
    y = _yesterday()
    for i in range(5):
        store.store_memory(
            f"k{i}", emb, f"第{i}条记忆:讨论项目进度",
            importance=0.7, created_at=y,
        )
    report = asyncio.run(c.consolidate(level=ConsolidationLevel.DAILY))
    # 只产出 1 条
    assert report.promoted == 1
    assert report.source_count == 5
    assert report.compression_ratio == 5.0
    new = [it for it in store.list_keys_by_level(include_deleted=False)
           if it["key"].startswith("consolidated:daily:")]
    assert len(new) == 1
    assert new[0]["level"] == "daily"


def test_consolidator_daily_keeps_source_facts(
    consolidator_setup: tuple[Consolidator, LongTermStore, MemoryHooks],
) -> None:
    """🆕 源 fact **不删**(用户决策 2026-10-12):留给 forget 按时间窗处理。

    旧行为是升格即软删源 fact —— 那样"第 4 个月删第 1 个月的 fact"
    就无从谈起(源 fact 在升格当天就没了)。
    """
    import asyncio
    c, store, _ = consolidator_setup
    emb = asyncio.run(StubEmbedder(dimension=64).embed("重要任务"))
    store.store_memory(
        "k1", emb, "明天 8 点开会讨论项目",
        importance=0.7, created_at=_yesterday(),
    )
    asyncio.run(c.consolidate(level=ConsolidationLevel.DAILY))
    pl = store.get_payload("k1")
    assert pl is not None
    assert not pl.get("deleted"), "源 fact 不该被升格时软删"
    assert "level" not in pl, "源 fact 不该被打 level tag"


def test_consolidator_skips_low_importance(
    consolidator_setup: tuple[Consolidator, LongTermStore, MemoryHooks],
) -> None:
    """importance < 0.2 → skipped_low_importance。"""
    import asyncio
    c, store, _ = consolidator_setup
    emb = asyncio.run(StubEmbedder(dimension=64).embed("琐事"))
    store.store_memory("lo", emb, "开个灯", importance=0.1, created_at=_yesterday())
    report = asyncio.run(c.consolidate(level=ConsolidationLevel.DAILY))
    assert report.promoted == 0
    assert report.skipped_low_importance == 1


def test_consolidator_weekly_aggregates_daily(
    consolidator_setup: tuple[Consolidator, LongTermStore, MemoryHooks],
) -> None:
    """🆕 weekly:N 条 daily → **1 条**周报(旧实现是 N 条 weekly)。"""
    import asyncio
    from zoneinfo import ZoneInfo
    c, store, _ = consolidator_setup
    emb = asyncio.run(StubEmbedder(dimension=64).embed("8点烧水"))
    # 过去 3 天的本地 12:00 → 一定落在 [本地今日 00:00 - 7d, 本地今日 00:00)
    tz = ZoneInfo("Asia/Shanghai")
    base_local = datetime.now(UTC).astimezone(tz)
    for i in range(4):
        ts_local = (base_local - timedelta(days=i + 1)).replace(
            hour=12, minute=0, second=0, microsecond=0,
        )
        store.store_memory(
            f"d{i}", emb, f"第{i}天的日报内容",
            importance=0.6, level="daily", created_at=ts_local.astimezone(UTC),
        )
    report = asyncio.run(c.consolidate(level=ConsolidationLevel.WEEKLY))
    assert report.promoted == 1
    assert report.source_count == 4
    new = [it for it in store.list_keys_by_level(include_deleted=False)
           if it["key"].startswith("consolidated:weekly:")]
    assert len(new) == 1
    # 源 daily 也保留
    assert store.get_payload("d0") is not None


def test_consolidator_triggers_on_promote_hook(
    consolidator_setup: tuple[Consolidator, LongTermStore, MemoryHooks],
) -> None:
    """consolidate 触发 ON_PROMOTE 钩子。"""
    import asyncio
    from smartbutler.emotion.memory.hooks import MemoryEventType
    c, store, hooks = consolidator_setup
    seen: list[str] = []
    def cb(*, key: str, source_keys: list[str]) -> None:  # noqa: ARG001
        seen.append(key)
    hooks.register(MemoryEventType.ON_PROMOTE, cb)
    emb = asyncio.run(StubEmbedder(dimension=64).embed("8点烧水"))
    store.store_memory(
        "k1", emb, "明天 8 点要烧水", importance=0.6, created_at=_yesterday(),
    )
    asyncio.run(c.consolidate(level=ConsolidationLevel.DAILY))
    assert len(seen) == 1
    assert seen[0].startswith("consolidated:daily:")


# ------------------- 时区 / 窗口边界回归(2026-10-12) -------------------

def test_resolve_window_daily_uses_local_day(
    consolidator_setup: tuple[Consolidator, LongTermStore, MemoryHooks],
) -> None:
    """daily 窗口 = [本地昨日 00:00, 本地今日 00:00),锁死不漂移。

    关键性质:
    - 标签日期是**本地**日期(不是 UTC)
    - 凌晨 3 点跑(now=本地 03:00)跟上午 10 点跑(now=本地 10:00)窗口一致
    - 不会因"几点跑"误把今天的 fact 算进昨日
    """
    from smartbutler.emotion.memory.consolidation import LOCAL_TZ
    c, _, _ = consolidator_setup
    # 本地 03:00 跑
    now_local_3am = datetime(2026, 10, 12, 3, 0, 0, tzinfo=LOCAL_TZ)
    w = c._resolve_window(ConsolidationLevel.DAILY, now_local_3am.astimezone(UTC))
    # since 必须是本地昨日 00:00
    assert w["since"].astimezone(LOCAL_TZ).hour == 0
    assert w["since"].astimezone(LOCAL_TZ).minute == 0
    assert w["since"].astimezone(LOCAL_TZ).date() == date(2026, 10, 11)
    # until 必须是本地今日 00:00(锁死,不是 now)
    assert w["until"].astimezone(LOCAL_TZ).hour == 0
    assert w["until"].astimezone(LOCAL_TZ).minute == 0
    assert w["until"].astimezone(LOCAL_TZ).date() == date(2026, 10, 12)
    # 标签用本地日期
    assert "2026-10-11" in w["label"] and "2026-10-12" in w["label"]

    # 本地 10:00 跑 → 窗口相同
    now_local_10am = datetime(2026, 10, 12, 10, 0, 0, tzinfo=LOCAL_TZ)
    w2 = c._resolve_window(ConsolidationLevel.DAILY, now_local_10am.astimezone(UTC))
    assert w2["since"] == w["since"]
    assert w2["until"] == w["until"]


def test_consolidator_daily_excludes_today_early_morning(
    consolidator_setup: tuple[Consolidator, LongTermStore, MemoryHooks],
) -> None:
    """凌晨 3 点跑,今天 0-3 点写的 fact 不应被当作昨日纳入。

    模拟场景:北京 10-12 凌晨 2:00 用户写了 1 条 fact(应该归"今天"),
    凌晨 3:00 schedule 触发 daily 跑 → 不应纳入。
    """
    import asyncio
    from zoneinfo import ZoneInfo
    from smartbutler.emotion.memory.consolidation import LOCAL_TZ
    c, store, _ = consolidator_setup
    emb = asyncio.run(StubEmbedder(dimension=64).embed("x"))

    # 北京 10-12 凌晨 2:00 写 1 条 → UTC 10-11 18:00
    tz = ZoneInfo("Asia/Shanghai")
    today_2am_local = datetime(2026, 10, 12, 2, 0, 0, tzinfo=tz)
    store.store_memory(
        "today_2am", emb, "今天凌晨 2 点的 fact",
        importance=0.7, created_at=today_2am_local.astimezone(UTC),
    )
    # 北京 10-12 凌晨 3:00 跑 consolidate(模拟 schedule 触发时刻)
    now_local_3am = datetime(2026, 10, 12, 3, 0, 0, tzinfo=tz)
    report = asyncio.run(
        c.consolidate(level=ConsolidationLevel.DAILY, now=now_local_3am.astimezone(UTC))
    )
    # today_2am 是今天 0-3 点的 fact,不该被纳入"昨日"
    assert report.candidates_scanned == 0
    assert report.promoted == 0


def test_consolidator_daily_includes_yesterday_all_day(
    consolidator_setup: tuple[Consolidator, LongTermStore, MemoryHooks],
) -> None:
    """本地昨日全天 00:00-24:00 写的 fact 都应纳入。

    包括:本地 10-11 00:00、10-11 12:00、10-11 23:59
    """
    import asyncio
    from zoneinfo import ZoneInfo
    c, store, _ = consolidator_setup
    emb = asyncio.run(StubEmbedder(dimension=64).embed("x"))
    tz = ZoneInfo("Asia/Shanghai")
    # 昨日 3 个时刻
    moments_local = [
        datetime(2026, 10, 11, 0, 0, 0, tzinfo=tz),    # 00:00 整
        datetime(2026, 10, 11, 12, 0, 0, tzinfo=tz),   # 12:00
        datetime(2026, 10, 11, 23, 59, 59, tzinfo=tz), # 23:59:59
    ]
    for i, m in enumerate(moments_local):
        store.store_memory(
            f"y{i}", emb, f"昨日 {m.strftime('%H:%M')} 的 fact",
            importance=0.7, created_at=m.astimezone(UTC),
        )
    # 10-12 凌晨 3:00 跑
    now_local = datetime(2026, 10, 12, 3, 0, 0, tzinfo=tz)
    report = asyncio.run(
        c.consolidate(level=ConsolidationLevel.DAILY, now=now_local.astimezone(UTC))
    )
    assert report.candidates_scanned == 3, (
        f"昨日整天 3 条 fact 应全部纳入,实扫 {report.candidates_scanned}"
    )
