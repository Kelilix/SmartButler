"""tests/e2e/test_memory_thinking.py — thinking + Memory 端到端集成测试。

默认 skip；启用方式：
    pytest -m e2e tests/e2e/test_memory_thinking.py -v

本文件验证 Phase 6.2 P0 整条链路(方案 §5.2 验收项):

链路:
    1. 真实 QwenEmbedder 把"用户偏好"文本 → 1024 维向量
    2. 写入 QdrantStorage(嵌入式 path=tmp) + LongTermStore
    3. 构造 MemoryFacade(Embedder + Retriever + Qdrant)
    4. 注入到 ButlerOrchestrator(memory_facade=...)
    5. 真实 LLM(DeepSeek-Flash)走完整 graph:
       ainvoke("我明天想喝什么?")
       → decide_node 召回相关历史(包含"用户喜欢美式咖啡")
       → 拼到 system_prompt 的 `## 相关历史记忆` 段
       → LLM 看到后回答
    6. 验证回复里出现"咖啡"相关内容(说明 LLM 真的看到 memory_block)

依赖 .env 中的 LLM + Embedding 凭证；没有凭证会自动 skip。
"""
from __future__ import annotations

from pathlib import Path

import pytest
import structlog

from smartbutler.capabilities.embedding import (
    Embedder,
    EmbeddingSettings,
    QwenEmbedder,
    build_default_embedder,
    load_embedding_settings,
)
from smartbutler.capabilities.llm import create_llm
from smartbutler.config import load_llm_settings
from smartbutler.emotion.memory.facade import MemoryContext, MemoryFacade
from smartbutler.emotion.memory.hooks import MemoryHooks
from smartbutler.emotion.memory.importance import ImportanceScorer
from smartbutler.emotion.memory.long_term import LongTermStore
from smartbutler.emotion.memory.retrieval import MemoryRetriever
from smartbutler.storage import QdrantStorage
from smartbutler.thinking import ButlerOrchestrator

pytestmark = pytest.mark.e2e

log = structlog.get_logger()


# ---------------------------------------------------------------------------
# Skip helpers
# ---------------------------------------------------------------------------


def _require_llm_key() -> None:
    if not load_llm_settings().openai_api_key:
        pytest.skip("SMARTBUTLER_LLM_OPENAI_API_KEY 未配,跳过 e2e")


def _require_embed_key() -> None:
    if not load_embedding_settings().api_key:
        pytest.skip("SMARTBUTLER_EMBEDDING_API_KEY 未配,跳过 e2e")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def embedder() -> Embedder:
    """真实 QwenEmbedder(从 .env 读)。"""
    _require_embed_key()
    settings = load_embedding_settings(strict=True, qdrant_vector_size=1024)
    e = build_default_embedder(settings)
    yield e
    if hasattr(e, "close"):
        e.close()  # type: ignore[attr-defined]


@pytest.fixture
def long_term_store(tmp_path: Path) -> LongTermStore:
    """真实 QdrantStorage(嵌入式 tmp path,dim=1024 跟 embedding 配套)。"""
    q = QdrantStorage(
        path=tmp_path / "qdrant_e2e",
        collection="e2e_memory",
        vector_size=1024,
    )
    return LongTermStore(q)


@pytest.fixture
def memory_facade(
    long_term_store: LongTermStore, embedder: Embedder
) -> MemoryFacade:
    """完整 MemoryFacade:真实 Embedder + 真实 Qdrant + 启发式评分。"""
    hooks = MemoryHooks()
    retriever = MemoryRetriever(
        store=long_term_store,
        embedder=embedder,
        scorer=ImportanceScorer(),
    )
    return MemoryFacade(
        retriever=retriever,
        hooks=hooks,
        long_term=long_term_store,
        embedder=embedder,
    )


@pytest.fixture
async def orchestrator_with_memory(memory_facade: MemoryFacade):
    """ButlerOrchestrator 已注入 memory_facade(走真实 LLM)。"""
    _require_llm_key()
    settings = load_llm_settings()
    llm = create_llm(settings)
    try:
        orch = ButlerOrchestrator(
            llm=llm,
            llm_settings=settings,
            memory_facade=memory_facade,
        )
        yield orch
    finally:
        await llm.aclose()


# ---------------------------------------------------------------------------
# 1. 链路预检:配置 + Embedder + Qdrant 三件套能跑
# ---------------------------------------------------------------------------


def test_embedding_settings_match_qdrant() -> None:
    """EmbeddingSettings.dimension == 1024(跟 .env 中的 qdrant_vector_size 一致)。

    启动期校验,防止 Qdrant 集合已建 dim=1024 但 embedding 返回 1024 以外的维度。
    """
    _require_embed_key()
    s = load_embedding_settings(strict=True, qdrant_vector_size=1024)
    assert s.dimension == 1024


@pytest.mark.asyncio
async def test_embedder_real_and_matches_dimension(embedder: Embedder) -> None:
    """Embedder.dimension == 1024 且能真出 1024 维向量。"""
    v = await embedder.embed("dim test")
    assert len(v) == 1024
    assert v != [0.0] * 1024


# ---------------------------------------------------------------------------
# 2. facade 真实链路:remember → recall
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_facade_remember_then_recall_real(
    memory_facade: MemoryFacade,
) -> None:
    """真实 Embedder + 真实 Qdrant:remember 一条,recall 能命中。"""
    ctx = MemoryContext(user_id="e2e-user", session_id="e2e-sess")
    await memory_facade.remember(
        "用户喜欢喝美式咖啡,不喜欢加糖",
        ctx=ctx,
        tags=["preference"],
    )
    hits = await memory_facade.recall(
        "用户喜欢什么饮料?",
        ctx=ctx,
        k=3,
        min_importance=0.0,
    )
    assert hits, "real Qdrant + Qwen embed 召回为空"
    assert any("咖啡" in h["content"] for h in hits), (
        f"召回内容不含 '咖啡': {[h['content'] for h in hits]}"
    )


@pytest.mark.asyncio
async def test_facade_format_for_prompt_real(
    memory_facade: MemoryFacade,
) -> None:
    """format_for_prompt 返回拼好的 markdown 片段(真实链路)。"""
    ctx = MemoryContext(user_id="e2e-user", session_id="e2e-sess")
    await memory_facade.remember("用户住在上海浦东", ctx=ctx)
    await memory_facade.remember("用户喜欢喝拿铁", ctx=ctx)
    block = await memory_facade.format_for_prompt(
        "用户住在哪里?", ctx=ctx, k=5, max_chars=2000,
    )
    assert "### 相关历史记忆" in block
    assert "上海" in block or "拿铁" in block


# ---------------------------------------------------------------------------
# 3. 端到端:真实 LLM 看到 memory_block
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_thinking_ainvoke_uses_recalled_memory(
    orchestrator_with_memory: ButlerOrchestrator,
    memory_facade: MemoryFacade,
) -> None:
    """端到端:先 remember 一条偏好,再 ainvoke 问相关问题,验证 LLM 真的看到。

    这是 Phase 6.2 P0 最重要的 e2e: 验证方案 §5.2 验收项
    "模拟 1 轮对话,验证 LLM 收到的 system_prompt 含 `## 相关历史记忆` 段"。

    策略:
    - 在同一个 session_id 下,先 remember("用户喜欢喝美式咖啡,每天早上 1 杯")
    - ainvoke("我明天早上想喝点什么,有什么建议吗?")
    - 期望 LLM 回复里出现"咖啡"或"美式"相关内容(说明它看到了历史)
    """
    ctx = MemoryContext(user_id="e2e-mem", session_id="e2e-thinking-sess")

    # 1. 先存一条偏好
    await memory_facade.remember(
        "用户喜欢喝美式咖啡,每天早上 1 杯,不加糖",
        ctx=ctx,
    )

    # 2. ainvoke 问相关问题
    answer = await orchestrator_with_memory.ainvoke(
        "我明天早上想喝点什么,有什么建议吗?",
        user_id="e2e-mem",
        session_id="e2e-thinking-sess",
    )
    log.info(
        "thinking_memory_answer",
        answer=answer,
        answer_len=len(answer),
    )

    # 3. 断言:LLM 看到了 memory,提到"美式"或"咖啡"或"不加糖"
    #    (不严格匹配"咖啡"二字,防止 LLM 偶尔用"美式咖啡"/"冰美式"等变体时漏判;
    #     "美式" 和 "不加糖" 都是从 memory_block 里来的强信号)
    assert isinstance(answer, str) and len(answer) > 0
    has_coffee_signal = (
        "美式" in answer
        or "咖啡" in answer
        or "不加糖" in answer
    )
    assert has_coffee_signal, (
        f"LLM 回复里没出现'美式'/'咖啡'/'不加糖',说明可能没看到 memory_block。\n"
        f"answer={answer!r}"
    )
    assert "[ToolError]" not in answer
    assert "[AgentError]" not in answer
    log.info(
        "memory_block_verified",
        detected_signals={
            k: (k in answer) for k in ("美式", "咖啡", "不加糖", "每天早上")
        },
    )


@pytest.mark.asyncio
async def test_thinking_ainvoke_no_relevant_memory_no_crash(
    orchestrator_with_memory: ButlerOrchestrator,
) -> None:
    """没有任何历史时,ainvoke 仍能正常工作(空 memory_block 不崩)。"""
    answer = await orchestrator_with_memory.ainvoke(
        "你好,请用一句话介绍自己。",
        user_id="e2e-mem-empty",
        session_id="e2e-empty-sess",
    )
    assert isinstance(answer, str) and len(answer) > 0
    assert "[ToolError]" not in answer
    assert "[AgentError]" not in answer


# ---------------------------------------------------------------------------
# 4. 跨 session 隔离(用 source 隔离)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_thinking_source_isolation_real(
    orchestrator_with_memory: ButlerOrchestrator,
    memory_facade: MemoryFacade,
) -> None:
    """真实链路下,source A 的记忆不会在 source B 的召回中出现。"""
    # Alice 存一条
    ctx_a = MemoryContext(
        user_id="alice", session_id="e2e-sess", source="alice",
    )
    await memory_facade.remember("Alice 养了一只猫", ctx=ctx_a)

    # Bob 召回 → 不应看到 Alice 的
    ctx_b = MemoryContext(
        user_id="bob", session_id="e2e-sess", source="bob",
    )
    hits = await memory_facade.recall(
        "养了什么宠物?", ctx=ctx_b, k=5, min_importance=0.0,
    )
    assert not any("Alice" in h["content"] for h in hits), (
        f"Bob 召回了 Alice 的记忆(隔离失败): {[h['content'] for h in hits]}"
    )

    # Alice 召回 → 应当看到自己的
    hits_a = await memory_facade.recall(
        "养了什么宠物?", ctx=ctx_a, k=5, min_importance=0.0,
    )
    assert any("Alice" in h["content"] for h in hits_a), (
        f"Alice 召不到自己的记忆: {[h['content'] for h in hits_a]}"
    )


# ---------------------------------------------------------------------------
# 5. 短期记忆真持久化(Phase 6.2 P0 修复)
# ---------------------------------------------------------------------------


def test_short_term_saver_is_persistent_sqlite(tmp_path: Path) -> None:
    """ShortTermMemory.get_sync_saver() 返回的是 SqliteSaver,落盘到 db 文件。

    验证:
    - 不再是 InMemorySaver(进程内)
    - db 文件被创建(SqliteSaver 不会立即建表,size=0 也算 OK;
      LangGraph 第一次 ainvoke 时才 setup)
    """
    from langgraph.checkpoint.sqlite import SqliteSaver
    from langgraph.checkpoint.memory import InMemorySaver

    from smartbutler.emotion.memory.short_term import ShortTermMemory

    stm = ShortTermMemory(tmp_path / "short_term_e2e.db")
    saver = stm.get_sync_saver()

    # 类型断言
    assert isinstance(saver, SqliteSaver), (
        f"短期记忆应落 SqliteSaver,实际得到 {type(saver).__name__}"
    )
    assert not isinstance(saver, InMemorySaver)

    # 落盘断言:文件被创建
    db_file = tmp_path / "short_term_e2e.db"
    assert db_file.exists(), "短期记忆 SQLite 文件未创建"


def test_short_term_saver_survives_restart(tmp_path: Path) -> None:
    """验证 Phase 6.2 P0 修复目标:重启进程不丢短期上下文。

    流程:
    1. 第一个 ShortTermMemory 实例,拿 saver,写一个 checkpoint
    2. 第二个 ShortTermMemory 实例(新对象),拿 saver,读 checkpoint 应能命中

    SqliteSaver 内部会自动 setup,调用方不用手动建表。
    """
    from langgraph.checkpoint.sqlite import SqliteSaver
    from langgraph.checkpoint.memory import InMemorySaver

    from smartbutler.emotion.memory.short_term import ShortTermMemory

    db_path = tmp_path / "short_term_restart.db"

    # ---- 第一轮:写 ----
    stm1 = ShortTermMemory(db_path)
    saver1 = stm1.get_sync_saver()
    assert isinstance(saver1, SqliteSaver)
    assert not isinstance(saver1, InMemorySaver)

    # 构造一个最小 Checkpoint 写入
    from langgraph.checkpoint.base import Checkpoint
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

    serde = JsonPlusSerializer()
    config = {"configurable": {"thread_id": "test-restart-thread", "checkpoint_ns": ""}}
    checkpoint = Checkpoint(
        v=1,
        ts="2026-10-10T10:00:00.000000+00:00",
        channel_values={"messages": ["hello", "world"]},
        channel_versions={"messages": "1"},
        versions_seen={},
        pending_sends=[],
        id="ckpt-001",
    )
    saved_config = saver1.put(
        config,
        checkpoint,
        {"source": "test", "step": 0, "parents": {}},
        [],
    )
    assert saved_config is not None
    # 注:不显式 close,LangGraph SqliteSaver 在进程退出时 GC 关闭

    # ---- 第二轮:新进程读(模拟重启:新建 ShortTermMemory 实例)----
    stm2 = ShortTermMemory(db_path)
    saver2 = stm2.get_sync_saver()
    assert isinstance(saver2, SqliteSaver)
    assert not isinstance(saver2, InMemorySaver)

    # 读 checkpoint
    loaded = saver2.get(
        {"configurable": {"thread_id": "test-restart-thread", "checkpoint_ns": ""}}
    )
    assert loaded is not None, "重启后读不到 checkpoint(短期记忆没真持久化)"
    assert loaded["id"] == "ckpt-001"
    assert loaded["channel_values"]["messages"] == ["hello", "world"]


@pytest.mark.asyncio
async def test_orchestrator_uses_short_term_persistent_saver(tmp_path: Path) -> None:
    """注入 short_term 后,orchestrator._ensure_graph 真的把 SqliteSaver 接到了 graph。"""
    from langgraph.checkpoint.sqlite import SqliteSaver
    from langgraph.checkpoint.memory import InMemorySaver

    from smartbutler.emotion.memory.short_term import ShortTermMemory
    from smartbutler.capabilities.llm import create_llm
    from smartbutler.config import load_llm_settings
    from smartbutler.thinking import ButlerOrchestrator

    _require_llm_key()
    settings = load_llm_settings()
    llm = create_llm(settings)
    try:
        stm = ShortTermMemory(tmp_path / "short_term_orch.db")
        orch = ButlerOrchestrator(
            llm=llm,
            llm_settings=settings,
            short_term=stm,
        )
        # 触发 _ensure_graph 走真实 graph 编译
        # (这里用 patched tools 避免真实 LLM 调用耗时)
        from unittest.mock import patch
        with patch.object(orch, "_collect_tools", return_value=[]):
            compiled = orch._ensure_graph()  # type: ignore[attr-defined]
        # 验证
        assert compiled is not None
        assert orch._short_term_saver_cache is not None  # type: ignore[attr-defined]
        assert isinstance(orch._short_term_saver_cache, SqliteSaver)  # type: ignore[attr-defined]
        assert not isinstance(orch._short_term_saver_cache, InMemorySaver)  # type: ignore[attr-defined]
    finally:
        await llm.aclose()
