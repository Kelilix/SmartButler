"""GraphStore 单元测试(Phase 9 Step 3.1)。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from smartbutler.emotion.memory.graph.models import (
    GraphEdge,
    GraphNode,
    make_edge_id,
    make_name_node_id,
    make_voice_node_id,
    normalize_person_name,
)
from smartbutler.emotion.memory.graph.store import GraphStore


@pytest.fixture
def store(tmp_path: Path) -> GraphStore:
    return GraphStore(tmp_path / "graph.db")


def _node(node_id: str, name: str, **kw) -> GraphNode:
    return GraphNode(
        node_id=node_id,
        name=name,
        aliases=kw.get("aliases", []),
        user_id=kw.get("user_id"),
        voice_bound=kw.get("voice_bound", False),
        summary=kw.get("summary", ""),
        created_at=kw.get("created_at", "2026-11-09T00:00:00+00:00"),
        updated_at=kw.get("updated_at", "2026-11-09T00:00:00+00:00"),
    )


def _edge(src: str, tgt: str, rel: str, fact: str, **kw) -> GraphEdge:
    return GraphEdge(
        edge_id=make_edge_id(src, rel, tgt, fact),
        source_id=src,
        target_id=tgt,
        relation_type=rel,
        fact=fact,
        speaker=kw.get("speaker", "u1"),
        valid_at=kw.get("valid_at"),
        invalid_at=kw.get("invalid_at"),
        created_at=kw.get("created_at", "2026-11-09T00:00:00+00:00"),
        expired_at=kw.get("expired_at"),
        task_ids=kw.get("task_ids", ["t-1"]),
    )


# ---------------------------------------------------------------------------
# 初始化
# ---------------------------------------------------------------------------


def test_schema_init_is_idempotent(tmp_path: Path):
    """重复初始化不报错(建表幂等)。"""
    p = tmp_path / "graph.db"
    GraphStore(p)
    GraphStore(p)  # 不抛异常


def test_store_creates_parent_dir(tmp_path: Path):
    """父目录不存在时自动创建。"""
    p = tmp_path / "nested" / "dir" / "graph.db"
    GraphStore(p)
    assert p.exists()


# ---------------------------------------------------------------------------
# 节点
# ---------------------------------------------------------------------------


def test_upsert_and_get_node(store: GraphStore):
    """节点插入 + 读取 round-trip。"""
    store.upsert_node(_node("person:u1", "我"))
    node = store.get_node("person:u1")
    assert node is not None
    assert node.name == "我"
    assert node.type == "Person"
    assert node.voice_bound is False


def test_upsert_node_preserves_voice_binding(store: GraphStore):
    """⚠️ 普通 upsert 不得清掉已绑定的声纹锚点。

    否则节点会"降级"回未绑定,导致后续同名询问反复打扰用户。
    """
    store.upsert_node(_node("person:u1", "我", user_id="spk_a", voice_bound=True))
    # 再次 upsert 一个不带 user_id 的节点
    store.upsert_node(_node("person:u1", "我"))
    node = store.get_node("person:u1")
    assert node is not None
    assert node.user_id == "spk_a"
    assert node.voice_bound is True


def test_upsert_node_merges_aliases(store: GraphStore):
    """aliases 合并而非替换(不同来源提到不同称呼)。"""
    store.upsert_node(_node("person:u1", "李xx", aliases=["李女士"]))
    store.upsert_node(_node("person:u1", "李xx", aliases=["我妈"]))
    node = store.get_node("person:u1")
    assert node is not None
    assert "李女士" in node.aliases
    assert "我妈" in node.aliases


def test_find_nodes_by_name_matches_aliases(store: GraphStore):
    """按名字查节点时 aliases 也算命中。"""
    store.upsert_node(_node("person:u1", "李xx", aliases=["李女士"]))
    found = store.find_nodes_by_name("李女士")
    assert len(found) == 1
    assert found[0].node_id == "person:u1"


def test_find_nodes_by_name_is_conservative(store: GraphStore):
    """「小李」与「李xx」不合并(保守策略)。"""
    store.upsert_node(_node("person:u1", "李xx"))
    assert store.find_nodes_by_name("小李") == []


def test_find_nodes_by_name_strips_bracket_modifier(store: GraphStore):
    """「李雷(同事)」与「李雷」归一为同一节点。"""
    store.upsert_node(_node("person:u1", "李雷"))
    found = store.find_nodes_by_name("李雷(同事)")
    assert len(found) == 1


def test_find_node_by_user_id(store: GraphStore):
    """按声纹 user_id 查节点。"""
    store.upsert_node(_node("person:spk_a", "李xx", user_id="spk_a", voice_bound=True))
    found = store.find_node_by_user_id("spk_a")
    assert found is not None
    assert found.node_id == "person:spk_a"


def test_add_alias(store: GraphStore):
    """追加别名。"""
    store.upsert_node(_node("person:u1", "李xx"))
    store.add_alias("person:u1", "小李")
    node = store.get_node("person:u1")
    assert node is not None
    assert "小李" in node.aliases


def test_bind_voice_keeps_node_id(store: GraphStore):
    """⚠️ 技术基石:绑定声纹后 node_id 不变。"""
    store.upsert_node(_node("person:name:abc", "李xx"))
    assert store.bind_voice("person:name:abc", "spk_b2c8") is True
    node = store.get_node("person:name:abc")
    assert node is not None
    assert node.user_id == "spk_b2c8"
    assert node.voice_bound is True
    # 原 node_id 仍然存在(没有换 id)
    assert store.get_node("person:spk_b2c8") is None


# ---------------------------------------------------------------------------
# 节点合并
# ---------------------------------------------------------------------------


def test_merge_nodes_moves_edges_and_deletes_dup(store: GraphStore):
    """合并节点:边改指向主节点,dup 被删除。"""
    store.upsert_node(_node("person:a", "张伟A"))
    store.upsert_node(_node("person:b", "张伟B"))
    store.upsert_edge(_edge("person:a", "person:b", "WORKS_WITH", "张伟A和B是同事"))

    assert store.merge_nodes("person:a", "person:b") is True

    assert store.get_node("person:b") is None
    node_a = store.get_node("person:a")
    assert node_a is not None
    assert "张伟B" in node_a.aliases

    edges = store.find_edges_for_node("person:a")
    assert len(edges) == 1
    assert edges[0].source_id == "person:a"
    assert edges[0].target_id == "person:a"


def test_merge_nodes_carries_over_voice_binding(store: GraphStore):
    """主节点没绑声纹、dup 绑了 → 声纹锚点搬过来。"""
    store.upsert_node(_node("person:a", "张伟A"))
    store.upsert_node(_node("person:b", "张伟B", user_id="spk_x", voice_bound=True))
    store.merge_nodes("person:a", "person:b")
    node = store.get_node("person:a")
    assert node is not None
    assert node.user_id == "spk_x"
    assert node.voice_bound is True


def test_merge_nodes_rejects_invalid_args(store: GraphStore):
    """非法参数返回 False。"""
    store.upsert_node(_node("person:a", "A"))
    assert store.merge_nodes("person:a", "person:a") is False
    assert store.merge_nodes("person:a", "") is False
    assert store.merge_nodes("person:a", "person:nope") is False


def test_count_edges(store: GraphStore):
    """节点边数统计(选主节点用)。"""
    store.upsert_node(_node("person:a", "A"))
    store.upsert_node(_node("person:b", "B"))
    store.upsert_edge(_edge("person:a", "person:b", "KNOWS", "A认识B"))
    assert store.count_edges("person:a") == 1
    assert store.count_edges("person:b") == 1
    assert store.count_edges("person:zzz") == 0


# ---------------------------------------------------------------------------
# 边
# ---------------------------------------------------------------------------


def test_upsert_edge_inserts_then_appends_task(store: GraphStore):
    """同 edge_id 二次写入只追加 task_ids,不新增边。"""
    e1 = _edge("person:a", "person:b", "KNOWS", "A认识B", task_ids=["t-1"])
    assert store.upsert_edge(e1) is True
    e2 = _edge("person:a", "person:b", "KNOWS", "A认识B", task_ids=["t-2"])
    assert store.upsert_edge(e2) is False
    edges = store.find_edges_for_node("person:a")
    assert len(edges) == 1
    assert set(edges[0].task_ids) == {"t-1", "t-2"}


def test_find_edges_by_pair_both_directions(store: GraphStore):
    """同端点查询覆盖两个方向(对称关系只存一条边)。"""
    store.upsert_node(_node("person:a", "A"))
    store.upsert_node(_node("person:b", "B"))
    store.upsert_edge(_edge("person:a", "person:b", "IS_FRIEND_OF", "A和B是朋友"))
    found = store.find_edges_by_pair("person:b", "person:a")
    assert len(found) == 1


def test_invalidate_edge_keeps_history(store: GraphStore):
    """⚠️ 矛盾消解只加时间戳,绝不 DELETE。"""
    store.upsert_node(_node("person:a", "A"))
    store.upsert_node(_node("person:b", "B"))
    e = _edge("person:a", "person:b", "IS_FRIEND_OF", "A和B是朋友")
    store.upsert_edge(e)

    assert store.invalidate_edge(
        e.edge_id, invalid_at="2026-11-09", expired_at="2026-11-09T10:00:00+00:00"
    ) is True

    # 边还在库里(历史保留)
    fetched = store.get_edge(e.edge_id)
    assert fetched is not None
    assert fetched.invalid_at == "2026-11-09"
    assert fetched.is_active is False

    # 活跃查询不再返回它
    assert store.find_edges_by_pair("person:a", "person:b") == []
    # 但历史查询还能拿到
    assert len(store.find_edges_by_pair("person:a", "person:b", only_active=False)) == 1


def test_neighbors_depth_1_and_2(store: GraphStore):
    """递归 CTE:N 跳邻居查询。"""
    for nid in ("a", "b", "c"):
        store.upsert_node(_node(f"person:{nid}", nid.upper()))
    store.upsert_edge(_edge("person:a", "person:b", "KNOWS", "A认识B"))
    store.upsert_edge(_edge("person:b", "person:c", "KNOWS", "B认识C"))

    assert store.neighbors("person:a", depth=1) == ["person:b"]
    depth2 = set(store.neighbors("person:a", depth=2))
    assert depth2 == {"person:b", "person:c"}


def test_neighbors_skips_inactive_edges(store: GraphStore):
    """失效边不参与邻居遍历。"""
    store.upsert_node(_node("person:a", "A"))
    store.upsert_node(_node("person:b", "B"))
    e = _edge("person:a", "person:b", "IS_FRIEND_OF", "A和B是朋友")
    store.upsert_edge(e)
    store.invalidate_edge(e.edge_id, invalid_at="2026-11-09", expired_at="2026-11-09")

    assert store.neighbors("person:a", depth=1) == []
    assert store.neighbors("person:a", depth=1, only_active=False) == ["person:b"]


def test_append_edge_task(store: GraphStore):
    """追加溯源 task_id(去重)。"""
    store.upsert_node(_node("person:a", "A"))
    store.upsert_node(_node("person:b", "B"))
    e = _edge("person:a", "person:b", "KNOWS", "A认识B", task_ids=["t-1"])
    store.upsert_edge(e)
    store.append_edge_task(e.edge_id, "t-2")
    store.append_edge_task(e.edge_id, "t-2")  # 重复
    fetched = store.get_edge(e.edge_id)
    assert fetched is not None
    assert fetched.task_ids == ["t-1", "t-2"]


# ---------------------------------------------------------------------------
# 幂等日志
# ---------------------------------------------------------------------------


def test_task_log_idempotency(store: GraphStore):
    """同一 task 重复记录 → has_task 为 True。"""
    assert store.has_task("t-1") is False
    store.log_task("t-1", node_count=2, edge_count=1)
    assert store.has_task("t-1") is True
    log = store.get_task_log("t-1")
    assert log is not None
    assert log["node_count"] == 2
    assert log["edge_count"] == 1
    assert log["status"] == "ok"


def test_stats(store: GraphStore):
    """图规模统计。"""
    store.upsert_node(_node("person:a", "A"))
    store.upsert_node(_node("person:b", "B", user_id="spk_x", voice_bound=True))
    store.upsert_edge(_edge("person:a", "person:b", "KNOWS", "A认识B"))
    s = store.stats()
    assert s["nodes"] == 2
    assert s["edges"] == 1
    assert s["active_edges"] == 1
    assert s["voice_bound_nodes"] == 1


# ---------------------------------------------------------------------------
# 模型工具
# ---------------------------------------------------------------------------


def test_normalize_person_name():
    """名字归一化:全角/空格/括号修饰。"""
    assert normalize_person_name("李雷(同事)") == "李雷"
    assert normalize_person_name("  李  雷 ") == "李雷"
    assert normalize_person_name("李雷") == normalize_person_name("李雷")
    assert normalize_person_name("") == ""


def test_make_node_ids_are_stable():
    """节点 id 生成稳定且两种锚点格式不同。"""
    assert make_name_node_id("李xx") == make_name_node_id("李xx")
    assert make_name_node_id("李xx") != make_name_node_id("李雷")
    assert make_voice_node_id("spk_a").startswith("person:spk_a")
    assert make_name_node_id("李xx").startswith("person:name:")


def test_make_edge_id_is_stable_and_sensitive():
    """边 id 幂等基石:同参数同 id,不同 fact 不同 id。"""
    a = make_edge_id("person:a", "KNOWS", "person:b", "A认识B")
    b = make_edge_id("person:a", "KNOWS", "person:b", "A认识B")
    c = make_edge_id("person:a", "KNOWS", "person:b", "A认识C")
    assert a == b
    assert a != c


# ---------------------------------------------------------------------------
# clear_all(运维:清图)
# ---------------------------------------------------------------------------


def test_clear_all_wipes_everything(store: GraphStore):
    """clear_all → 节点 / 边 / 抽取日志全清。"""
    store.upsert_node(GraphNode(node_id="person:u1", name="高天禹",
                               user_id="u1", voice_bound=True))
    store.upsert_node(GraphNode(node_id="person:n1", name="李xx"))
    store.upsert_edge(GraphEdge(
        edge_id="e1", source_id="person:u1", target_id="person:n1",
        relation_type="FRIEND_OF", fact="李xx是我朋友",
        created_at="2026-11-09T00:00:00+00:00", task_ids=["t1"],
    ))
    store.log_task("t1", node_count=2, edge_count=1)

    before = store.stats()
    assert before["nodes"] == 2
    assert before["edges"] == 1
    assert before["voice_bound_nodes"] == 1

    counts = store.clear_all()
    assert counts == {"entities": 2, "edges": 1, "task_log": 1}

    after = store.stats()
    assert after["nodes"] == 0
    assert after["edges"] == 0
    assert after["active_edges"] == 0
    assert after["tasks"] == 0
    assert after["voice_bound_nodes"] == 0


def test_clear_all_is_idempotent(store: GraphStore):
    """清完再清不报错(计数为 0)。"""
    store.upsert_node(GraphNode(node_id="person:x", name="张伟"))
    store.clear_all()
    counts = store.clear_all()
    assert counts["entities"] == 0
    assert counts["edges"] == 0
