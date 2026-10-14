"""ProactiveReasoning / RuleBasedProactiveReasoning 单元测试。"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from smartbutler.events.core.event import BaseEvent, EventPriority, EventSource
from smartbutler.thinking.proactive import ProactiveUrgency
from smartbutler.thinking.proactive.reasoning import RuleBasedProactiveReasoning


def _make_event(
    *,
    priority: EventPriority = EventPriority.NORMAL,
    source: EventSource = EventSource.DEVICE,
    topic: str = "device.door.opened",
    user_id: str = "alice",
) -> BaseEvent:
    return BaseEvent(
        source=source,
        topic=topic,
        user_id=user_id,
        timestamp=datetime.now(UTC),
        priority=priority,
    )


class TestRuleBasedSilentPriority:
    @pytest.mark.asyncio
    async def test_silent_priority_silences(self) -> None:
        reasoning = RuleBasedProactiveReasoning()
        e = _make_event(priority=EventPriority.SILENT)
        d = await reasoning.should_respond(e)
        assert d.should_respond is False
        assert d.reason == "event_priority_silent"


class TestRuleBasedUrgent:
    @pytest.mark.asyncio
    async def test_urgent_alarm_acted_high(self) -> None:
        reasoning = RuleBasedProactiveReasoning()
        e = _make_event(
            priority=EventPriority.URGENT, topic="device.smoke.alarm"
        )
        d = await reasoning.should_respond(e)
        assert d.should_respond is True
        assert d.urgency == ProactiveUrgency.HIGH
        assert d.suggested_tone == "紧急通知"

    @pytest.mark.asyncio
    async def test_urgent_non_alarm_acted_med(self) -> None:
        reasoning = RuleBasedProactiveReasoning()
        e = _make_event(
            priority=EventPriority.URGENT, topic="device.door.opened"
        )
        d = await reasoning.should_respond(e)
        assert d.should_respond is True
        assert d.urgency == ProactiveUrgency.MED

    @pytest.mark.asyncio
    async def test_urgent_leak_acted_high(self) -> None:
        reasoning = RuleBasedProactiveReasoning()
        e = _make_event(
            priority=EventPriority.URGENT, topic="device.water.leak_detected"
        )
        d = await reasoning.should_respond(e)
        assert d.should_respond is True
        assert d.urgency == ProactiveUrgency.HIGH


class TestRuleBasedDedup:
    @pytest.mark.asyncio
    async def test_first_event_default_silent(self) -> None:
        """NORMAL 优先级,首次出现 → 默认 silent。"""
        reasoning = RuleBasedProactiveReasoning()
        e = _make_event(priority=EventPriority.NORMAL)
        d = await reasoning.should_respond(e)
        assert d.should_respond is False
        assert d.reason == "default_silent"

    @pytest.mark.asyncio
    async def test_duplicate_within_window_silenced(self) -> None:
        """同 (user, topic) 在 5 分钟内重复 → silent。"""
        reasoning = RuleBasedProactiveReasoning()
        e1 = _make_event(priority=EventPriority.NORMAL)
        e2 = _make_event(priority=EventPriority.NORMAL)
        now = datetime.now(UTC)

        d1 = await reasoning.should_respond(e1, now=now)
        d2 = await reasoning.should_respond(e2, now=now + timedelta(seconds=60))

        assert d1.reason == "default_silent"
        assert d2.reason == "dedup_window"
        assert d2.should_respond is False

    @pytest.mark.asyncio
    async def test_different_topic_not_deduped(self) -> None:
        """不同 topic → 独立判定。"""
        reasoning = RuleBasedProactiveReasoning()
        e1 = _make_event(topic="device.door.opened")
        e2 = _make_event(topic="device.window.opened")
        now = datetime.now(UTC)

        d1 = await reasoning.should_respond(e1, now=now)
        d2 = await reasoning.should_respond(e2, now=now + timedelta(seconds=10))

        # 两者都 silent,但 reason 不同(各自首次)
        assert d1.reason == "default_silent"
        assert d2.reason == "default_silent"

    @pytest.mark.asyncio
    async def test_different_user_not_deduped(self) -> None:
        """不同 user_id → 独立判定。"""
        reasoning = RuleBasedProactiveReasoning()
        e1 = _make_event(user_id="alice")
        e2 = _make_event(user_id="bob")
        now = datetime.now(UTC)

        d1 = await reasoning.should_respond(e1, now=now)
        d2 = await reasoning.should_respond(e2, now=now + timedelta(seconds=10))

        assert d1.reason == "default_silent"
        assert d2.reason == "default_silent"

    @pytest.mark.asyncio
    async def test_outside_window_not_deduped(self) -> None:
        """超过 5 分钟窗口 → 不算重复。"""
        reasoning = RuleBasedProactiveReasoning()
        e1 = _make_event(priority=EventPriority.NORMAL)
        e2 = _make_event(priority=EventPriority.NORMAL)
        now = datetime.now(UTC)

        d1 = await reasoning.should_respond(e1, now=now)
        # 6 分钟后
        d2 = await reasoning.should_respond(e2, now=now + timedelta(seconds=400))

        assert d1.reason == "default_silent"
        assert d2.reason == "default_silent"  # 又是首次

    def test_reset_dedup_state(self) -> None:
        reasoning = RuleBasedProactiveReasoning()
        reasoning._seen[("alice", "test")] = datetime.now(UTC)  # noqa: SLF001
        assert len(reasoning._seen) == 1  # noqa: SLF001
        reasoning.reset_dedup_state()
        assert len(reasoning._seen) == 0  # noqa: SLF001
