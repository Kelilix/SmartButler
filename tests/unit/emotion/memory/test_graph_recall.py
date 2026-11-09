"""图通道召回融合单测(Phase 9 Step 3.8)。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from smartbutler.capabilities.embedding import StubEmbedder
from smartbutler.emotion.memory.facade import MemoryContext, MemoryFacade
from smartbutler.emotion.memory.graph.models import GraphEdge, GraphNode, make_edge_id
from smartbutler.emotion.memory.graph.store import GraphStore
from smartbutler.emotion.memory.hooks import MemoryHooks
from smartbutler.emotion.memory.importance import ImportanceScorer
from smartbutler.emotion.memory.long_term import LongTermStore
from smartbutler.emotion.memory.retrieval import MemoryRetriever
from smartbutler.storage import QdrantStorage


@pytest.fixture
def facade(tmp_path: Path) -> MemoryFacade:
    q = QdrantStorage(path=tmp_path / "q", collection="t", vector_size=64)
    store = LongTermStore(q)
    embedder = StubEmbedder(dimension=64)
    retriever = MemoryRetriever(store, embedder, ImportanceScorer())
    return MemoryFacade(retriever, MemoryHooks(), store, embedder)


@pytest.fixture
def graph(tmp_path: Path) -> GraphStore:
    return GraphStore(tmp_path / "graph.db")


def _seed_edge(
    store: GraphStore,
    *,
    src: str,
    src_name: str,
    tgt: str,
    tgt_name: str,
    rel: str,
    fact: str,
    valid_at: str | None = None,
) -> None:
    store.upsert_node(GraphNode(node_id=src, name=src_name))
    store.upsert_node(GraphNode(node_id=tgt, name=tgt_name))
    edge_id = make_edge_id(src, rel, tgt, fact)
    store.upsert_edge(
        GraphEdge(
            edge_id=edge_id,
            source_id=src,
            target_id=tgt,
            relation_type=rel,
            fact=fact,
            valid_at=valid_at,
            created_at="2026-11-09T00:00:00+00:00",
            task_ids=["t-1"],
        )
    )


# ---------------------------------------------------------------------------
# 图通道关闭
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_graph_block_empty_when_not_bound(facade: MemoryFacade):
    """未绑定图 → 不产出图块。"""
    block = facade._render_graph_block("我妈是谁", ctx=None)
    assert block == ""


# ---------------------------------------------------------------------------
# 图通道开启
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_graph_block_renders_edges(facade: MemoryFacade, graph: GraphStore):
    """图块渲染成自然语言。"""
    _seed_edge(
        graph,
        src="person:me",
        src_name="我",
        tgt="person:mom",
        tgt_name="妈妈",
        rel="IS_MOTHER_OF",
        fact="我妈妈是我的母亲",
    )
    facade.bind_graph_store(graph)

    block = facade._render_graph_block("我妈是谁", ctx=None)
    assert "人物关系" in block
    assert "妈妈" in block
    assert "是母亲" in block


@pytest.mark.asyncio
async def test_graph_block_anchors_on_speaker(facade: MemoryFacade, graph: GraphStore):
    """⚠️ 说话人锚定:ctx.user_id → 该用户的边。"""
    graph.upsert_node(
        GraphNode(node_id="person:u1", name="我", user_id="spk_u1", voice_bound=True)
    )
    graph.upsert_node(GraphNode(node_id="person:mom", name="妈妈"))
    graph.upsert_edge(
        GraphEdge(
            edge_id=make_edge_id(
                "person:u1", "IS_MOTHER_OF", "person:mom", "我妈妈是我的母亲"
            ),
            source_id="person:u1",
            target_id="person:mom",
            relation_type="IS_MOTHER_OF",
            fact="我妈妈是我的母亲",
            created_at="2026-11-09T00:00:00+00:00",
            task_ids=["t-1"],
        )
    )
    facade.bind_graph_store(graph)

    ctx = MemoryContext(user_id="spk_u1", session_id="s1")
    block = facade._render_graph_block("我妈是谁", ctx=ctx)
    assert "妈妈" in block


@pytest.mark.asyncio
async def test_graph_block_finds_by_name_in_query(facade: MemoryFacade, graph: GraphStore):
    """query 里提到人名 → 拉出该节点的边。"""
    _seed_edge(
        graph,
        src="person:me",
        src_name="我",
        tgt="person:zhangwei",
        tgt_name="张伟",
        rel="WORKS_WITH",
        fact="张伟是我的同事",
    )
    facade.bind_graph_store(graph)

    block = facade._render_graph_block("张伟最近怎么样", ctx=None)
    assert "张伟" in block


@pytest.mark.asyncio
async def test_graph_block_excludes_invalidated_edges(
    facade: MemoryFacade, graph: GraphStore
):
    """⚠️ 失效边不进召回(图层也要遵守时态)。"""
    _seed_edge(
        graph,
        src="person:me",
        src_name="我",
        tgt="person:zhangwei",
        tgt_name="张伟",
        rel="IS_FRIEND_OF",
        fact="张伟是我朋友",
    )
    facade.bind_graph_store(graph)

    edges = graph.find_edges_for_node("person:zhangwei")
    graph.invalidate_edge(
        edges[0].edge_id,
        invalid_at="2026-11-09",
        expired_at="2026-11-09T10:00:00+00:00",
    )

    block = facade._render_graph_block("张伟是谁", ctx=None)
    assert "张伟" not in block


@pytest.mark.asyncio
async def test_graph_block_respects_max_edges(facade: MemoryFacade, graph: GraphStore):
    """边数上限截断。"""
    for i in range(10):
        _seed_edge(
            graph,
            src="person:me",
            src_name="我",
            tgt=f"person:p{i}",
            tgt_name=f"同事{i}",
            rel="WORKS_WITH",
            fact=f"同事{i}是我的同事",
        )
    facade.bind_graph_store(graph, max_edges=3)

    block = facade._render_graph_block("同事", ctx=None)
    assert block.count("- ") <= 3


@pytest.mark.asyncio
async def test_graph_block_survives_store_error(facade: MemoryFacade):
    """图库异常 → 返回空块,不崩(降级)。"""

    class _BrokenStore:
        def find_node_by_user_id(self, user_id: str) -> None:
            raise RuntimeError("db down")

        def find_nodes_by_name(self, name: str) -> list:
            raise RuntimeError("db down")

        def find_edges_for_node(self, *a: object, **kw: object) -> list:
            raise RuntimeError("db down")

    facade.bind_graph_store(_BrokenStore())
    block = facade._render_graph_block("张伟", ctx=None)
    assert block == ""


@pytest.mark.asyncio
async def test_format_for_prompt_includes_graph_block(
    facade: MemoryFacade, graph: GraphStore
):
    """⚠️ format_for_prompt 拼上图块。"""
    _seed_edge(
        graph,
        src="person:me",
        src_name="我",
        tgt="person:mom",
        tgt_name="妈妈",
        rel="IS_MOTHER_OF",
        fact="我妈妈是我的母亲",
    )
    facade.bind_graph_store(graph)

    out = await facade.format_for_prompt("我妈是谁", ctx=None)
    assert "人物关系" in out
    assert "妈妈" in out


@pytest.mark.asyncio
async def test_format_for_prompt_without_graph_unchanged(facade: MemoryFacade):
    """未绑定图 → 输出不含人物关系块。"""
    out = await facade.format_for_prompt("我妈是谁", ctx=None)
    assert "人物关系" not in out


# ---------------------------------------------------------------------------
# 人名提取
# ---------------------------------------------------------------------------


def test_graph_block_matches_node_names_in_query(
    facade: MemoryFacade, graph: GraphStore
):
    """⚠️ 按**图里的节点名**匹配 query —— 不从中文里猜人名。

    "张伟最近怎么样" 里没有空格,纯正则分不出"张伟"和"最近"。
    反过来做(图里有哪些人我们知道)才准。
    """
    _seed_edge(
        graph,
        src="person:me",
        src_name="我",
        tgt="person:zhangwei",
        tgt_name="张伟",
        rel="WORKS_WITH",
        fact="张伟是我的同事",
    )
    facade.bind_graph_store(graph)

    # 名字命中 → 拉出边(query 里的"最近"不会被误当人名)
    assert "张伟" in facade._render_graph_block("张伟最近怎么样", ctx=None)
    # query 里没有任何图里的人名 → 不拉边
    assert facade._render_graph_block("最近天气不错", ctx=None) == ""


def test_relation_labels_cover_all_predicates():
    """中文标签覆盖全部谓词。"""
    from smartbutler.emotion.memory.facade import _RELATION_LABELS
    from smartbutler.emotion.memory.graph.models import all_edge_types

    missing = all_edge_types() - set(_RELATION_LABELS)
    assert not missing, f"缺少中文标签的谓词: {missing}"
