"""🆕 2026-11-04: 「日汇总重复压缩」bug 回归测试。

## Bug 描述

``Consolidator._scan()`` 在 DAILY 层级传 ``source_level=None``,
本意是"扫**没有 level 字段**的原始 fact"(auto_extract 写的)。

但 ``LongTermStore.list_keys_by_level(level=None)`` 的实现是
「must 为空 → **不过滤 → 滚动全表**」—— 把"无 level 字段"错误地
等同于"全表"。

于是**上一轮生成的日报/周报/月报本身**被当成源 fact 再压一遍:

    D+1 03:00 生成日报 R1(R1.created_at = D+1 03:00)
    D+2 03:00 生成日报 R2,窗口 = [D+1 00:00, D+2 00:00)
       → R1 的 created_at 落在窗口内 → R1 被当成源 fact
    D+3 ... 日复一日,越滚越大

而且 R1 已带 ``level:daily`` / ``user:X`` / ``task:Y`` tag,
再压一层会**重写归属**,把"日报里按项目分段的结构"压成扁平摘要。

## 修法

daily 层级显式要求"payload 里**没有 level 字段**"
(服务端 ``IsEmptyCondition`` + 客户端 ``has_level`` 标志双保险)。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

from smartbutler.emotion.memory.consolidation import Consolidator
from smartbutler.emotion.memory.levels import task_tag, user_tag


class _Emb:
    async def embed(self, text: str) -> list[float]:
        return [0.0] * 1024


def _store_returning(items: list[dict[str, Any]]) -> MagicMock:
    store = MagicMock()
    store.store_memory = MagicMock(return_value=None)
    store.list_keys_by_level = MagicMock(return_value=items)
    return store


# ---------------------------------------------------------------------------
# 1. _scan 层:daily 不该扫到带 level 的条目
# ---------------------------------------------------------------------------


def test_scan_daily_excludes_leveled_items() -> None:
    """daily 扫描排除带 level 字段的条目(升格产物)。"""
    store = _store_returning([
        {
            "key": "raw_fact",
            "content": "原始 fact",
            "importance": 0.8,
            "created_at": "2026-11-01T10:00:00+00:00",
            "level": "",
            "has_level": False,
            "tags": [user_tag("u_dad"), task_tag("t1")],
        },
        {
            "key": "yesterday_daily",
            "content": "【日报】昨天的日报",
            "importance": 0.8,
            "created_at": "2026-11-02T03:00:00+00:00",
            "level": "daily",
            "has_level": True,
            "tags": [user_tag("u_dad"), task_tag("t1"), "level:daily"],
        },
    ])
    c = Consolidator(store=store, embedder=_Emb())  # type: ignore[arg-type]

    since = datetime(2026, 11, 1, 0, 0, tzinfo=UTC)
    now = datetime(2026, 11, 3, 0, 0, tzinfo=UTC)
    out = c._scan(level=None, since=since, now=now, limit=100)

    keys = [o["key"] for o in out]
    assert "raw_fact" in keys, "原始 fact 必须被扫到"
    assert "yesterday_daily" not in keys, "升格产物不能被当成源 fact"


def test_scan_daily_excludes_all_leveled_items() -> None:
    """daily/weekly/monthly 三种升格产物都要排除。"""
    store = _store_returning([
        {
            "key": "d",
            "content": "日报",
            "importance": 0.8,
            "created_at": "2026-11-02T03:00:00+00:00",
            "level": "daily",
            "has_level": True,
            "tags": [],
        },
        {
            "key": "w",
            "content": "周报",
            "importance": 0.8,
            "created_at": "2026-11-02T04:00:00+00:00",
            "level": "weekly",
            "has_level": True,
            "tags": [],
        },
        {
            "key": "m",
            "content": "月报",
            "importance": 0.8,
            "created_at": "2026-11-02T05:00:00+00:00",
            "level": "monthly",
            "has_level": True,
            "tags": [],
        },
    ])
    c = Consolidator(store=store, embedder=_Emb())  # type: ignore[arg-type]

    since = datetime(2026, 11, 1, 0, 0, tzinfo=UTC)
    now = datetime(2026, 11, 3, 0, 0, tzinfo=UTC)
    assert c._scan(level=None, since=since, now=now, limit=100) == []


def test_scan_weekly_still_scans_leveled_daily() -> None:
    """weekly 层级的源是 daily → 必须能扫到(不能被新过滤误伤)。"""
    store = _store_returning([
        {
            "key": "d1",
            "content": "日报",
            "importance": 0.8,
            "created_at": "2026-11-02T03:00:00+00:00",
            "level": "daily",
            "has_level": True,
            "tags": [user_tag("u_dad")],
        },
    ])
    c = Consolidator(store=store, embedder=_Emb())  # type: ignore[arg-type]

    since = datetime(2026, 11, 1, 0, 0, tzinfo=UTC)
    now = datetime(2026, 11, 3, 0, 0, tzinfo=UTC)
    out = c._scan(level="daily", since=since, now=now, limit=100)
    assert len(out) == 1, "weekly 必须能拿 daily 当源"


def test_scan_daily_keeps_fact_with_empty_level_string() -> None:
    """老数据的 level=""(字段**缺失**)→ 是原始 fact,不能误杀。

    这就是为什么要用"字段在不在"而不是"值是否非空"。
    """
    store = _store_returning([
        {
            "key": "legacy_raw",
            "content": "老数据",
            "importance": 0.8,
            "created_at": "2026-11-01T10:00:00+00:00",
            "level": "",
            "has_level": False,  # payload 里压根没有 level key
            "tags": [user_tag("u_dad")],
        },
    ])
    c = Consolidator(store=store, embedder=_Emb())  # type: ignore[arg-type]

    since = datetime(2026, 11, 1, 0, 0, tzinfo=UTC)
    now = datetime(2026, 11, 3, 0, 0, tzinfo=UTC)
    out = c._scan(level=None, since=since, now=now, limit=100)
    assert len(out) == 1


def test_scan_daily_excludes_level_field_present_but_empty() -> None:
    """level 字段**存在**但值为空串 → 有 level key,与服务端一致,排除。

    ⚠️ 这个边界必须锁死:服务端 Qdrant 的 ``IsEmptyCondition`` 只匹配
    "key 缺失",**不**匹配"key 存在但值为空串"。如果客户端的判断逻辑
    跟服务端不一致,就会出现"服务端已排除、客户端又放回来"的错配。

    (生产代码里 ``store_memory`` 只在 ``level is not None`` 时才写该字段,
    所以实际数据里不会有这种条目 —— 但两层判断必须语义一致。)
    """
    store = _store_returning([
        {
            "key": "level_empty_but_present",
            "content": "字段存在但值为空",
            "importance": 0.8,
            "created_at": "2026-11-01T10:00:00+00:00",
            "level": "",
            "has_level": True,  # payload 里**有** level key
            "tags": [user_tag("u_dad")],
        },
    ])
    c = Consolidator(store=store, embedder=_Emb())  # type: ignore[arg-type]

    since = datetime(2026, 11, 1, 0, 0, tzinfo=UTC)
    now = datetime(2026, 11, 3, 0, 0, tzinfo=UTC)
    assert c._scan(level=None, since=since, now=now, limit=100) == []


def test_scan_daily_fallback_without_has_level_flag() -> None:
    """老 store / MagicMock 没有 has_level 标志 → 退化为看 level 的值。

    这是给**第三方实现**留的兜底:拿不到精确标志时,
    "level 值非空" 至少能挡住绝大多数升格产物。
    """
    store = _store_returning([
        {
            "key": "no_flag_raw",
            "content": "原始 fact",
            "importance": 0.8,
            "created_at": "2026-11-01T10:00:00+00:00",
            "level": "",
            "tags": [user_tag("u_dad")],
        },
        {
            "key": "no_flag_daily",
            "content": "日报",
            "importance": 0.8,
            "created_at": "2026-11-01T11:00:00+00:00",
            "level": "daily",
            "tags": [user_tag("u_dad"), "level:daily"],
        },
    ])
    c = Consolidator(store=store, embedder=_Emb())  # type: ignore[arg-type]

    since = datetime(2026, 11, 1, 0, 0, tzinfo=UTC)
    now = datetime(2026, 11, 3, 0, 0, tzinfo=UTC)
    out = c._scan(level=None, since=since, now=now, limit=100)

    keys = [o["key"] for o in out]
    assert keys == ["no_flag_raw"], f"不该放回升格产物,实际={keys}"


def test_scan_requests_only_unleveled_from_store() -> None:
    """必须把 only_unleveled=True 传给 store(服务端过滤是主防线)。"""
    store = _store_returning([])
    c = Consolidator(store=store, embedder=_Emb())  # type: ignore[arg-type]

    since = datetime(2026, 11, 1, 0, 0, tzinfo=UTC)
    now = datetime(2026, 11, 3, 0, 0, tzinfo=UTC)
    c._scan(level=None, since=since, now=now, limit=100)

    kwargs = store.list_keys_by_level.call_args.kwargs
    assert kwargs.get("only_unleveled") is True
    assert kwargs.get("level") is None


def test_scan_weekly_does_not_request_only_unleveled() -> None:
    """weekly 层级不该带 only_unleveled(它要的就是带 level 的条目)。"""
    store = _store_returning([])
    c = Consolidator(store=store, embedder=_Emb())  # type: ignore[arg-type]

    since = datetime(2026, 11, 1, 0, 0, tzinfo=UTC)
    now = datetime(2026, 11, 3, 0, 0, tzinfo=UTC)
    c._scan(level="daily", since=since, now=now, limit=100)

    kwargs = store.list_keys_by_level.call_args.kwargs
    assert not kwargs.get("only_unleveled")


# ---------------------------------------------------------------------------
# 2. 端到端:连续两天跑 daily,第二天不能把第一天的日报再吃一遍
# ---------------------------------------------------------------------------


def test_daily_does_not_recompress_previous_daily() -> None:
    """🆕 核心回归:连续两天的 daily,第二天的源里不含第一天的日报。

    模拟真实调度:
        D+1 03:00 → 生成日报 R1(覆盖 D 整天)
        D+2 03:00 → 生成日报 R2(覆盖 D+1 整天)
    R1 的 created_at 落在 D+2 的窗口内 —— 但它**不该**被当成源 fact。
    """
    store = _store_returning([
        {
            "key": "raw_1",
            "content": "[user=u_dad] 我明天去上海",
            "importance": 0.8,
            "created_at": "2026-11-01T10:00:00+00:00",
            "level": "",
            "has_level": False,
            "tags": [user_tag("u_dad"), task_tag("t1")],
        },
        {
            "key": "R1_yesterday_daily",
            "content": "【日报】昨天 2026-10-31 ~ 2026-11-01\n\n去上海",
            "importance": 0.8,
            "created_at": "2026-11-02T03:00:00+00:00",
            "level": "daily",
            "has_level": True,
            "tags": [user_tag("u_dad"), task_tag("t1"), "level:daily"],
        },
    ])
    c = Consolidator(store=store, embedder=_Emb())  # type: ignore[arg-type]

    # D+2 的窗口 = [11-01 00:00, 11-02 00:00)
    # R1 的 created_at = 11-02 03:00 其实落在窗口**外**,
    # 所以这里用一个落在窗口内的版本,确保测的是"过滤"而不是"时间窗"。
    store.list_keys_by_level = MagicMock(return_value=[
        {
            "key": "raw_1",
            "content": "[user=u_dad] 我明天去上海",
            "importance": 0.8,
            "created_at": "2026-11-01T10:00:00+00:00",
            "level": "",
            "has_level": False,
            "tags": [user_tag("u_dad"), task_tag("t1")],
        },
        {
            "key": "R1_in_window",
            "content": "【日报】前一天的日报",
            "importance": 0.8,
            "created_at": "2026-11-01T12:00:00+00:00",
            "level": "daily",
            "has_level": True,
            "tags": [user_tag("u_dad"), task_tag("t1"), "level:daily"],
        },
    ])

    since = datetime(2026, 11, 1, 0, 0, tzinfo=UTC)
    now = datetime(2026, 11, 2, 0, 0, tzinfo=UTC)
    out = c._scan(level=None, since=since, now=now, limit=100)

    keys = [o["key"] for o in out]
    assert keys == ["raw_1"], (
        f"窗口内只该有原始 fact,实际={keys}(升格产物被重复压缩了)"
    )


# ---------------------------------------------------------------------------
# 3. store 层:only_unleveled 的服务端行为
# ---------------------------------------------------------------------------


def test_store_only_unleveled_filters_server_side(tmp_path: Any) -> None:
    """真实 Qdrant:only_unleveled=True 只返回没有 level key 的条目。"""
    from smartbutler.emotion.memory.long_term import create_long_term_store

    store = create_long_term_store(
        qdrant_path=tmp_path / "qdrant", vector_size=8,
    )
    emb = [0.0] * 8
    # 原始 fact:store_memory(level=None) → payload 里没有 level key
    store.store_memory("raw", emb, "原始 fact", importance=0.8)
    # 升格产物:level="daily"
    store.store_memory("daily", emb, "日报", importance=0.8, level="daily")

    unleveled = store.list_keys_by_level(level=None, only_unleveled=True)
    keys = {k["key"] for k in unleveled}
    assert "raw" in keys, "原始 fact 必须返回"
    assert "daily" not in keys, "升格产物必须被服务端过滤掉"

    # 不加过滤 → 两条都在(证明过滤真的生效了)
    everything = store.list_keys_by_level(level=None)
    assert len(everything) == 2


def test_store_only_unleveled_excludes_level_empty_string(tmp_path: Any) -> None:
    """真实 Qdrant:``level=""``(字段存在但值为空)→ 也被排除。

    ⚠️ 锁定服务端语义:Qdrant 的 ``IsEmptyCondition`` 只匹配"key 缺失",
    不匹配"key 存在但值为空串"。这个行为必须与客户端 ``has_level``
    判断保持一致,否则两层过滤会错配。
    """
    from smartbutler.emotion.memory.long_term import create_long_term_store

    store = create_long_term_store(
        qdrant_path=tmp_path / "qdrant", vector_size=8,
    )
    emb = [0.0] * 8
    store.store_memory("raw", emb, "原始 fact", importance=0.8)
    store.store_memory("empty_level", emb, "level 字段存在但为空", importance=0.8, level="")

    unleveled = store.list_keys_by_level(level=None, only_unleveled=True)
    keys = {k["key"] for k in unleveled}
    assert keys == {"raw"}, f"只有'字段缺失'的才该返回,实际={keys}"

    # has_level 标志也必须如实反映"字段在不在"
    rows = {k["key"]: k for k in store.list_keys_by_level(level=None)}
    assert rows["raw"]["has_level"] is False
    assert rows["empty_level"]["has_level"] is True
