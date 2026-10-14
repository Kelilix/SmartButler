"""MemoryFacade 短库切窗 N 值配置生效性测试(回归 2026-10-12)。

背景 bug
--------
``MemoryFacade.format_for_prompt`` 曾在内部写死::

    await view.build_window(..., n_prior_tasks=3, ...)

而 ``ShortTermView.build_window`` 的参数优先级是 **call-time > 构造参数**::

    n_prior = self._n_prior if n_prior_tasks is None else max(0, n_prior_tasks)

于是 facade 构造时收到的 ``MemorySettings.n_prior_tasks``(来自
``.env`` 的 ``SMARTBUTLER_MEMORY_N_PRIOR_TASKS``)被静默丢弃,
改配置不生效。``n_completed_tasks`` / ``n_chitchat_tasks`` 没被写死,
只有 ``n_prior_tasks`` 漏了。

本文件锁定:"facade 不再用 call-time 参数覆盖构造值"。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from smartbutler.capabilities.embedding import StubEmbedder
from smartbutler.emotion.memory.facade import MemoryContext, MemoryFacade
from smartbutler.emotion.memory.hooks import MemoryHooks
from smartbutler.emotion.memory.importance import ImportanceScorer
from smartbutler.emotion.memory.long_term import LongTermStore
from smartbutler.emotion.memory.retrieval import MemoryRetriever
from smartbutler.storage import QdrantStorage


class _RecordingView:
    """记录 build_window 收到的 call-time 参数的 fake view。

    只要 facade 往 build_window 传了 n_prior_tasks / n_completed_tasks /
    n_chitchat_tasks,这里就能抓到(修复前 n_prior_tasks=3 会被抓到)。
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def build_window(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        # 返回空 window,facade 拼出空 block,不影响断言
        from smartbutler.emotion.memory.short_term_view import TaskWindow

        return TaskWindow()


def _make_facade(tmp_path: Path, **kw: Any) -> tuple[MemoryFacade, _RecordingView]:
    """构造 facade + 注入 _RecordingView,返回 (facade, view)。"""
    q = QdrantStorage(path=tmp_path / "q", collection="t", vector_size=64)
    store = LongTermStore(q)
    embedder = StubEmbedder(dimension=64)
    retriever = MemoryRetriever(store, embedder, ImportanceScorer())
    f = MemoryFacade(retriever, MemoryHooks(), store, embedder, **kw)

    view = _RecordingView()
    f._short_term_view = view  # 直接注入,绕过懒构造
    return f, view


@pytest.mark.asyncio
async def test_format_for_prompt_does_not_override_n_prior(tmp_path: Path) -> None:
    """核心回归:facade 不再往 build_window 传 n_prior_tasks。

    修复前这里会看到 ``n_prior_tasks=3``(写死值),
    导致 .env 的 SMARTBUTLER_MEMORY_N_PRIOR_TASKS 永远不生效。
    """
    f, view = _make_facade(tmp_path, n_prior_tasks=7)

    await f.format_for_prompt(
        "随便问问", ctx=MemoryContext(user_id="u1", session_id="s1"), k=1,
    )

    assert view.calls, "build_window 未被调用,测试失效"
    for call in view.calls:
        assert "n_prior_tasks" not in call, (
            f"facade 仍在传 call-time n_prior_tasks={call['n_prior_tasks']},"
            "会覆盖构造参数(.env 配置不生效)"
        )


@pytest.mark.asyncio
async def test_format_for_prompt_does_not_override_n_completed(tmp_path: Path) -> None:
    """同理:n_completed_tasks 也不该被 call-time 覆盖。"""
    f, view = _make_facade(tmp_path, n_completed_tasks=5)

    await f.format_for_prompt(
        "随便问问", ctx=MemoryContext(user_id="u1", session_id="s1"), k=1,
    )

    assert view.calls, "build_window 未被调用,测试失效"
    for call in view.calls:
        assert "n_completed_tasks" not in call


@pytest.mark.asyncio
async def test_format_for_prompt_does_not_override_n_chitchat(tmp_path: Path) -> None:
    """同理:n_chitchat_tasks 也不该被 call-time 覆盖。"""
    f, view = _make_facade(tmp_path, n_chitchat_tasks=11)

    await f.format_for_prompt(
        "随便问问", ctx=MemoryContext(user_id="u1", session_id="s1"), k=1,
    )

    assert view.calls, "build_window 未被调用,测试失效"
    for call in view.calls:
        assert "n_chitchat_tasks" not in call


@pytest.mark.asyncio
async def test_format_for_prompt_still_passes_session_and_user(tmp_path: Path) -> None:
    """删掉硬编码参数后,session_id / user_id 仍要正常透传。

    防"改过头"——不能把必要的调用参数一起删掉。
    """
    f, view = _make_facade(tmp_path)

    await f.format_for_prompt(
        "hi", ctx=MemoryContext(user_id="u-42", session_id="s-42"), k=1,
    )

    assert view.calls, "build_window 未被调用,测试失效"
    call = view.calls[-1]
    assert call["current_session_id"] == "s-42"
    assert call["user_id"] == "u-42"
    assert "current_user_input" in call


@pytest.mark.asyncio
async def test_ensure_view_passes_constructor_values(tmp_path: Path) -> None:
    """_ensure_short_term_view 必须把 facade 的 N 值透给 ShortTermView。

    这是配置的最后一环:facade 存的值 → ShortTermView 的构造参数。
    """
    q = QdrantStorage(path=tmp_path / "q2", collection="t2", vector_size=64)
    store = LongTermStore(q)
    embedder = StubEmbedder(dimension=64)
    retriever = MemoryRetriever(store, embedder, ImportanceScorer())
    f = MemoryFacade(
        retriever, MemoryHooks(), store, embedder,
        n_prior_tasks=7, n_completed_tasks=5, n_chitchat_tasks=11,
    )

    captured: dict[str, Any] = {}

    class _FakeShortTerm:
        async def get_async_saver(self) -> Any:
            return object()

    def _factory() -> Any:
        return _FakeShortTerm()

    f.bind_short_term_factory(_factory)

    # 拦下 ShortTermView 构造,检查传入的 N 值
    import smartbutler.emotion.memory.short_term_view as stv_mod

    orig_init = stv_mod.ShortTermView.__init__

    def _spy_init(self: Any, **kwargs: Any) -> None:
        captured.update(kwargs)
        orig_init(self, **kwargs)

    stv_mod.ShortTermView.__init__ = _spy_init  # type: ignore[method-assign]
    try:
        view = await f._ensure_short_term_view()
    finally:
        stv_mod.ShortTermView.__init__ = orig_init  # type: ignore[method-assign]

    assert view is not None
    assert captured.get("n_prior_tasks") == 7, (
        f"n_prior_tasks 没透传到 ShortTermView:{captured.get('n_prior_tasks')}"
    )
    assert captured.get("n_completed_tasks") == 5
    assert captured.get("n_chitchat_tasks") == 11


# ---------------------------------------------------------------------------
# 端到端:构造参数 → build_window 实际取条数(不 mock,走真 ShortTermView)
# ---------------------------------------------------------------------------


class _NullSaver:
    """ShortTermView 老路径需要的最小 saver(这里只走 task_index 路径)。"""

    async def alist(self, *, config: Any = None, limit: int = 100) -> Any:  # noqa: ARG002
        return
        yield  # pragma: no cover - 空迭代器

    async def aget(self, config: Any) -> None:  # noqa: ARG002
        return None


def _mk_task(task_id: str, status: str, title: str, ts: str) -> Any:
    from smartbutler.emotion.memory.task_index import TaskRecord

    return TaskRecord(
        task_id=task_id, user_id="u1", category="other", title=title,
        status=status, start_ts=ts, end_ts=ts, summary="", source_thread="s1",
    )


def _ts(minute: int) -> str:
    from datetime import UTC, datetime, timedelta

    return (datetime.now(UTC) + timedelta(minutes=minute)).strftime(
        "%Y-%m-%dT%H:%M:%S+00:00"
    )


@pytest.mark.asyncio
async def test_e2e_paused_count_follows_facade_constructor(tmp_path: Path) -> None:
    """端到端:facade 构造的 n_prior_tasks 真正决定注入几条 paused 锚点。

    造 5 个 paused task,构造 facade 时 n_prior_tasks=5 →
    注入的"跨 task 锚点"段应有 5 条。
    修复前 facade 硬编码 3,这里只会得到 3 条。
    """
    from smartbutler.emotion.memory.task_index import TaskIndex
    from smartbutler.emotion.memory.short_term_view import ShortTermView

    idx = TaskIndex(tmp_path / "tasks.db")
    for i in range(5):
        idx.upsert_task(_mk_task(f"p{i}", "paused", f"暂停任务{i}", _ts(i)))

    q = QdrantStorage(path=tmp_path / "q3", collection="t3", vector_size=64)
    store = LongTermStore(q)
    embedder = StubEmbedder(dimension=64)
    retriever = MemoryRetriever(store, embedder, ImportanceScorer())
    f = MemoryFacade(retriever, MemoryHooks(), store, embedder, n_prior_tasks=5)

    # 用真 ShortTermView 走 task_index 路径(不走 saver)
    f._short_term_view = ShortTermView(
        saver=_NullSaver(), task_index=idx,
        n_prior_tasks=5, n_completed_tasks=3, n_chitchat_tasks=30,
    )

    block = await f.format_for_prompt(
        "随便问问", ctx=MemoryContext(user_id="u1", session_id="s1"), k=1,
    )
    # 有 ongoing 时 current 段会有内容;无 ongoing 时只剩锚点段。
    # 这里没有 ongoing task,所以 block 只应包含 5 条 paused 锚点。
    anchor_count = block.count("· 最后答复:")
    assert anchor_count == 5, f"期望 5 条 paused 锚点,实际 {anchor_count}:\n{block}"


@pytest.mark.asyncio
async def test_e2e_paused_count_respects_constructor_cap(tmp_path: Path) -> None:
    """反例:构造 n_prior_tasks=2 时,5 个 paused 只注入 2 条(上限生效)。"""
    from smartbutler.emotion.memory.task_index import TaskIndex
    from smartbutler.emotion.memory.short_term_view import ShortTermView

    idx = TaskIndex(tmp_path / "tasks4.db")
    for i in range(5):
        idx.upsert_task(_mk_task(f"p{i}", "paused", f"暂停任务{i}", _ts(i)))

    q = QdrantStorage(path=tmp_path / "q4", collection="t4", vector_size=64)
    store = LongTermStore(q)
    embedder = StubEmbedder(dimension=64)
    retriever = MemoryRetriever(store, embedder, ImportanceScorer())
    f = MemoryFacade(retriever, MemoryHooks(), store, embedder, n_prior_tasks=2)
    f._short_term_view = ShortTermView(
        saver=_NullSaver(), task_index=idx,
        n_prior_tasks=2, n_completed_tasks=3, n_chitchat_tasks=30,
    )

    block = await f.format_for_prompt(
        "随便问问", ctx=MemoryContext(user_id="u1", session_id="s1"), k=1,
    )
    anchor_count = block.count("· 最后答复:")
    assert anchor_count == 2, f"期望 2 条 paused 锚点,实际 {anchor_count}:\n{block}"