"""🆕 Phase 9: 记忆图的装配层(wiring)测试。

验证:
1. ``graph_enabled=False``(默认)→ 零副作用(不建图、不接线)
2. ``graph_enabled=True`` → 图组件正确装配并接到 facade / tool
3. 图装配失败 → 降级(记 warning),不影响主记忆系统
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from smartbutler.emotion.memory.wiring import MemorySystem, build_memory_system


class _FakeSettings:
    """假配置(避免依赖 .env)。"""

    n_completed_tasks = 3
    n_chitchat_tasks = 30
    n_prior_tasks = 3
    recall_min_score = 0.0
    judge_interval_seconds = 3600.0
    judge_on_message = False
    judge_min_interval_seconds = 60.0
    on_message_debounce_seconds = 0.0
    paused_max_staleness = 5
    judge_window_size = 2
    auto_extract_enabled = True
    auto_extract_min_importance = 0.0
    judge_timeout_seconds = 8.0

    # 🆕 Phase 9
    graph_enabled = False
    graph_edge_max_per_task = 20
    graph_dedupe_threshold = 0.85
    graph_recall_max_edges = 10
    graph_db_path = "graph.db"


async def _build(settings: _FakeSettings, tmp_path: Path) -> MemorySystem:
    return await build_memory_system(
        settings,
        llm=None,
        data_dir=tmp_path / "memory",
        qdrant_path=tmp_path / "qdrant",
        qdrant_collection="test_graph_wiring",
    )


# ---------------------------------------------------------------------------
# 1. 关闭(默认)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_graph_disabled_by_default(tmp_path: Path) -> None:
    """⚠️ 默认关闭 → 零副作用。"""
    sys_ = await _build(_FakeSettings(), tmp_path)
    try:
        assert sys_.graph_store is None
        assert sys_.graph_extract is None
        assert sys_.graph_binding is None
        # facade 没接图
        assert sys_.facade._graph_store is None
        # 不该建出 graph.db 文件
        assert not (tmp_path / "memory" / "data" / "graph.db").exists()
    finally:
        await sys_.stop()


@pytest.mark.asyncio
async def test_graph_disabled_produces_no_graph_block(tmp_path: Path) -> None:
    """关闭时 prompt 里没有人物关系块。"""
    sys_ = await _build(_FakeSettings(), tmp_path)
    try:
        out = await sys_.facade.format_for_prompt("我妈是谁", ctx=None)
        assert "人物关系" not in out
    finally:
        await sys_.stop()


@pytest.mark.asyncio
async def test_graph_disabled_unregisters_tool(tmp_path: Path) -> None:
    """⚠️ 关闭 → 注销 resolve_person tool,LLM 看不见它。

    tool 是模块级注册的,和 graph_enabled 无关。不注销的话 LLM 会
    调它然后拿到"记忆图功能未启用" —— 白烧一次工具调用。
    """
    from smartbutler.capabilities.tools.registry import ToolRegistry

    registry = ToolRegistry.get_default()
    # 先确保它存在(import 即注册)
    import smartbutler.capabilities.tools.graph.resolve_person  # noqa: F401

    if registry.try_get("resolve_person") is None:
        pytest.skip("resolve_person 未注册")

    sys_ = await _build(_FakeSettings(), tmp_path)
    try:
        assert registry.try_get("resolve_person") is None
    finally:
        await sys_.stop()


@pytest.mark.asyncio
async def test_graph_enabled_keeps_tool_registered(tmp_path: Path) -> None:
    """开启 → tool 保留在注册表里。"""
    from smartbutler.capabilities.tools.registry import ToolRegistry

    registry = ToolRegistry.get_default()
    import smartbutler.capabilities.tools.graph.resolve_person  # noqa: F401

    settings = _FakeSettings()
    settings.graph_enabled = True
    sys_ = await _build(settings, tmp_path)
    try:
        assert registry.try_get("resolve_person") is not None
    finally:
        await sys_.stop()


# ---------------------------------------------------------------------------
# 2. 开启
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_graph_enabled_wires_components(tmp_path: Path) -> None:
    """开启 → 图组件装配齐全并接到 facade。"""
    settings = _FakeSettings()
    settings.graph_enabled = True
    sys_ = await _build(settings, tmp_path)
    try:
        assert sys_.graph_store is not None
        assert sys_.graph_extract is not None
        assert sys_.graph_binding is not None

        # facade 接上了
        assert sys_.facade._graph_store is sys_.graph_store
        assert sys_.facade._graph_max_edges == 10

        # 图库文件建出来了
        assert sys_.graph_store.path.exists()

        # 抽取 hook 注册了
        assert sys_.graph_extract.stats()["extracted"] == 0
    finally:
        await sys_.stop()


@pytest.mark.asyncio
async def test_graph_enabled_injects_tool_dependencies(tmp_path: Path) -> None:
    """开启 → tool 依赖注入(resolve_person 能用)。"""
    import smartbutler.capabilities.tools.graph.resolve_person as tool_mod

    settings = _FakeSettings()
    settings.graph_enabled = True
    sys_ = await _build(settings, tmp_path)
    try:
        assert tool_mod._graph_store is not None
        assert tool_mod._resolver is not None
        assert tool_mod._writer is not None
    finally:
        await sys_.stop()
        tool_mod.clear_graph_dependencies()


@pytest.mark.asyncio
async def test_graph_store_is_usable(tmp_path: Path) -> None:
    """开启 → 图库真的能写能读。"""
    from smartbutler.emotion.memory.graph.models import GraphNode

    settings = _FakeSettings()
    settings.graph_enabled = True
    sys_ = await _build(settings, tmp_path)
    try:
        sys_.graph_store.upsert_node(GraphNode(node_id="person:x", name="张伟"))
        found = sys_.graph_store.find_nodes_by_name("张伟")
        assert len(found) == 1
        assert found[0].node_id == "person:x"
    finally:
        await sys_.stop()


@pytest.mark.asyncio
async def test_graph_path_under_data_dir(tmp_path: Path) -> None:
    """相对路径 → 落在 data_dir 下。

    ⚠️ 曾经的 bug:默认 ``graph_db_path="data/graph.db"`` 会被拼成
    ``memory/data/graph.db``(多一层 ``data/``)。现在默认是
    ``"graph.db"`` → ``memory/graph.db``。
    """
    settings = _FakeSettings()
    settings.graph_enabled = True
    sys_ = await _build(settings, tmp_path)
    try:
        expected = tmp_path / "memory" / "graph.db"
        assert sys_.graph_store.path == expected
        assert expected.exists()
    finally:
        await sys_.stop()


@pytest.mark.asyncio
async def test_graph_path_data_prefix_under_project_root(
    tmp_path: Path, monkeypatch
) -> None:
    """``"data/xxx.db"`` 写法 → 落在 **项目根/data/xxx.db**(不是 data_dir)。"""
    from smartbutler.emotion.memory.graph.paths import resolve_graph_db_path

    root = tmp_path / "proj"
    data_dir = root / "data" / "memory"
    # "data/graph.db" → <root>/data/graph.db
    assert resolve_graph_db_path(
        "data/graph.db", data_dir=data_dir, project_root=root
    ) == (root / "data" / "graph.db")
    # "graph.db" → data_dir/graph.db
    assert resolve_graph_db_path(
        "graph.db", data_dir=data_dir, project_root=root
    ) == (data_dir / "graph.db")
    # 绝对路径 → 原样
    abs_path = tmp_path / "abs" / "g.db"
    assert resolve_graph_db_path(
        abs_path, data_dir=data_dir, project_root=root
    ) == abs_path
    # 空值 → 默认 graph.db
    assert resolve_graph_db_path(
        None, data_dir=data_dir, project_root=root
    ) == (data_dir / "graph.db")


# ---------------------------------------------------------------------------
# 3. 降级
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_graph_init_failure_degrades(tmp_path: Path, monkeypatch) -> None:
    """⚠️ 图装配失败 → 记 warning,主记忆系统照常工作。"""
    import smartbutler.emotion.memory.graph.store as store_mod

    def _boom(*a: object, **kw: object) -> None:
        raise RuntimeError("db init failed")

    monkeypatch.setattr(store_mod.GraphStore, "__init__", _boom)

    settings = _FakeSettings()
    settings.graph_enabled = True
    sys_ = await _build(settings, tmp_path)
    try:
        assert sys_.graph_store is None
        assert sys_.graph_extract is None
        assert any("graph_init_failed" in w for w in sys_.warnings)
        # 主系统仍然可用
        assert sys_.facade is not None
        assert sys_.consolidator is not None
    finally:
        await sys_.stop()
