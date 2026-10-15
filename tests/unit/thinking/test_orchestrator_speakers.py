"""ButlerOrchestrator speakers 字段支持(Phase 7.6 声纹扩展 — 2026-10-15)。

## 设计原则(2026-10-15 用户决策)

**每轮 ainvoke 一个 user 在说话**——单值 user_id 足够,不需要 user_ids 多值。
多 user 场景(TOTHINK 1.1/1.2)通过**多轮 ainvoke** 切换 user_id 表达。

声纹识别的真正用途是:
- VoiceAdapter 算声纹 embedding → event.speakers 写好
- ainvoke 接收 event.speakers → 落 HumanMessage.additional_kwargs["speakers"]
- SqliteSaver 持久化 → 后续 Sub-Agent / 记忆系统 / Phase 8 情绪治理消费
  (读 HumanMessage.additional_kwargs["speakers"] 拿本轮声纹结果)

## 测试范围

1. ainvoke(speakers=[...]) → HumanMessage.additional_kwargs["speakers"] 正确
2. ainvoke 不传 speakers → additional_kwargs 无 speakers 字段(向后兼容)
3. ainvoke(user_id=单值)行为零变化
4. astream 同 ainvoke

## 不测

- user_ids 多值(已删除,过度设计)
- _current_user_ids(已删除)
- ButlerState.user_ids(已删除)
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from smartbutler.events.core.event import SpeakerInfo
from smartbutler.thinking.loop.graph import ButlerGraphBuilder
from smartbutler.thinking.loop.orchestrator import ButlerOrchestrator


# ---------------------------------------------------------------------------
# Fake 设施
# ---------------------------------------------------------------------------


class _FakeBaseLLM:
    def __init__(self) -> None:
        self.closed = False

    async def chat(self, messages: list[Any], **kwargs: Any) -> Any:  # noqa: ARG002
        return None

    async def chat_stream(  # noqa: ARG002
        self, messages: list[Any], **kwargs: Any
    ) -> AsyncIterator[Any]:
        return
        yield  # type: ignore[unreachable]

    async def aclose(self) -> None:
        self.closed = True


class _FakeSettings:
    provider = "openai"
    backend = "http"
    model = "fake-model"
    temperature = 0.7
    max_tokens = 1024
    timeout = 30.0
    max_retries = 2
    openai_api_key = None
    openai_base_url = None
    anthropic_api_key = None
    anthropic_base_url = None
    ollama_base_url = None


class _FakeCompiledGraph:
    def __init__(self) -> None:
        self.invocations: list[dict[str, Any]] = []

    async def ainvoke(self, state: dict[str, Any], config: dict[str, Any] | None = None) -> dict[str, Any]:  # noqa: ARG002
        self.invocations.append({"state": state})
        new_messages = list(state["messages"]) + [AIMessage(content="ok")]
        return {**state, "messages": new_messages, "iteration_count": 1}

    async def astream(self, state: dict[str, Any], config: dict[str, Any] | None = None, stream_mode: str = "values"):  # noqa: ARG002
        self.invocations.append({"state": state})
        new_messages = list(state["messages"]) + [AIMessage(content="ok")]
        yield {**state, "messages": new_messages, "iteration_count": 1}


def _make_orchestrator() -> tuple[ButlerOrchestrator, _FakeCompiledGraph]:
    compiled = _FakeCompiledGraph()
    fake_graph_builder = ButlerGraphBuilder()
    orch = ButlerOrchestrator(
        llm=_FakeBaseLLM(),  # type: ignore[arg-type]
        llm_settings=_FakeSettings(),  # type: ignore[arg-type]
        graph_builder=fake_graph_builder,
    )
    orch._compiled = compiled  # type: ignore[attr-defined]
    return orch, compiled


# ---------------------------------------------------------------------------
# 7.6 speakers 落 HumanMessage.additional_kwargs
# ---------------------------------------------------------------------------


class TestAinvokeSpeakers:
    @pytest.mark.asyncio
    async def test_speakers_written_to_additional_kwargs(self) -> None:
        """speakers 参数 → HumanMessage.additional_kwargs["speakers"](落盘透传)。"""
        orch, compiled = _make_orchestrator()
        sp_dad = SpeakerInfo(user_id="dad", confidence=0.95, is_known=True)
        sp_mom = SpeakerInfo(user_id="mom", confidence=0.72, is_known=True)
        await orch.ainvoke("今天周几了", user_id="dad", speakers=[sp_dad, sp_mom])
        state = compiled.invocations[0]["state"]
        msg = state["messages"][0]
        assert isinstance(msg, HumanMessage)
        speakers_in_kwargs = msg.additional_kwargs.get("speakers")
        assert speakers_in_kwargs is not None
        assert len(speakers_in_kwargs) == 2
        assert speakers_in_kwargs[0]["user_id"] == "dad"
        assert speakers_in_kwargs[0]["confidence"] == 0.95
        assert speakers_in_kwargs[0]["is_known"] is True
        # user_id 仍是单值(向后兼容)
        assert state["user_id"] == "dad"

    @pytest.mark.asyncio
    async def test_no_speakers_means_no_field(self) -> None:
        """不传 speakers → additional_kwargs 没有 speakers 字段(向后兼容)。"""
        orch, compiled = _make_orchestrator()
        await orch.ainvoke("hi", user_id="gaotianyu")
        state = compiled.invocations[0]["state"]
        msg = state["messages"][0]
        assert isinstance(msg, HumanMessage)
        # 不应有 speakers 字段(节省落盘空间,老数据无此字段)
        assert "speakers" not in msg.additional_kwargs
        # user_id 仍是单值
        assert state["user_id"] == "gaotianyu"

    @pytest.mark.asyncio
    async def test_speakers_empty_list_omitted(self) -> None:
        """speakers=[] 空 list → 也当作"没传",不写字段(空值不污染)。"""
        orch, compiled = _make_orchestrator()
        await orch.ainvoke("hi", user_id="gaotianyu", speakers=[])
        state = compiled.invocations[0]["state"]
        msg = state["messages"][0]
        assert "speakers" not in msg.additional_kwargs


# ---------------------------------------------------------------------------
# astream 同样
# ---------------------------------------------------------------------------


class TestAstreamSpeakers:
    @pytest.mark.asyncio
    async def test_astream_speakers(self) -> None:
        """astream 也支持 speakers 参数,行为同 ainvoke。"""
        orch, compiled = _make_orchestrator()
        sp = SpeakerInfo(user_id="dad", confidence=0.95, is_known=True)
        async for _ in orch.astream("hi", user_id="dad", speakers=[sp]):
            pass
        state = compiled.invocations[0]["state"]
        msg = state["messages"][0]
        assert isinstance(msg, HumanMessage)
        speakers_in_kwargs = msg.additional_kwargs.get("speakers")
        assert speakers_in_kwargs is not None
        assert speakers_in_kwargs[0]["user_id"] == "dad"


# ---------------------------------------------------------------------------
# 向后兼容(老调用方)
# ---------------------------------------------------------------------------


class TestBackwardCompat:
    @pytest.mark.asyncio
    async def test_old_caller_no_speakers_unchanged(self) -> None:
        """老调用方只传 user_input → 行为完全不变。"""
        orch, compiled = _make_orchestrator()
        await orch.ainvoke("hi")
        state = compiled.invocations[0]["state"]
        assert state["user_id"] == "user"
        msg = state["messages"][0]
        assert "speakers" not in msg.additional_kwargs

    @pytest.mark.asyncio
    async def test_old_caller_user_id_only(self) -> None:
        """老调用方只传 user_id → 行为完全不变。"""
        orch, compiled = _make_orchestrator()
        await orch.ainvoke("hi", user_id="gaotianyu")
        state = compiled.invocations[0]["state"]
        assert state["user_id"] == "gaotianyu"
        msg = state["messages"][0]
        assert "speakers" not in msg.additional_kwargs
