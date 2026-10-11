"""summarizer.py + archive_store.py + task_boundary.py + consolidation.py 集成测试(Phase 6.3 P1)。"""

from __future__ import annotations

from datetime import datetime, UTC, timedelta
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
    c = Consolidator(
        store=s,
        embedder=embedder,
        extractor=FiveW1HExtractor(),
        summarizer=HeuristicSummarizer(),
        llm_scorer=__import__("smartbutler.emotion.memory.summarizer", fromlist=["LLMScorer"]).LLMScorer(),
        llm_summarizer=LLMSummarizer(),
        hooks=hooks,
    )
    return c, s, hooks


def test_consolidator_daily_promotes_unlevel(
    consolidator_setup: tuple[Consolidator, LongTermStore, MemoryHooks],
) -> None:
    """daily 触发:无 level 的 fact 升 L1。"""
    import asyncio
    c, store, _ = consolidator_setup
    emb = asyncio.run(StubEmbedder(dimension=64).embed("重要任务"))
    store.store_memory("k1", emb, "明天 8 点开会讨论项目", importance=0.7)
    report = asyncio.run(c.consolidate(level=ConsolidationLevel.DAILY))
    assert report.promoted == 1
    # 源 fact 被软删
    pl = store.get_payload("k1")
    assert pl is not None and pl.get("deleted") is True
    # 新 fact 有 level:daily
    new = [it for it in store.list_keys_by_level(include_deleted=False) if it["key"].startswith("promoted:daily:")]
    assert len(new) == 1
    assert new[0]["level"] == "daily"


def test_consolidator_skips_low_importance(
    consolidator_setup: tuple[Consolidator, LongTermStore, MemoryHooks],
) -> None:
    """importance < 0.2 → skipped_low_importance。"""
    import asyncio
    c, store, _ = consolidator_setup
    emb = asyncio.run(StubEmbedder(dimension=64).embed("琐事"))
    store.store_memory("lo", emb, "开个灯", importance=0.1)
    report = asyncio.run(c.consolidate(level=ConsolidationLevel.DAILY))
    assert report.promoted == 0
    assert report.skipped_low_importance == 1


def test_consolidator_weekly_promotes_old_daily(
    consolidator_setup: tuple[Consolidator, LongTermStore, MemoryHooks],
) -> None:
    """weekly 触发:L1 daily 且 age > 7 天 → 升 L2,软删 L1。"""
    import asyncio
    c, store, _ = consolidator_setup
    emb = asyncio.run(StubEmbedder(dimension=64).embed("8点烧水"))
    old = (datetime.now(UTC) - timedelta(days=10)).isoformat()
    store.store_memory(
        "old_daily", emb, "明早 8 点烧洗澡水",
        importance=0.6, level="daily",
        created_at=datetime.fromisoformat(old),
    )
    report = asyncio.run(c.consolidate(level=ConsolidationLevel.WEEKLY))
    assert report.promoted == 1
    # old_daily 被软删
    pl = store.get_payload("old_daily")
    assert pl is not None and pl.get("deleted") is True
    # 新 fact level:weekly
    new = [it for it in store.list_keys_by_level(include_deleted=False) if it["key"].startswith("promoted:weekly:")]
    assert len(new) == 1


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
    store.store_memory("k1", emb, "明天 8 点要烧水", importance=0.6)
    asyncio.run(c.consolidate(level=ConsolidationLevel.DAILY))
    assert len(seen) == 1
    assert seen[0].startswith("promoted:daily:")
