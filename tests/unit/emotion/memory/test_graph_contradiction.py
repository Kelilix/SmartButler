"""ContradictionResolver 单元测试(Phase 9 Step 3.5 —— 矛盾消解)。"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from smartbutler.emotion.memory.graph.contradiction import (
    ContradictionResolver,
    _cosine,
    _normalize_fact,
    _parse_json,
)
from smartbutler.emotion.memory.graph.models import GraphEdge, make_edge_id
from smartbutler.emotion.memory.graph.store import GraphStore


class _FakeLLM:
    def __init__(self, content: str = "", *, raise_exc: bool = False) -> None:
        self.content = content
        self.raise_exc = raise_exc
        self.calls: list[list[Any]] = []

    async def chat(self, messages: list[Any], **kw: Any) -> Any:
        self.calls.append(messages)
        if self.raise_exc:
            raise RuntimeError("boom")

        class _Resp:
            def __init__(self, c: str) -> None:
                self.content = c

        return _Resp(self.content)


class _FakeEmbedder:
    def __init__(self, mapping: dict[str, list[float]]) -> None:
        self._mapping = mapping

    async def embed(self, text: str) -> list[float]:
        return self._mapping.get(text, [0.0, 1.0])


@pytest.fixture
def store(tmp_path: Path) -> GraphStore:
    return GraphStore(tmp_path / "graph.db")


def _edge(
    src: str,
    tgt: str,
    rel: str,
    fact: str,
    *,
    task_ids: list[str] | None = None,
) -> GraphEdge:
    return GraphEdge(
        edge_id=make_edge_id(src, rel, tgt, fact),
        source_id=src,
        target_id=tgt,
        relation_type=rel,
        fact=fact,
        created_at="2026-11-09T00:00:00+00:00",
        task_ids=task_ids or ["t-1"],
    )


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def test_normalize_fact():
    """fact 归一化:去空白 + 小写。"""
    assert _normalize_fact("  张女士 是我 朋友 ") == "张女士是我朋友"
    assert _normalize_fact("A") == _normalize_fact("a")


def test_parse_json_variants():
    """JSON 解析容错。"""
    assert _parse_json('{"a": 1}') == {"a": 1}
    assert _parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert _parse_json("前言 {\"a\": 1} 后记") == {"a": 1}
    assert _parse_json("no json") is None


def test_cosine():
    """余弦相似度。"""
    import struct

    a = struct.pack("2f", 1.0, 0.0)
    b = struct.pack("2f", 1.0, 0.0)
    c = struct.pack("2f", 0.0, 1.0)
    assert _cosine(a, b) == pytest.approx(1.0)
    assert _cosine(a, c) == pytest.approx(0.0)
    assert _cosine(a, struct.pack("1f", 1.0)) == 0.0


# ---------------------------------------------------------------------------
# 快路径
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_fast_path_merges_identical_fact(store: GraphStore):
    """⚠️ 快路径:fact 句完全相同 → 合并,不新增边。"""
    resolver = ContradictionResolver(store=store, llm=None)
    old = _edge("person:a", "person:b", "IS_FRIEND_OF", "张女士是我朋友")
    store.upsert_edge(old)

    new = _edge("person:a", "person:b", "IS_FRIEND_OF", "张女士是我朋友", task_ids=["t-2"])
    outcome = await resolver.resolve(new_edge=new)

    assert outcome == "merged"
    assert store.stats()["edges"] == 1
    # task_ids 被追加
    fetched = store.get_edge(old.edge_id)
    assert fetched is not None
    assert set(fetched.task_ids) == {"t-1", "t-2"}


@pytest.mark.asyncio
async def test_resolve_returns_new_when_no_existing(store: GraphStore):
    """没有已有边 → new。"""
    resolver = ContradictionResolver(store=store, llm=None)
    new = _edge("person:a", "person:b", "IS_FRIEND_OF", "张女士是我朋友")
    assert await resolver.resolve(new_edge=new) == "new"


# ---------------------------------------------------------------------------
# 慢路径
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_slow_path_merges_semantically_same(store: GraphStore):
    """⚠️ 慢路径:语义相近 → 合并。"""
    embedder = _FakeEmbedder({
        "张女士是我朋友": [1.0, 0.0],
        "张女士是我的好朋友": [0.99, 0.01],
    })
    resolver = ContradictionResolver(
        store=store, llm=None, embedder=embedder, dedupe_threshold=0.85
    )
    old = _edge("person:a", "person:b", "IS_FRIEND_OF", "张女士是我朋友")
    store.upsert_edge(old)

    new = _edge(
        "person:a", "person:b", "IS_FRIEND_OF", "张女士是我的好朋友", task_ids=["t-2"]
    )
    outcome = await resolver.resolve(new_edge=new)

    assert outcome == "merged"
    assert store.stats()["edges"] == 1


@pytest.mark.asyncio
async def test_resolve_slow_path_skips_different_predicate(store: GraphStore):
    """不同谓词不走慢路径。"""
    embedder = _FakeEmbedder({
        "张女士是我朋友": [1.0, 0.0],
        "张女士是我的好朋友": [0.99, 0.01],
    })
    resolver = ContradictionResolver(
        store=store, llm=None, embedder=embedder, dedupe_threshold=0.85
    )
    old = _edge("person:a", "person:b", "IS_FRIEND_OF", "张女士是我朋友")
    store.upsert_edge(old)

    new = _edge("person:a", "person:b", "KNOWS", "张女士是我的好朋友")
    assert await resolver.resolve(new_edge=new) == "new"


# ---------------------------------------------------------------------------
# LLM 矛盾判定
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_invalidates_contradicted_edge(store: GraphStore):
    """⚠️ 矛盾 → 旧边失效(加时间戳,绝不 DELETE)。"""
    llm = _FakeLLM(json.dumps({
        "contradicts": True, "reason": "新关系否定了旧关系",
    }))
    resolver = ContradictionResolver(store=store, llm=llm)
    old = _edge("person:a", "person:b", "IS_FRIEND_OF", "张女士是我朋友")
    store.upsert_edge(old)

    new = _edge(
        "person:a", "person:b", "IS_FRIEND_OF", "张女士不再是我朋友了,我们掰了"
    )
    outcome = await resolver.resolve(new_edge=new, valid_at="2026-11-09")

    assert outcome == "contradicts"

    # ⚠️ 旧边还在,但已失效
    fetched = store.get_edge(old.edge_id)
    assert fetched is not None
    assert fetched.invalid_at == "2026-11-09"
    assert fetched.is_active is False
    assert fetched.expired_at is not None

    # 活跃查询不再返回它
    assert store.find_edges_by_pair("person:a", "person:b") == []
    # 但历史查询还能拿到
    assert len(store.find_edges_by_pair("person:a", "person:b", only_active=False)) == 1


@pytest.mark.asyncio
async def test_resolve_keeps_non_contradicted_edges(store: GraphStore):
    """不矛盾 → 两条边共存(关系演进)。"""
    llm = _FakeLLM(json.dumps({"contradicts": False, "reason": "关系演进"}))
    resolver = ContradictionResolver(store=store, llm=llm)
    old = _edge("person:a", "person:b", "IS_FRIEND_OF", "张女士是我朋友")
    store.upsert_edge(old)

    new = _edge("person:a", "person:b", "IS_FRIEND_OF", "张女士是我同事了")
    outcome = await resolver.resolve(new_edge=new)

    assert outcome == "new"
    # 旧边仍然活跃
    fetched = store.get_edge(old.edge_id)
    assert fetched is not None
    assert fetched.is_active is True


@pytest.mark.asyncio
async def test_resolve_llm_failure_keeps_edge(store: GraphStore):
    """LLM 报错 → 不失效(保守:宁可两条边共存,不要误杀历史)。"""
    resolver = ContradictionResolver(store=store, llm=_FakeLLM(raise_exc=True))
    old = _edge("person:a", "person:b", "IS_FRIEND_OF", "张女士是我朋友")
    store.upsert_edge(old)

    new = _edge("person:a", "person:b", "IS_FRIEND_OF", "张女士不再是我朋友了")
    outcome = await resolver.resolve(new_edge=new)

    assert outcome == "new"
    fetched = store.get_edge(old.edge_id)
    assert fetched is not None
    assert fetched.is_active is True


@pytest.mark.asyncio
async def test_resolve_different_predicate_not_judged(store: GraphStore):
    """不同谓词不做矛盾判定。"""
    llm = _FakeLLM(json.dumps({"contradicts": True, "reason": "x"}))
    resolver = ContradictionResolver(store=store, llm=llm)
    old = _edge("person:a", "person:b", "IS_FRIEND_OF", "张女士是我朋友")
    store.upsert_edge(old)

    new = _edge("person:a", "person:b", "WORKS_WITH", "张女士是我同事")
    assert await resolver.resolve(new_edge=new) == "new"
    assert llm.calls == []
