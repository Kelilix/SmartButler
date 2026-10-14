"""🆕 Phase 6.3+: AutoExtractHook 单测。

覆盖:
- 注册到 hooks 不抛
- 触发了 ON_TASK_COMPLETE 之后写 Qdrant 一条 raw fact(level=None)
- importance < min_importance 不写
- LLM/读 short_term 失败不崩
- task_id 不重复抽(idempotent 由 scheduler 保证,这里只验 hook 自身可重入)
- 空 thread_id / 空消息用 title 兜底
"""
from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from smartbutler.emotion.memory.auto_extract import AutoExtractHook
from smartbutler.emotion.memory.hooks import MemoryEventType, MemoryHooks
from smartbutler.emotion.memory.task_index import TaskIndex, TaskMessageRecord


# ------------------- Fixtures -------------------


@pytest.fixture
def hooks() -> MemoryHooks:
    return MemoryHooks()


@pytest.fixture
def facade() -> MagicMock:
    f = MagicMock()
    f.remember = AsyncMock(return_value="key-1")
    f.hooks = MemoryHooks()
    return f


@pytest.fixture
def short_term() -> MagicMock:
    s = MagicMock()
    # 模拟 get_async_saver 返回带 aget 的 saver
    saver = MagicMock()
    state_dict = {
        "channel_values": {
            "messages": [
                _msg("human", "我明天 8 点要去上海出差"),
                _msg("ai", "好的,已记录"),
            ]
        }
    }

    async def _aget(cfg: dict[str, Any]) -> dict[str, Any]:
        return state_dict

    saver.aget = _aget

    async def _get_async_saver() -> MagicMock:
        return saver

    s.get_async_saver = _get_async_saver
    return s


def _msg(role: str, content: str) -> Any:
    """构造一个 minimal message 形态。"""
    m = MagicMock()
    m.role = role
    m.content = content
    m.additional_kwargs = {}
    m.id = f"msg-{content[:8]}"
    return m


def make_short_term(user_msg: str) -> MagicMock:
    """工厂:造一个 short_term mock,user_msg 可定制。"""
    s = MagicMock()
    saver = MagicMock()
    state_dict = {
        "channel_values": {
            "messages": [
                _msg("human", user_msg),
                _msg("ai", "好的"),
            ]
        }
    }

    async def _aget(cfg: dict[str, Any]) -> dict[str, Any]:
        return state_dict

    saver.aget = _aget

    async def _get_async_saver() -> MagicMock:
        return saver

    s.get_async_saver = _get_async_saver
    return s


@pytest.fixture
def task_index(tmp_path: Path) -> TaskIndex:
    p = tmp_path / "tasks.db"
    return TaskIndex(p)


# ------------------- Tests -------------------


def test_register_does_not_raise(
    hooks: MemoryHooks, facade: MagicMock, short_term: MagicMock, task_index: TaskIndex
) -> None:
    """注册不抛 + ON_TASK_COMPLETE 出现在已注册事件里。"""
    h = AutoExtractHook(
        facade=facade, short_term=short_term, task_index=task_index, min_importance=0.3
    )
    h.register(hooks)
    # 至少要有一个 callback 注册到 ON_TASK_COMPLETE
    assert any(MemoryEventType.ON_TASK_COMPLETE in hooks._callbacks for _ in [0])  # type: ignore[attr-defined]


async def test_on_task_complete_writes_fact(
    hooks: MemoryHooks, facade: MagicMock, short_term: MagicMock, task_index: TaskIndex
) -> None:
    """完整路径:trigger → fire-and-forget → remember 调用。"""
    h = AutoExtractHook(
        facade=facade, short_term=short_term, task_index=task_index, min_importance=0.3
    )
    h.register(hooks)

    # Phase 6.4: auto_extract 按 task_id 从 task_index 读 user 消息,
    # 造一条 task "t1" 的 user 消息让它能拼进 content
    task_index.append_message(TaskMessageRecord(
        task_id="t1",
        message_id="m1",
        thread_id="thread-1",
        role="user",
        user_id="u-male",
        spoken_at="2026-01-01T00:00:00+00:00",
        content="我明天 8 点要去上海出差",
    ))

    # 触发 —— 内部会 create_task
    hooks.trigger(
        MemoryEventType.ON_TASK_COMPLETE,
        task_id="t1",
        category="booking",
        content="买咖啡",
        status="completed",
        thread_id="thread-1",
    )

    # 等待 fire-and-forget 任务完成
    await _drain()

    # 验证 facade.remember 被调
    assert facade.remember.await_count == 1
    call_kwargs = facade.remember.await_args.kwargs
    # content 应包含 user 消息
    # 注:Phase 6.4 起 _collect_user_content 改为按 task_id 从
    # task_index.task_messages 读(不再走 short_term),所以这里要给
    # task_index 造一条 user 消息,否则会回退到 title 兜底。
    assert "上海出差" in facade.remember.await_args.args[0]
    # importance >= min_importance
    assert call_kwargs["importance"] >= 0.3
    # tags 包含 task/category
    assert "task:t1" in call_kwargs["tags"]
    assert "category:booking" in call_kwargs["tags"]
    # extracted_count 累加
    assert h.extracted_count == 1
    assert h.skipped_low_importance == 0


async def test_low_importance_no_filter_when_min_importance_zero(
    hooks: MemoryHooks, task_index: TaskIndex
) -> None:
    """🆕 2026-10-13:``min_importance=0.0``(默认)→ 不门控,所有 fact 都写。

    背景:旧实现用 HeuristicScorer 启发式评分,<0.3 丢弃;新行为把门控
    阈值外置到 ``min_importance`` 字段(.env:``AUTO_EXTRACT_MIN_IMPORTANCE``),
    **0.0 = 不门控**(默认),>0 = 走启发式门控(老行为)。门控下放到
    consolidator 阶段。详见 auto_extract.py docstring。
    """
    facade = MagicMock()
    facade.remember = AsyncMock(return_value="k")
    facade.hooks = MemoryHooks()
    short_term = make_short_term("hi")
    h = AutoExtractHook(
        facade=facade, short_term=short_term, task_index=task_index,
        min_importance=0.0,  # 默认值,wiring 不传时也是 0
    )
    assert h._filter_enabled is False
    h.register(hooks)

    # 给 task_index 加一条短消息
    task_index.append_message(TaskMessageRecord(
        task_id="t1",
        message_id="m1",
        thread_id="thread-1",
        role="user",
        user_id="u-male",
        spoken_at="2026-01-01T00:00:00+00:00",
        content="hi",
    ))

    hooks.trigger(
        MemoryEventType.ON_TASK_COMPLETE,
        task_id="t1",
        category="chitchat",
        content="hi",
        status="chitchat_done",
        thread_id="thread-1",
    )
    await _drain()

    # min_importance=0 → 门控关闭,"hi" 也会写
    assert facade.remember.await_count == 1
    assert h.skipped_low_importance == 0
    assert h.extracted_count == 1
    # 写进去的 tags 应当带 user_id
    tags = facade.remember.await_args.kwargs["tags"]
    assert "user:u-male" in tags
    # importance 是默认 0.5
    assert facade.remember.await_args.kwargs["importance"] == 0.5


async def test_low_importance_filter_active_when_min_importance_positive(
    hooks: MemoryHooks, task_index: TaskIndex
) -> None:
    """``min_importance>0`` → HeuristicScorer 门控生效(老行为)。

    何时用:wiring 读 .env 看到 ``SMARTBUTLER_MEMORY_AUTO_EXTRACT_MIN_IMPORTANCE
    > 0`` 时直接把值传给 hook,作为回退到"老行为"的开关。
    """
    facade = MagicMock()
    facade.remember = AsyncMock()
    facade.hooks = MemoryHooks()
    short_term = make_short_term("hi")
    h = AutoExtractHook(
        facade=facade, short_term=short_term, task_index=task_index,
        min_importance=0.99,  # 显式打开门控
    )
    assert h._filter_enabled is True
    h.register(hooks)

    hooks.trigger(
        MemoryEventType.ON_TASK_COMPLETE,
        task_id="t1",
        category="chitchat",
        content="hi",
        status="chitchat_done",
        thread_id="thread-1",
    )
    await _drain()

    # 门控生效,没调 remember
    facade.remember.assert_not_awaited()
    assert h.skipped_low_importance == 1
    assert h.extracted_count == 0


async def test_empty_thread_falls_back_to_title(
    hooks: MemoryHooks, short_term: MagicMock, task_index: TaskIndex
) -> None:
    """thread_id 空时用 title 兜底。"""
    facade = MagicMock()
    facade.remember = AsyncMock(return_value="k")
    facade.hooks = MemoryHooks()
    h = AutoExtractHook(
        facade=facade, short_term=short_term, task_index=task_index, min_importance=0.1
    )
    h.register(hooks)

    hooks.trigger(
        MemoryEventType.ON_TASK_COMPLETE,
        task_id="t1",
        category="chitchat",
        content="用户打了个招呼",
        status="chitchat_done",
        thread_id="",  # 空
    )
    await _drain()

    assert facade.remember.await_count == 1
    # content 用 title 兜底
    assert "打了个招呼" in facade.remember.await_args.args[0]


async def test_remember_failure_does_not_crash(
    hooks: MemoryHooks, short_term: MagicMock, task_index: TaskIndex
) -> None:
    """remember 抛异常 → failed_count 累加,hook 自身不崩。"""
    facade = MagicMock()

    async def _boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("qdrant down")

    facade.remember = AsyncMock(side_effect=_boom)
    facade.hooks = MemoryHooks()
    h = AutoExtractHook(
        facade=facade, short_term=short_term, task_index=task_index, min_importance=0.1
    )
    h.register(hooks)

    hooks.trigger(
        MemoryEventType.ON_TASK_COMPLETE,
        task_id="t1",
        category="chitchat",
        content="打招呼",
        status="chitchat_done",
        thread_id="thread-1",
    )
    await _drain()

    assert h.failed_count == 1
    assert h.extracted_count == 0


async def test_short_term_failure_falls_back_to_title(
    hooks: MemoryHooks, task_index: TaskIndex
) -> None:
    """get_async_saver 抛异常 → 用 title 兜底,记 failed_count 不增。"""
    facade = MagicMock()
    facade.remember = AsyncMock(return_value="k")
    facade.hooks = MemoryHooks()
    short_term = MagicMock()

    async def _boom() -> MagicMock:
        raise RuntimeError("db locked")

    short_term.get_async_saver = _boom
    h = AutoExtractHook(
        facade=facade, short_term=short_term, task_index=task_index, min_importance=0.1
    )
    h.register(hooks)

    hooks.trigger(
        MemoryEventType.ON_TASK_COMPLETE,
        task_id="t1",
        category="chitchat",
        content="紧急事情",
        status="chitchat_done",
        thread_id="thread-x",
    )
    await _drain()

    # 走兜底路径,正常写
    assert facade.remember.await_count == 1
    assert "紧急事情" in facade.remember.await_args.args[0]
    assert h.failed_count == 0  # 兜底是正常的,失败计数不增


# ------------------- helpers -------------------


async def _drain() -> None:
    """让当前 loop 上所有 create_task 跑完。"""
    await asyncio.sleep(0)
    # 多等几轮避免 fire-and-forget 没排到
    for _ in range(5):
        await asyncio.sleep(0)
