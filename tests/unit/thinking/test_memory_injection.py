"""🆕 2026-11-02: 记忆系统注入链路测试。

验证 **装配 → 注入 → thinking 层真的用上了** 这条链路:

1. :meth:`ButlerOrchestrator.attach_memory_system` 把 facade / short_term
   接进 thinking 层
2. :func:`voice_service._build_memory_system` 能装配出系统并带上显示名解析
3. 装配失败时**降级**而不是崩溃(记忆是增强能力,不该拖垮语音服务)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from smartbutler.thinking.loop.orchestrator import ButlerOrchestrator


class _FakeLLM:
    """占位 LLM(attach_memory_system 不需要真的调 LLM)。"""

    def chat(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    async def aclose(self) -> None:
        return None


def _make_orchestrator() -> ButlerOrchestrator:
    return ButlerOrchestrator(llm=_FakeLLM())  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 1. attach_memory_system
# ---------------------------------------------------------------------------


def test_attach_memory_system_wires_facade_and_short_term() -> None:
    """注入记忆系统 → facade / short_term 都接进 thinking 层。"""
    orch = _make_orchestrator()
    facade = MagicMock()
    short_term = MagicMock()
    system = MagicMock()
    system.facade = facade
    system.short_term = short_term

    orch.attach_memory_system(system)

    assert orch._memory_facade is facade
    assert orch._short_term is short_term
    assert orch._memory_system is system
    # system prompt 需要重建(记忆块要重新注入)
    assert orch._compiled is None


def test_attach_memory_system_with_none_is_noop() -> None:
    """注入 None 不报错,也不改变现状。"""
    orch = _make_orchestrator()
    before = orch._memory_facade
    orch.attach_memory_system(None)
    assert orch._memory_facade is before


def test_memory_system_property_exposes_handle() -> None:
    """公开访问器暴露记忆系统句柄(供 server 优雅关停)。"""
    orch = _make_orchestrator()
    assert orch.memory_system is None
    system = MagicMock()
    orch.attach_memory_system(system)
    assert orch.memory_system is system


@pytest.mark.asyncio
async def test_server_stop_stops_memory_system() -> None:
    """server 关停时会停掉记忆系统(停掉 task judge 后台调度器)。"""
    from smartbutler.server.runtime import ButlerServer, ServerConfig

    class _FakeMemory:
        def __init__(self) -> None:
            self.stopped = False

        async def stop(self) -> None:
            self.stopped = True

    class _FakeOrch:
        def __init__(self) -> None:
            self.memory_system = _FakeMemory()

    orch = _FakeOrch()
    server = ButlerServer(orchestrator=orch, config=ServerConfig())  # type: ignore[arg-type]
    # 模拟已启动
    server._started = True

    await server.stop()

    assert orch.memory_system.stopped, "server 关停应停掉记忆系统"


# ---------------------------------------------------------------------------
# 2. voice_service 装配
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_memory_system_in_voice_service(tmp_path: Path) -> None:
    """voice_service 的装配函数能产出可用的记忆系统。"""
    from smartbutler.scripts.voice_service import _build_memory_system

    registry = MagicMock()
    registry.list_members = AsyncMock(return_value=[
        MagicMock(user_id="u_dad", display_name="爸爸"),
        MagicMock(user_id="u_mom", display_name="妈妈"),
    ])

    system = await _build_memory_system(None, registry)
    if system is None:
        pytest.skip("记忆系统装配不可用(环境依赖)")

    assert system.facade is not None
    assert system.short_term is not None
    # 显示名解析器已绑定:user_id → 姓名
    assert system.facade._display_name("u_dad") == "爸爸"
    assert system.facade._display_name("u_mom") == "妈妈"
    # 未知 user_id 回退到原值
    assert system.facade._display_name("u_x") == "u_x"

    await system.stop()


@pytest.mark.asyncio
async def test_build_memory_system_degrades_on_failure(monkeypatch) -> None:
    """装配失败 → 返回 None,不抛异常(记忆挂不该拖垮语音服务)。"""
    from smartbutler.emotion.memory import wiring
    from smartbutler.scripts import voice_service

    # 让 wiring 的装配函数抛异常
    async def _boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("qdrant down")

    monkeypatch.setattr(wiring, "build_memory_system", _boom)

    # voice_service 的包装层必须把异常吞掉,返回 None
    result = await voice_service._build_memory_system(None, None)
    assert result is None, "装配失败应降级为 None,而不是抛异常"
