"""VoiceBinding 单元测试(Phase 9 Step 3.6 —— 声纹绑定 + 同名消歧)。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from smartbutler.emotion.memory.graph.binding import VoiceBinding
from smartbutler.emotion.memory.graph.models import GraphEdge, GraphNode, make_edge_id
from smartbutler.emotion.memory.graph.resolver import PersonResolver
from smartbutler.emotion.memory.graph.store import GraphStore


@pytest.fixture
def store(tmp_path: Path) -> GraphStore:
    return GraphStore(tmp_path / "graph.db")


@pytest.fixture
def binder(store: GraphStore) -> VoiceBinding:
    return VoiceBinding(store=store, resolver=PersonResolver(store=store))


def _seed_person(
    store: GraphStore,
    node_id: str,
    name: str,
    *,
    summary: str = "",
    user_id: str | None = None,
    voice_bound: bool = False,
) -> None:
    store.upsert_node(
        GraphNode(
            node_id=node_id,
            name=name,
            summary=summary,
            user_id=user_id,
            voice_bound=voice_bound,
        )
    )


# ---------------------------------------------------------------------------
# 未命中 → 直接建节点
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bind_creates_node_when_no_candidate(binder: VoiceBinding, store: GraphStore):
    """图里没这个人 → 直接建声纹锚定节点。"""
    outcome = await binder.bind(user_id="spk_b2c8", display_name="李xx")
    assert outcome.status == "created"
    assert outcome.node_id == "person:spk_b2c8"

    node = store.get_node("person:spk_b2c8")
    assert node is not None
    assert node.name == "李xx"
    assert node.user_id == "spk_b2c8"
    assert node.voice_bound is True


@pytest.mark.asyncio
async def test_bind_is_idempotent(binder: VoiceBinding, store: GraphStore):
    """同一 user_id 重复绑定 → 幂等,返回原节点。"""
    await binder.bind(user_id="spk_b2c8", display_name="李xx")
    outcome = await binder.bind(user_id="spk_b2c8", display_name="李xx")
    assert outcome.status == "bound"
    assert outcome.node_id == "person:spk_b2c8"
    assert store.stats()["nodes"] == 1


@pytest.mark.asyncio
async def test_bind_missing_params(binder: VoiceBinding):
    """缺参数 → skipped。"""
    assert (await binder.bind(user_id="", display_name="李xx")).status == "skipped"
    assert (await binder.bind(user_id="spk_x", display_name="")).status == "skipped"


# ---------------------------------------------------------------------------
# 命中同名 → 立即询问
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bind_returns_needs_confirmation_on_same_name(
    binder: VoiceBinding, store: GraphStore
):
    """⚠️ 命中同名 → 返回 needs_confirmation,**不绑定**。"""
    _seed_person(store, "person:a", "张伟", summary="张伟是我的同事,在XX公司工作")

    outcome = await binder.bind(user_id="spk_b2c8", display_name="张伟")
    assert outcome.status == "needs_confirmation"
    assert outcome.is_pending is True
    assert len(outcome.candidates) == 1

    cand = outcome.candidates[0]
    assert cand.node_id == "person:a"
    assert cand.name == "张伟"
    assert "同事" in cand.summary

    # ⚠️ 还没绑定 —— 声纹没落到图上
    assert store.find_node_by_user_id("spk_b2c8") is None
    assert store.get_node("person:a").voice_bound is False


@pytest.mark.asyncio
async def test_bind_needs_confirmation_even_with_single_candidate(
    binder: VoiceBinding, store: GraphStore
):
    """⚠️ 只命中 1 个也要询问 —— 同名场景下"只找到 1 个"最危险。"""
    _seed_person(store, "person:a", "张伟")
    outcome = await binder.bind(user_id="spk_x", display_name="张伟")
    assert outcome.status == "needs_confirmation"


@pytest.mark.asyncio
async def test_bind_multiple_candidates_returns_all(binder: VoiceBinding, store: GraphStore):
    """多个同名 → 候选全给。"""
    _seed_person(store, "person:a", "张伟", summary="A是同事")
    _seed_person(store, "person:b", "张伟", summary="B是客户")

    outcome = await binder.bind(user_id="spk_x", display_name="张伟")
    assert outcome.status == "needs_confirmation"
    assert len(outcome.candidates) == 2
    ids = {c.node_id for c in outcome.candidates}
    assert ids == {"person:a", "person:b"}


# ---------------------------------------------------------------------------
# 确认"是" → 绑定
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_confirm_binds_to_candidate(binder: VoiceBinding, store: GraphStore):
    """用户答"是" → 绑定到候选,node_id 不变。"""
    _seed_person(store, "person:a", "张伟", summary="张伟是我的同事")

    outcome = await binder.confirm(
        user_id="spk_b2c8", display_name="张伟", candidate_node_id="person:a"
    )
    assert outcome.status == "bound"
    assert outcome.node_id == "person:a"

    node = store.get_node("person:a")
    assert node is not None
    assert node.user_id == "spk_b2c8"
    assert node.voice_bound is True


@pytest.mark.asyncio
async def test_confirm_keeps_existing_edges(binder: VoiceBinding, store: GraphStore):
    """⚠️ 技术基石:绑定后已有边一条都不用改。"""
    _seed_person(store, "person:a", "张伟")
    _seed_person(store, "person:me", "我")
    edge_id = make_edge_id("person:me", "IS_FRIEND_OF", "person:a", "张伟是我朋友")
    store.upsert_edge(
        GraphEdge(
            edge_id=edge_id,
            source_id="person:me",
            target_id="person:a",
            relation_type="IS_FRIEND_OF",
            fact="张伟是我朋友",
            created_at="2026-11-09T00:00:00+00:00",
            task_ids=["t-1"],
        )
    )

    before = store.find_edges_for_node("person:a")
    assert len(before) == 1

    await binder.confirm(
        user_id="spk_b2c8", display_name="张伟", candidate_node_id="person:a"
    )

    after = store.find_edges_for_node("person:a")
    assert len(after) == 1
    assert after[0].edge_id == edge_id
    assert after[0].target_id == "person:a"


@pytest.mark.asyncio
async def test_confirm_adds_display_name_as_alias(binder: VoiceBinding, store: GraphStore):
    """用户自报的名字与图里主名不同 → 补成别名。"""
    _seed_person(store, "person:a", "张伟")
    await binder.confirm(
        user_id="spk_x", display_name="老张", candidate_node_id="person:a"
    )
    node = store.get_node("person:a")
    assert node is not None
    assert "老张" in node.aliases


@pytest.mark.asyncio
async def test_confirm_missing_node(binder: VoiceBinding):
    """候选节点不存在 → skipped,不崩。"""
    outcome = await binder.confirm(
        user_id="spk_x", display_name="张伟", candidate_node_id="person:nope"
    )
    assert outcome.status == "skipped"


# ---------------------------------------------------------------------------
# 确认"不是" → 建区分节点
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_distinguish_creates_separate_node(binder: VoiceBinding, store: GraphStore):
    """⚠️ 用户答"不是" → 建区分节点,原节点不受影响。"""
    _seed_person(store, "person:a", "张伟", summary="张伟是我的同事")

    outcome = await binder.distinguish(user_id="spk_b2c8", display_name="张伟")
    assert outcome.status == "distinct"
    assert outcome.node_id != "person:a"
    assert "#" in outcome.node_id

    # 原节点没被碰
    old = store.get_node("person:a")
    assert old is not None
    assert old.user_id is None
    assert old.voice_bound is False

    # 新节点绑了声纹
    new = store.get_node(outcome.node_id)
    assert new is not None
    assert new.user_id == "spk_b2c8"
    assert new.name == "张伟"  # name 保持可读


@pytest.mark.asyncio
async def test_distinguish_increments_suffix(binder: VoiceBinding, store: GraphStore):
    """第三个同名 → #3。"""
    _seed_person(store, "person:a", "张伟")
    o1 = await binder.distinguish(user_id="spk_1", display_name="张伟")
    o2 = await binder.distinguish(user_id="spk_2", display_name="张伟")
    assert o1.node_id.endswith("#2")
    assert o2.node_id.endswith("#3")


@pytest.mark.asyncio
async def test_bind_with_confirm_false_creates_distinct(binder: VoiceBinding, store: GraphStore):
    """不允许询问(用户不在)→ 直接建区分节点。"""
    _seed_person(store, "person:a", "张伟")
    outcome = await binder.bind(
        user_id="spk_x", display_name="张伟", confirm=False
    )
    assert outcome.status == "distinct"
    assert outcome.node_id != "person:a"


# ---------------------------------------------------------------------------
# 描述生成
# ---------------------------------------------------------------------------


def test_describe_candidate():
    """候选描述:带 summary 优先。"""
    from smartbutler.emotion.memory.graph.binding import CandidateNode

    c = CandidateNode(node_id="person:a", name="张伟", summary="我的同事")
    assert VoiceBinding.describe_candidate(c) == "张伟,我的同事"

    c2 = CandidateNode(node_id="person:a", name="张伟")
    assert VoiceBinding.describe_candidate(c2) == "张伟"


# ---------------------------------------------------------------------------
# 声纹采集完成 → 绑定到图(voice_service 集成点)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_voice_service_bind_creates_node(store: GraphStore):
    """声纹注册完成 → 图里没有同名 → 直接建节点。"""
    from smartbutler.scripts.voice_service import _bind_voice_to_graph

    binder = VoiceBinding(store=store, resolver=PersonResolver(store=store))
    await _bind_voice_to_graph(
        binder, user_id="spk_a3f9", display_name="李xx"
    )
    node = store.find_node_by_user_id("spk_a3f9")
    assert node is not None
    assert node.node_id == "person:spk_a3f9"
    assert node.name == "李xx"


@pytest.mark.asyncio
async def test_voice_service_bind_with_same_name_creates_distinct(store: GraphStore):
    """⚠️ 采集完成时用户不在 → 不询问,建区分节点。

    同名消歧留给对话中的 `resolve_person`(那时用户在场)。
    """
    from smartbutler.scripts.voice_service import _bind_voice_to_graph

    _seed_person(store, "person:a", "张伟", summary="张伟是我的同事")
    binder = VoiceBinding(store=store, resolver=PersonResolver(store=store))
    await _bind_voice_to_graph(binder, user_id="spk_x", display_name="张伟")

    # 新节点绑了声纹
    bound = store.find_node_by_user_id("spk_x")
    assert bound is not None
    assert bound.node_id != "person:a"
    # 原节点没被动
    assert store.get_node("person:a").user_id is None


@pytest.mark.asyncio
async def test_voice_service_bind_survives_failure(store: GraphStore):
    """绑定失败 → 不抛(降级)。"""

    class _BrokenBinding:
        async def bind(self, **kw: object) -> object:
            raise RuntimeError("db down")

    from smartbutler.scripts.voice_service import _bind_voice_to_graph

    await _bind_voice_to_graph(
        _BrokenBinding(), user_id="spk_x", display_name="李xx"
    )  # 不抛


@pytest.mark.asyncio
async def test_voice_service_bind_with_none_binding():
    """图未启用(binding=None)→ 跳过。"""
    from smartbutler.scripts.voice_service import _bind_voice_to_graph

    await _bind_voice_to_graph(None, user_id="spk_x", display_name="李xx")  # 不抛
