"""MemoryHooks 单测(Phase 6.2 P0)。"""

from __future__ import annotations

import pytest

from smartbutler.emotion.memory.hooks import MemoryEventType, MemoryHooks


def test_hooks_constructor_registers_default_callbacks() -> None:
    """构造时自动注册 ON_STORE / ON_RECALL 的默认 stub。"""
    h = MemoryHooks()
    assert h.callback_count(MemoryEventType.ON_STORE) >= 1
    assert h.callback_count(MemoryEventType.ON_RECALL) >= 1
    # ON_PROMOTE / ON_HABIT_DETECTED 暂时无 callback(留口子)
    assert h.callback_count(MemoryEventType.ON_PROMOTE) == 0
    assert h.callback_count(MemoryEventType.ON_HABIT_DETECTED) == 0


def test_hooks_register_adds_callback() -> None:
    """register 增加 callback 数量。"""
    h = MemoryHooks()

    def cb(**kwargs) -> None:  # noqa: ARG001
        pass

    initial = h.callback_count(MemoryEventType.ON_STORE)
    h.register(MemoryEventType.ON_STORE, cb)
    assert h.callback_count(MemoryEventType.ON_STORE) == initial + 1


def test_hooks_register_non_callable_raises() -> None:
    """register 非 callable 抛 TypeError。"""
    h = MemoryHooks()
    with pytest.raises(TypeError):
        h.register(MemoryEventType.ON_STORE, "not callable")  # type: ignore[arg-type]


def test_hooks_trigger_calls_all_callbacks() -> None:
    """trigger 触发所有 callback,按注册顺序。"""
    h = MemoryHooks()
    calls: list[str] = []

    def cb_a(*args, **kwargs) -> None:  # noqa: ARG001
        calls.append("a")

    def cb_b(*args, **kwargs) -> None:  # noqa: ARG001
        calls.append("b")

    h.register(MemoryEventType.ON_PROMOTE, cb_a)
    h.register(MemoryEventType.ON_PROMOTE, cb_b)
    h.trigger(MemoryEventType.ON_PROMOTE, key="k", content="c")
    assert calls == ["a", "b"]


def test_hooks_trigger_isolates_exceptions() -> None:
    """trigger 中任一 callback 抛异常,不影响其他。"""
    h = MemoryHooks()
    calls: list[str] = []

    def cb_bad(*args, **kwargs) -> None:
        raise RuntimeError("boom")

    def cb_good(*args, **kwargs) -> None:
        calls.append("good")

    h.register(MemoryEventType.ON_PROMOTE, cb_bad)
    h.register(MemoryEventType.ON_PROMOTE, cb_good)
    # 不应抛异常
    h.trigger(MemoryEventType.ON_PROMOTE, key="k")
    assert calls == ["good"]


def test_hooks_trigger_empty_callbacks() -> None:
    """trigger 空 callback 列表不抛。"""
    h = MemoryHooks()
    # ON_PROMOTE 初始无 callback
    h.trigger(MemoryEventType.ON_PROMOTE, key="k")
