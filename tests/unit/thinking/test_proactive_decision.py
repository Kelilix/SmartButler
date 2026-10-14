"""ProactiveDecision / ProactiveResult / ProactiveUrgency 单元测试。"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from smartbutler.thinking.proactive import (
    ProactiveDecision,
    ProactiveResult,
    ProactiveUrgency,
)


class TestProactiveUrgency:
    def test_values(self) -> None:
        assert ProactiveUrgency.LOW.value == "low"
        assert ProactiveUrgency.MED.value == "med"
        assert ProactiveUrgency.HIGH.value == "high"

    def test_is_str_enum(self) -> None:
        # StrEnum 兼容 str 比较
        assert ProactiveUrgency.HIGH == "high"
        assert ProactiveUrgency.LOW in ("low", "med")


class TestProactiveDecision:
    def test_default_construction(self) -> None:
        d = ProactiveDecision(
            should_respond=True,
            reason="user in diet",
            urgency=ProactiveUrgency.MED,
        )
        assert d.should_respond is True
        assert d.reason == "user in diet"
        assert d.urgency == ProactiveUrgency.MED
        assert d.suggested_tone == "温柔提醒"  # default
        assert d.context_hints == []  # default

    def test_silent_factory(self) -> None:
        d = ProactiveDecision.silent("default_silent")
        assert d.should_respond is False
        assert d.reason == "default_silent"
        assert d.urgency == ProactiveUrgency.LOW
        assert d.suggested_tone == "沉默"
        assert d.context_hints == []

    def test_frozen(self) -> None:
        d = ProactiveDecision(should_respond=True, reason="x")
        with pytest.raises(ValidationError):
            d.should_respond = False  # type: ignore[misc]

    def test_extra_forbid(self) -> None:
        with pytest.raises(ValidationError):
            ProactiveDecision(should_respond=True, reason="x", extra_field="nope")  # type: ignore[call-arg]

    def test_empty_reason_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ProactiveDecision(should_respond=True, reason="")


class TestProactiveResult:
    def test_silent_factory(self) -> None:
        r = ProactiveResult.silent("default_silent", related_event_id="evt-1")
        assert r.acted is False
        assert r.message is None
        assert r.push_channel is None
        assert r.silence_reason == "default_silent"
        assert r.urgency == ProactiveUrgency.LOW
        assert r.related_event_id == "evt-1"

    def test_acted_with_factory(self) -> None:
        r = ProactiveResult.acted_with(
            "建议:涮水后再吃",
            urgency=ProactiveUrgency.MED,
            related_event_id="evt-2",
        )
        assert r.acted is True
        assert r.message == "建议:涮水后再吃"
        assert r.urgency == ProactiveUrgency.MED
        assert r.silence_reason is None
        assert r.related_event_id == "evt-2"

    def test_acted_with_strips_whitespace(self) -> None:
        r = ProactiveResult.acted_with("  你好  ")
        assert r.message == "你好"

    def test_acted_with_rejects_empty(self) -> None:
        with pytest.raises(ValueError, match="不允许空 message"):
            ProactiveResult.acted_with("")
        with pytest.raises(ValueError, match="不允许空 message"):
            ProactiveResult.acted_with("   ")

    def test_frozen(self) -> None:
        r = ProactiveResult.silent("x")
        with pytest.raises(ValidationError):
            r.acted = True  # type: ignore[misc]

    def test_extra_forbid(self) -> None:
        with pytest.raises(ValidationError):
            ProactiveResult(acted=True, extra_field="nope")  # type: ignore[call-arg]
