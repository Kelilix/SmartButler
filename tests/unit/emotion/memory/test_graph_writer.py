"""GraphWriter + GraphExtractHook 单元测试(Phase 9 Step 3.4)。"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from smartbutler.emotion.memory.graph.extractor import ExtractionResult
from smartbutler.emotion.memory.graph.hooks import GraphExtractHook
from smartbutler.emotion.memory.graph.models import GraphEdgeCandidate
from smartbutler.emotion.memory.graph.resolver import PersonResolver
from smartbutler.emotion.memory.graph.store import GraphStore
from smartbutler.emotion.memory.graph.writer import GraphWriter


class _FakeLLM:
    def __init__(self, content: str) -> None:
        self.content = content
        self.calls: list[list[Any]] = []

    async def chat(self, messages: list[Any], **kw: Any) -> Any:
        self.calls.append(messages)

        class _Resp:
            def __init__(self, c: str) -> None:
                self.content = c

        return _Resp(self.content)


class _FakeMsg:
    def __init__(self, role: str, content: str, user_id: str = "") -> None:
        self.role = role
        self.content = content
        self.user_id = user_id


class _FakeTaskIndex:
    def __init__(self, msgs: list[_FakeMsg]) -> None:
        self._msgs = msgs

    def iter_task_messages(self, task_id: str) -> list[_FakeMsg]:
        return self._msgs


@pytest.fixture
def store(tmp_path: Path) -> GraphStore:
    return GraphStore(tmp_path / "graph.db")


@pytest.fixture
def writer(store: GraphStore) -> GraphWriter:
    return GraphWriter(store=store, resolver=PersonResolver(store=store))


def _result(
    edges: list[tuple[str, str, str, str]],
    nodes: list[str] | None = None,
) -> ExtractionResult:
    """构造抽取结果。``edges`` 是 (source, relation, target, fact) 列表。"""
    res = ExtractionResult()
    res.nodes = nodes or []
    for src, rel, tgt, fact in edges:
        res.edges.append(
            GraphEdgeCandidate(source=src, relation_type=rel, target=tgt, fact=fact)
        )
    return res


# ---------------------------------------------------------------------------
# 落库
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_task_creates_nodes_and_edges(writer: GraphWriter, store: GraphStore):
    """落库:节点 + 边都建好。"""
    res = _result([("我", "IS_FRIEND_OF", "张女士", "张女士是我朋友")])
    nodes, edges = await writer.write_task(task_id="t-1", result=res, speaker="u1")
    assert nodes == 2
    assert edges == 1

    stats = store.stats()
    assert stats["nodes"] == 2
    assert stats["edges"] == 1


@pytest.mark.asyncio
async def test_write_task_is_idempotent(writer: GraphWriter, store: GraphStore):
    """⚠️ 同一 task 重复落库 → 只落一次。"""
    res = _result([("我", "IS_FRIEND_OF", "张女士", "张女士是我朋友")])
    await writer.write_task(task_id="t-1", result=res, speaker="u1")
    nodes, edges = await writer.write_task(task_id="t-1", result=res, speaker="u1")
    assert nodes == 0
    assert edges == 0
    assert store.stats()["edges"] == 1


@pytest.mark.asyncio
async def test_write_task_dedupes_within_batch(writer: GraphWriter, store: GraphStore):
    """⚠️ 批内消解:完全相同的 fact 不写两条边(Graphiti #1872 缺陷)。"""
    res = _result([
        ("我", "IS_FRIEND_OF", "张女士", "张女士是我朋友"),
        ("我", "IS_FRIEND_OF", "张女士", "张女士是我朋友"),
    ])
    _, edges = await writer.write_task(task_id="t-1", result=res, speaker="u1")
    assert edges == 1
    assert store.stats()["edges"] == 1


@pytest.mark.asyncio
async def test_write_task_keeps_different_facts_same_pair(writer: GraphWriter, store: GraphStore):
    """同端点但 fact 不同 → 保留两条(可能是关系演进)。"""
    res = _result([
        ("我", "IS_FRIEND_OF", "张女士", "张女士是我朋友"),
        ("我", "IS_FRIEND_OF", "张女士", "张女士是我最好的朋友,认识十年了"),
    ])
    _, edges = await writer.write_task(task_id="t-1", result=res, speaker="u1")
    assert edges == 2


@pytest.mark.asyncio
async def test_write_task_merges_edge_across_tasks(writer: GraphWriter, store: GraphStore):
    """跨 task 相同边 → 只追加 task_ids,不新增。"""
    res = _result([("我", "IS_FRIEND_OF", "张女士", "张女士是我朋友")])
    await writer.write_task(task_id="t-1", result=res, speaker="u1")
    await writer.write_task(task_id="t-2", result=res, speaker="u1")
    assert store.stats()["edges"] == 1
    me = store.find_nodes_by_name("我")[0]
    edges = store.find_edges_for_node(me.node_id)
    assert set(edges[0].task_ids) == {"t-1", "t-2"}


@pytest.mark.asyncio
async def test_write_task_skips_self_relation(writer: GraphWriter, store: GraphStore):
    """自己跟自己的边不写。"""
    res = _result([("我", "KNOWS", "我", "我认识我")])
    nodes, edges = await writer.write_task(task_id="t-1", result=res, speaker="u1")
    assert edges == 0


@pytest.mark.asyncio
async def test_write_task_refreshes_summary(writer: GraphWriter, store: GraphStore):
    """⚠️ summary 来自真实边 fact 句(用于向用户描述这个人)。"""
    res = _result([
        ("我", "WORKS_WITH", "张伟", "张伟是我的同事,在XX公司工作"),
    ])
    await writer.write_task(task_id="t-1", result=res, speaker="u1")

    nodes = store.find_nodes_by_name("张伟")
    assert len(nodes) == 1
    assert "张伟是我的同事" in nodes[0].summary


@pytest.mark.asyncio
async def test_write_task_summary_excludes_invalidated_edges(writer: GraphWriter, store: GraphStore):
    """失效边不进 summary。"""
    res = _result([("我", "IS_FRIEND_OF", "张女士", "张女士是我朋友")])
    await writer.write_task(task_id="t-1", result=res, speaker="u1")

    node = store.find_nodes_by_name("张女士")[0]
    edges = store.find_edges_for_node(node.node_id)
    store.invalidate_edge(edges[0].edge_id, invalid_at="2026-11-09", expired_at="x")

    # 再写一次触发 summary 重算
    res2 = _result([("我", "WORKS_WITH", "张女士", "张女士现在是我同事")])
    await writer.write_task(task_id="t-2", result=res2, speaker="u1")

    node = store.get_node(node.node_id)
    assert node is not None
    assert "张女士是我朋友" not in node.summary
    assert "张女士现在是我同事" in node.summary


@pytest.mark.asyncio
async def test_write_task_failure_logs_and_does_not_raise(writer: GraphWriter, store: GraphStore):
    """落库异常 → 不抛异常,记 failed 状态。"""
    res = _result([("我", "IS_FRIEND_OF", "张女士", "张女士是我朋友")])

    async def _boom(**kw: Any) -> tuple[int, int]:
        raise RuntimeError("db down")

    writer._write_task_inner = _boom  # type: ignore[method-assign]
    nodes, edges = await writer.write_task(task_id="t-1", result=res, speaker="u1")
    assert (nodes, edges) == (0, 0)
    log = store.get_task_log("t-1")
    assert log is not None
    assert log["status"] == "failed"


@pytest.mark.asyncio
async def test_write_task_empty_task_id(writer: GraphWriter):
    """空 task_id → 直接返回。"""
    res = _result([("我", "IS_FRIEND_OF", "张女士", "张女士是我朋友")])
    assert await writer.write_task(task_id="", result=res, speaker="u1") == (0, 0)


# ---------------------------------------------------------------------------
# Hook
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hook_extracts_and_writes(tmp_path: Path):
    """hook 端到端:task 消息 → 抽取 → 落库。"""
    store = GraphStore(tmp_path / "graph.db")
    writer = GraphWriter(store=store, resolver=PersonResolver(store=store))
    llm = _FakeLLM(json.dumps({
        "nodes": [{"name": "我", "type": "Person"}, {"name": "张女士", "type": "Person"}],
        "edges": [{
            "source": "我", "relation_type": "IS_FRIEND_OF", "target": "张女士",
            "fact": "张女士是我朋友",
        }],
    }))
    from smartbutler.emotion.memory.graph.extractor import GraphExtractor

    hook = GraphExtractHook(
        extractor=GraphExtractor(llm=llm),
        writer=writer,
        task_index=_FakeTaskIndex([
            _FakeMsg("user", "张女士是我朋友", "spk_u1"),
            _FakeMsg("assistant", "好的,记住了"),
        ]),
    )

    await hook._extract_and_write(task_id="t-1", title="闲聊")
    assert store.stats()["edges"] == 1
    assert hook.extracted_count == 1


@pytest.mark.asyncio
async def test_hook_disabled_does_nothing(tmp_path: Path):
    """开关关 → 零副作用。"""
    store = GraphStore(tmp_path / "graph.db")
    writer = GraphWriter(store=store, resolver=PersonResolver(store=store))
    from smartbutler.emotion.memory.graph.extractor import GraphExtractor

    hook = GraphExtractHook(
        extractor=GraphExtractor(llm=_FakeLLM('{"nodes": [], "edges": []}')),
        writer=writer,
        task_index=_FakeTaskIndex([_FakeMsg("user", "张女士是我朋友", "spk_u1")]),
        enabled=False,
    )
    await hook._extract_and_write(task_id="t-1", title="闲聊")
    assert store.stats()["edges"] == 0
    assert hook.skipped_count == 1


@pytest.mark.asyncio
async def test_hook_falls_back_to_title(tmp_path: Path):
    """读不到 task 消息 → 用 title 兜底。"""
    store = GraphStore(tmp_path / "graph.db")
    writer = GraphWriter(store=store, resolver=PersonResolver(store=store))
    llm = _FakeLLM(json.dumps({
        "nodes": [{"name": "我", "type": "Person"}, {"name": "张女士", "type": "Person"}],
        "edges": [{
            "source": "我", "relation_type": "IS_FRIEND_OF", "target": "张女士",
            "fact": "张女士是我朋友",
        }],
    }))
    from smartbutler.emotion.memory.graph.extractor import GraphExtractor

    hook = GraphExtractHook(
        extractor=GraphExtractor(llm=llm),
        writer=writer,
        task_index=_FakeTaskIndex([]),
    )
    await hook._extract_and_write(task_id="t-1", title="张女士是我朋友")
    assert store.stats()["edges"] == 1


@pytest.mark.asyncio
async def test_hook_survives_task_index_error(tmp_path: Path):
    """task_index 抛异常 → 不崩,计入 failed。"""
    store = GraphStore(tmp_path / "graph.db")
    writer = GraphWriter(store=store, resolver=PersonResolver(store=store))
    from smartbutler.emotion.memory.graph.extractor import GraphExtractor

    class _BrokenIndex:
        def iter_task_messages(self, task_id: str) -> Any:
            raise RuntimeError("db down")

    hook = GraphExtractHook(
        extractor=GraphExtractor(llm=_FakeLLM('{"nodes": [], "edges": []}')),
        writer=writer,
        task_index=_BrokenIndex(),
    )
    await hook._extract_and_write(task_id="t-1", title="闲聊")
    # 兜底用 title → 无边可抽,不算 failed
    assert store.stats()["edges"] == 0
