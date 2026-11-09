"""GraphExtractor + PersonResolver 单元测试(Phase 9 Step 3.2 / 3.3)。"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from smartbutler.emotion.memory.graph.extractor import (
    GraphExtractor,
    _parse_json,
)
from smartbutler.emotion.memory.graph.models import GraphNode
from smartbutler.emotion.memory.graph.resolver import PersonResolver
from smartbutler.emotion.memory.graph.store import GraphStore


class _FakeLLM:
    """假 LLM:返回预设内容(可模拟失败)。"""

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


class _FakeProfile:
    def __init__(self, user_id: str, display_name: str) -> None:
        self.user_id = user_id
        self.display_name = display_name


class _FakeRegistry:
    def __init__(self, profiles: dict[str, _FakeProfile]) -> None:
        self._profiles = profiles

    async def get_by_display_name(self, name: str) -> _FakeProfile | None:
        return self._profiles.get(name.strip())


# ---------------------------------------------------------------------------
# JSON 解析
# ---------------------------------------------------------------------------


def test_parse_json_plain():
    assert _parse_json('{"a": 1}') == {"a": 1}


def test_parse_json_with_fence():
    assert _parse_json('```json\n{"a": 1}\n```') == {"a": 1}


def test_parse_json_with_surrounding_text():
    raw = '好的,结果如下:\n{"nodes": [], "edges": []}\n希望有用'
    assert _parse_json(raw) == {"nodes": [], "edges": []}


def test_parse_json_invalid_returns_none():
    assert _parse_json("not json") is None
    assert _parse_json("") is None


# ---------------------------------------------------------------------------
# 抽取
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_extract_nodes_and_edges():
    """标准 JSON → 正确解析节点与边。"""
    llm = _FakeLLM(json.dumps({
        "nodes": [{"name": "我", "type": "Person"}, {"name": "张女士", "type": "Person"}],
        "edges": [{
            "source": "我", "relation_type": "IS_FRIEND_OF", "target": "张女士",
            "fact": "张女士是我最好的朋友,认识十年了", "fact_ref": "1",
        }],
    }))
    ex = GraphExtractor(llm=llm)
    result = await ex.extract(facts=[("u1", "张女士是我最好的朋友")])
    assert result.nodes == ["我", "张女士"]
    assert len(result.edges) == 1
    e = result.edges[0]
    assert e.source == "我"
    assert e.target == "张女士"
    assert e.relation_type == "IS_FRIEND_OF"
    assert e.fact == "张女士是我最好的朋友,认识十年了"


@pytest.mark.asyncio
async def test_extract_drops_edge_with_invalid_endpoint():
    """⚠️ 端点不在人物列表 → 丢弃(防 LLM 编造端点)。"""
    llm = _FakeLLM(json.dumps({
        "nodes": [{"name": "我", "type": "Person"}],
        "edges": [{
            "source": "我", "relation_type": "KNOWS", "target": "编造的人",
            "fact": "我认识编造的人",
        }],
    }))
    ex = GraphExtractor(llm=llm)
    result = await ex.extract(facts=[("u1", "我认识编造的人")])
    assert result.edges == []
    assert result.dropped_invalid_endpoint == 1


@pytest.mark.asyncio
async def test_extract_degrades_unknown_predicate():
    """谓词不在词表 → 降级 RELATES_TO,保留 fact 句。"""
    llm = _FakeLLM(json.dumps({
        "nodes": [{"name": "A", "type": "Person"}, {"name": "B", "type": "Person"}],
        "edges": [{
            "source": "A", "relation_type": "SOME_UNKNOWN_REL", "target": "B",
            "fact": "A和B有某种关系",
        }],
    }))
    ex = GraphExtractor(llm=llm)
    result = await ex.extract(facts=[("u1", "A和B有某种关系")])
    assert len(result.edges) == 1
    assert result.edges[0].relation_type == "RELATES_TO"
    assert result.edges[0].fact == "A和B有某种关系"
    assert result.dropped_unknown_predicate == 1


@pytest.mark.asyncio
async def test_extract_drops_non_person_node():
    """⚠️ LLM 抽出公司/地点 → 节点丢弃,相关边也丢弃。"""
    llm = _FakeLLM(json.dumps({
        "nodes": [
            {"name": "我", "type": "Person"},
            {"name": "上海", "type": "Location"},
        ],
        "edges": [{
            "source": "我", "relation_type": "KNOWS", "target": "上海",
            "fact": "我在上海",
        }],
    }))
    ex = GraphExtractor(llm=llm)
    result = await ex.extract(facts=[("u1", "我在上海")])
    assert "上海" not in result.nodes
    assert result.edges == []


@pytest.mark.asyncio
async def test_extract_drops_self_relation():
    """自己跟自己没关系。"""
    llm = _FakeLLM(json.dumps({
        "nodes": [{"name": "我", "type": "Person"}],
        "edges": [{
            "source": "我", "relation_type": "KNOWS", "target": "我", "fact": "我认识我",
        }],
    }))
    ex = GraphExtractor(llm=llm)
    result = await ex.extract(facts=[("u1", "我认识我")])
    assert result.edges == []


@pytest.mark.asyncio
async def test_extract_truncates_at_max_edges():
    """边数上限截断。"""
    nodes = [{"name": f"P{i}", "type": "Person"} for i in range(10)]
    edges = [
        {"source": "P0", "relation_type": "KNOWS", "target": f"P{i}",
         "fact": f"P0认识P{i}"}
        for i in range(1, 10)
    ]
    llm = _FakeLLM(json.dumps({"nodes": nodes, "edges": edges}))
    ex = GraphExtractor(llm=llm, max_edges=3)
    result = await ex.extract(facts=[("u1", "一堆关系")])
    assert len(result.edges) == 3


@pytest.mark.asyncio
async def test_extract_llm_failure_returns_empty():
    """LLM 报错 → 返回空结果,不抛异常(降级)。"""
    ex = GraphExtractor(llm=_FakeLLM(raise_exc=True))
    result = await ex.extract(facts=[("u1", "张女士是我朋友")])
    assert result.is_empty


@pytest.mark.asyncio
async def test_extract_bad_json_returns_empty():
    """JSON 畸形 → 返回空结果,不抛异常。"""
    ex = GraphExtractor(llm=_FakeLLM("这不是 JSON"))
    result = await ex.extract(facts=[("u1", "张女士是我朋友")])
    assert result.is_empty


@pytest.mark.asyncio
async def test_extract_without_llm_returns_empty():
    """未注入 LLM → 返回空结果。"""
    ex = GraphExtractor(llm=None)
    result = await ex.extract(facts=[("u1", "张女士是我朋友")])
    assert result.is_empty


@pytest.mark.asyncio
async def test_extract_empty_facts_returns_empty():
    """没有 fact → 不调 LLM。"""
    llm = _FakeLLM('{"nodes": [], "edges": []}')
    ex = GraphExtractor(llm=llm)
    result = await ex.extract(facts=[])
    assert result.is_empty
    assert llm.calls == []


@pytest.mark.asyncio
async def test_extract_includes_facts_and_messages_in_prompt():
    """输入 = fact 骨架 + 原始消息(决策① A3)。"""
    llm = _FakeLLM('{"nodes": [], "edges": []}')
    ex = GraphExtractor(llm=llm)
    await ex.extract(
        facts=[("spk_a", "张女士是我朋友")],
        messages="[user] 张女士是我朋友\n[assistant] 好的",
    )
    prompt = llm.calls[0][-1].content
    assert "张女士是我朋友" in prompt
    assert "原始对话" in prompt
    assert "spk_a" in prompt


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path: Path) -> GraphStore:
    return GraphStore(tmp_path / "graph.db")


@pytest.mark.asyncio
async def test_resolve_path_a_voice_anchor(store: GraphStore):
    """路径 A:声纹档案命中 → person:<user_id>。"""
    registry = _FakeRegistry({"张女士": _FakeProfile("spk_a3f9", "张女士")})
    resolver = PersonResolver(store=store, registry=registry)
    node = await resolver.resolve("张女士")
    assert node is not None
    assert node.node_id == "person:spk_a3f9"
    assert node.user_id == "spk_a3f9"
    assert node.voice_bound is True


@pytest.mark.asyncio
async def test_resolve_path_b_existing_graph_node(store: GraphStore):
    """路径 B:图内已有 → 复用(且不新建)。"""
    store.upsert_node(GraphNode(node_id="person:u1", name="李xx"))
    resolver = PersonResolver(store=store, registry=None)
    node = await resolver.resolve("李xx")
    assert node is not None
    assert node.node_id == "person:u1"


@pytest.mark.asyncio
async def test_resolve_path_c_new_name_anchor(store: GraphStore):
    """路径 C:都没命中 → 名字锚点(不写库)。"""
    resolver = PersonResolver(store=store, registry=None)
    node = await resolver.resolve("新的人")
    assert node is not None
    assert node.node_id.startswith("person:name:")
    assert node.user_id is None
    # 只查不建 —— 库里还没有
    assert store.get_node(node.node_id) is None


@pytest.mark.asyncio
async def test_resolve_prefers_existing_graph_node_over_new_voice_anchor(store: GraphStore):
    """声纹命中时,若图里已有同 user_id 的节点 → 用它(保住已有边)。"""
    store.upsert_node(
        GraphNode(node_id="person:name:abc", name="张女士", user_id="spk_a3f9",
                  voice_bound=True)
    )
    registry = _FakeRegistry({"张女士": _FakeProfile("spk_a3f9", "张女士")})
    resolver = PersonResolver(store=store, registry=registry)
    node = await resolver.resolve("张女士")
    assert node is not None
    assert node.node_id == "person:name:abc"


@pytest.mark.asyncio
async def test_resolve_registry_failure_degrades(store: GraphStore):
    """声纹库异常 → 降级到路径 B/C,不崩。"""

    class _BrokenRegistry:
        async def get_by_display_name(self, name: str) -> Any:
            raise RuntimeError("db down")

    resolver = PersonResolver(store=store, registry=_BrokenRegistry())
    node = await resolver.resolve("李xx")
    assert node is not None
    assert node.node_id.startswith("person:name:")


@pytest.mark.asyncio
async def test_resolve_empty_name_returns_none(store: GraphStore):
    """空名 → None。"""
    resolver = PersonResolver(store=store, registry=None)
    assert await resolver.resolve("") is None
    assert await resolver.resolve("   ") is None


@pytest.mark.asyncio
async def test_find_candidates(store: GraphStore):
    """候选查找(同名消歧用)。"""
    store.upsert_node(GraphNode(node_id="person:a", name="张伟"))
    store.upsert_node(GraphNode(node_id="person:b", name="张伟"))
    candidates = PersonResolver(store=store).find_candidates("张伟")
    assert len(candidates) == 2


@pytest.mark.asyncio
async def test_make_distinct_node_id(store: GraphStore):
    """同名 → 后缀区分。"""
    resolver = PersonResolver(store=store)
    base = resolver.make_distinct_node_id("张伟", [])
    assert base.startswith("person:name:")
    second = resolver.make_distinct_node_id("张伟", [base])
    assert second == f"{base}#2"
    third = resolver.make_distinct_node_id("张伟", [base, second])
    assert third == f"{base}#3"


def test_is_same_person():
    """同一人判定(保守)。"""
    a = GraphNode(node_id="person:a", name="李xx", user_id="spk_x", voice_bound=True)
    b = GraphNode(node_id="person:b", name="李xx", user_id="spk_x", voice_bound=True)
    c = GraphNode(node_id="person:c", name="李雷")
    assert PersonResolver.is_same_person(a, b) is True
    assert PersonResolver.is_same_person(a, c) is False
