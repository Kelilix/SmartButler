"""协议字段纯数据结构测试（Phase 4-6 阶段就位）。

本测试**不依赖**任何外部服务 / LLM / Redis / 设备。
只验证协议的 Pydantic 字段约束和派生方法,确保 Phase 7 启动时协议已经稳定。

Phase 7 启动后,会新增:
- tests/test_event_bus.py          (Redis Streams 集成测试)
- tests/test_normalizer.py        (归一化规则测试)
- tests/test_trigger.py           (触发器测试)
- tests/test_homeassistant_adapter.py
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from smartbutler.events.core.event import (
    BaseEvent,
    EventPriority,
    EventSource,
)
from smartbutler.events.protocol.device_event import (
    DeviceEvent,
    DeviceType,
)
from smartbutler.events.protocol.timer_event import (
    TimerEvent,
    TimerType,
)
from smartbutler.events.protocol.voice_event import (
    VoiceAction,
    VoiceEvent,
)


# === BaseEvent ===

class TestBaseEvent:
    def test_minimal_valid_event(self) -> None:
        """最小必填字段能构造成功。"""
        e = BaseEvent(
            source=EventSource.DEVICE,
            topic="device.door.opened",
            user_id="gaotianyu",
            timestamp=datetime(2026, 10, 8, 18, 45, tzinfo=ZoneInfo("Asia/Shanghai")),
        )
        assert e.event_id.startswith("evt_")
        assert e.priority == EventPriority.NORMAL
        assert e.payload == {}

    def test_event_id_is_unique_across_instances(self) -> None:
        """两个事件的 event_id 必须不同。"""
        common = {
            "source": EventSource.DEVICE,
            "topic": "device.door.opened",
            "user_id": "u",
            "timestamp": datetime.now(ZoneInfo("Asia/Shanghai")),
        }
        e1 = BaseEvent(**common)
        e2 = BaseEvent(**common)
        assert e1.event_id != e2.event_id

    def test_event_is_frozen(self) -> None:
        """事件不可变(修改任何字段报错)。"""
        e = BaseEvent(
            source=EventSource.DEVICE,
            topic="device.door.opened",
            user_id="u",
            timestamp=datetime.now(ZoneInfo("Asia/Shanghai")),
        )
        with pytest.raises(ValidationError):
            e.user_id = "hacker"  # type: ignore[misc]

    def test_extra_field_rejected(self) -> None:
        """未声明字段被拒(extra='forbid')。"""
        with pytest.raises(ValidationError):
            BaseEvent(
                source=EventSource.DEVICE,
                topic="device.door.opened",
                user_id="u",
                timestamp=datetime.now(ZoneInfo("Asia/Shanghai")),
                unknown_field="oops",  # type: ignore[call-arg]
            )

    def test_parent_agent_tag_derivation(self) -> None:
        """parent_agent_tag 派生正确。"""
        e = BaseEvent(
            source=EventSource.DEVICE,
            topic="device.door.opened",
            user_id="u",
            timestamp=datetime.now(ZoneInfo("Asia/Shanghai")),
        )
        assert e.parent_agent_tag == "trigger:device.door"

    def test_parent_agent_tag_fallback_when_short_topic(self) -> None:
        """topic 不足 2 段时,fallback 到 source。"""
        e = BaseEvent(
            source=EventSource.WEBHOOK,
            topic="ping",
            user_id="u",
            timestamp=datetime.now(ZoneInfo("Asia/Shanghai")),
        )
        assert e.parent_agent_tag == "trigger:webhook"

    def test_to_user_input_includes_topic_user_id(self) -> None:
        """user_input 文本包含关键字段。"""
        e = BaseEvent(
            source=EventSource.DEVICE,
            topic="device.door.opened",
            user_id="gaotianyu",
            device_id="door_front_01",
            timestamp=datetime(2026, 10, 8, 18, 45, tzinfo=ZoneInfo("Asia/Shanghai")),
        )
        text = e.to_user_input()
        assert "[系统事件]" in text
        assert "device.door.opened" in text
        assert "gaotianyu" in text
        assert "door_front_01" in text


# === DeviceEvent ===

class TestDeviceEvent:
    def test_make_factory_builds_correct_topic(self) -> None:
        """make() 工厂自动填 topic。"""
        e = DeviceEvent.make(
            device_type=DeviceType.DOOR,
            device_id="door_front_01",
            action="opened",
            user_id="u",
        )
        assert e.topic == "device.door.opened"
        assert e.source == EventSource.DEVICE

    def test_make_requires_action(self) -> None:
        """action 必填。"""
        from pydantic import ValidationError as PydErr

        with pytest.raises((TypeError, PydErr)):
            DeviceEvent.make(  # type: ignore[call-arg]
                device_type=DeviceType.DOOR,
                device_id="d",
                user_id="u",
            )

    def test_metrics_optional(self) -> None:
        """metrics 字段可空。"""
        e = DeviceEvent.make(
            device_type=DeviceType.DOOR,
            device_id="d",
            action="opened",
            user_id="u",
        )
        assert e.metrics is None

    def test_to_user_input_includes_device_type_action(self) -> None:
        """to_user_input 含设备类型 + 动作。"""
        e = DeviceEvent.make(
            device_type=DeviceType.WATER_HEATER,
            device_id="wh_01",
            action="reached_temp",
            user_id="u",
            metrics={"temperature": 65.0},
        )
        text = e.to_user_input()
        assert "water_heater" in text
        assert "reached_temp" in text
        assert "65.0" in text

    def test_urgent_priority_preserved(self) -> None:
        """URGENT 优先级正确传递。"""
        e = DeviceEvent.make(
            device_type=DeviceType.SMOKE_DETECTOR,
            device_id="sd_01",
            action="alarm",
            user_id="u",
            priority=EventPriority.URGENT,
        )
        assert e.priority == EventPriority.URGENT


# === VoiceEvent ===

class TestVoiceEvent:
    def test_command_event_asr_text_used_as_user_input(self) -> None:
        """COMMAND 类事件,asr_text 直接作为 user_input(关键路径)。"""
        e = VoiceEvent.make(
            action=VoiceAction.COMMAND,
            device_id="speaker_lr",
            user_id="u",
            asr_text="帮我打开电热水器",
        )
        assert e.to_user_input() == "帮我打开电热水器"

    def test_wake_word_event_no_asr_text(self) -> None:
        """WAKE_WORD 类事件走标准格式。"""
        e = VoiceEvent.make(
            action=VoiceAction.WAKE_WORD,
            device_id="speaker_lr",
            user_id="u",
            wake_word="管家",
        )
        text = e.to_user_input()
        assert "[系统事件]" in text
        assert "管家" in text

    def test_asr_confidence_bounds(self) -> None:
        """asr_confidence 必须在 [0, 1]。"""
        from pydantic import ValidationError as PydErr

        with pytest.raises(PydErr):
            VoiceEvent.make(
                action=VoiceAction.COMMAND,
                device_id="s",
                user_id="u",
                asr_text="x",
                asr_confidence=1.5,  # 超出范围
            )


# === TimerEvent ===

class TestTimerEvent:
    def test_make_factory(self) -> None:
        """定时器工厂方法。"""
        now = datetime(2026, 10, 8, 7, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        e = TimerEvent.make(
            timer_id="cron_wakeup_001",
            timer_type=TimerType.CRON,
            fire_at=now,
            actual_fire_at=now,
            user_id="u",
            user_message="早,7 点了,该起床了",
            recurrence="0 7 * * *",
        )
        assert e.topic == "timer.cron"
        assert e.source == EventSource.TIMER
        assert e.recurrence == "0 7 * * *"

    def test_to_user_input_uses_user_message(self) -> None:
        """to_user_input 把 user_message 放在主位。"""
        now = datetime(2026, 10, 8, 7, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        e = TimerEvent.make(
            timer_id="t1",
            timer_type=TimerType.REMINDER,
            fire_at=now,
            actual_fire_at=now,
            user_id="u",
            user_message="该吃药了",
        )
        text = e.to_user_input()
        assert "该吃药了" in text
        assert "reminder" in text

    def test_actual_fire_at_required(self) -> None:
        """实际触发时间必填(否则延迟检测不到)。"""
        from pydantic import ValidationError as PydErr

        with pytest.raises(PydErr):
            TimerEvent(
                source=EventSource.TIMER,
                topic="timer.cron",
                user_id="u",
                timestamp=datetime.now(ZoneInfo("Asia/Shanghai")),
                timer_id="t1",
                timer_type=TimerType.CRON,
                fire_at=datetime.now(ZoneInfo("Asia/Shanghai")),
                user_message="x",
                # 缺 actual_fire_at
            )


# === 协议稳定性回归 ===

class TestProtocolStability:
    """Phase 7 启动时回看:这些字段必须没变。"""

    def test_base_event_required_fields(self) -> None:
        """BaseEvent 必填字段(7 个)。"""
        e = BaseEvent(
            source=EventSource.DEVICE,
            topic="t",
            user_id="u",
            timestamp=datetime.now(ZoneInfo("Asia/Shanghai")),
        )
        for field in ("event_id", "source", "topic", "user_id", "timestamp", "priority", "payload"):
            assert hasattr(e, field), f"BaseEvent 必填字段 {field} 缺失"

    def test_device_event_required_fields(self) -> None:
        """DeviceEvent 必填字段(3 个独有 + 继承 7 个)。"""
        e = DeviceEvent(
            source=EventSource.DEVICE,
            topic="device.door.opened",
            user_id="u",
            timestamp=datetime.now(ZoneInfo("Asia/Shanghai")),
            device_type=DeviceType.DOOR,
            action="opened",
            device_id="d",
        )
        for field in ("device_type", "action", "current_state", "device_id"):
            assert hasattr(e, field), f"DeviceEvent 字段 {field} 缺失"
