"""事件基类与协议字段（Phase 7）。

## 设计原则

1. **不可变**：事件一旦产生,内容不可改;Normalizer 如需改 → 产生新事件
2. **强类型**：所有字段用 Pydantic 校验,设备 adapter 入参错误立即报错
3. **协议稳定**：BaseEvent 字段一旦敲定,Phase 7 启动后**不删不改**（详见 ADR-008 §5）

## 字段语义

| 字段 | 含义 | 必填 | 示例 |
|------|------|------|------|
| ``event_id`` | 事件唯一 ID（用于 dedup） | ✅ | "evt_20261008_184501_abc123" |
| ``source`` | 事件来源分类 | ✅ | EventSource.DEVICE |
| ``topic`` | 事件总线 topic | ✅ | "device.door.opened" |
| ``user_id`` | 关联用户（设备主） | ✅ | "gaotianyu" |
| ``device_id`` | 设备 ID | 条件 | "door_front_01" |
| ``timestamp`` | 事件时间（设备时间,非接收时间） | ✅ | datetime(2026, 10, 8, 18, 45) |
| ``priority`` | 优先级（路由策略） | ✅ | EventPriority.NORMAL |
| ``payload`` | 业务负载（设备 adapter 自由定义） | ✅ | {"open_method": "fingerprint"} |
| ``parent_event_id`` | 联动事件链（热水器在门开 5 分钟前启动） | 否 | "evt_20261008_184001_xyz" |
| ``correlation_id`` | 关联 ID（多事件描述同一业务） | 否 | "session_arrival_20261008" |

## 不变量

- ``event_id`` 全局唯一,由 DeviceAdapter 生成
- ``timestamp`` 必须带时区（aware datetime）
- ``payload`` 必须是 JSON 序列化的 dict（不可塞二进制/类实例）
"""
from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


class EventSource(StrEnum):
    """事件来源分类。

    决定 parent_agent 协议前缀:
    - DEVICE → ``trigger:device.<subtype>``
    - VOICE  → ``trigger:voice.<subtype>``
    - TIMER  → ``trigger:timer.<subtype>``
    - WEBHOOK → ``trigger:webhook.<subtype>``
    """

    DEVICE = "device"          # 智能门锁 / 灯 / 空调 / 热水器 ...
    VOICE = "voice"            # 智能音响 / 麦克风阵列唤醒
    TIMER = "timer"            # 定时器 / 闹钟
    WEBHOOK = "webhook"        # 外部 API 回调


class EventPriority(StrEnum):
    """事件优先级。

    决定 AnswerRouter 路由策略:
    - SILENT  — 丢弃,不通知（用于 ack 事件等）
    - NORMAL  — 推默认设备
    - URGENT  — 推全屋设备,加大音量
    """

    SILENT = "silent"
    NORMAL = "normal"
    URGENT = "urgent"


class BaseEvent(BaseModel):
    """事件基类。

    所有具体事件（DeviceEvent / VoiceEvent / TimerEvent）继承此类。
    DeviceAdapter 负责把厂商私有事件转成 BaseEvent 派发。

    不可变（model_config frozen=True）—— Normalizer 如需修改,生成新事件。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    # --- 必填字段 ---

    event_id: str = Field(
        default_factory=lambda: f"evt_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:8]}",
        description="事件唯一 ID,全局唯一",
    )
    source: EventSource = Field(..., description="事件来源分类")
    topic: str = Field(
        ...,
        min_length=1,
        max_length=200,
        description="事件总线 topic,推荐 'source.subtype.action' 三段式",
    )
    user_id: str = Field(..., min_length=1, description="关联用户 ID")
    timestamp: datetime = Field(..., description="事件时间（设备时间,带时区）")
    priority: EventPriority = Field(
        default=EventPriority.NORMAL,
        description="事件优先级,决定路由策略",
    )
    payload: dict[str, Any] = Field(
        default_factory=dict,
        description="业务负载,JSON 序列化的 dict",
    )

    # --- 可选字段 ---

    device_id: str | None = Field(
        default=None,
        description="设备 ID（device 类事件必填,其他类型可选）",
    )
    parent_event_id: str | None = Field(
        default=None,
        description="联动事件链 ID（描述因果关系）",
    )
    correlation_id: str | None = Field(
        default=None,
        description="关联 ID,多事件描述同一业务",
    )

    # --- 派生方法 ---

    @property
    def parent_agent_tag(self) -> str:
        """派生 parent_agent 协议字段,供 EventTrigger 用。

        Examples:
            >>> e = BaseEvent(topic="device.door.opened", ...)
            >>> e.parent_agent_tag
            'trigger:device.door'
        """
        # topic 前 2 段 → parent_agent
        # 例: "device.door.opened" → "trigger:device.door"
        parts = self.topic.split(".")
        if len(parts) >= 2:
            return f"trigger:{parts[0]}.{parts[1]}"
        return f"trigger:{self.source.value}"

    def to_user_input(self) -> str:
        """派生 user_input 文本,供 EventTrigger 调 ainvoke 用。

        默认实现：``"[系统事件] <topic> user_id=<user_id> device=<device_id> payload=<json>"``
        子类可重写以产出更自然的语言。
        """
        import json

        parts: list[str] = [f"[系统事件] {self.topic}"]
        parts.append(f"user_id={self.user_id}")
        if self.device_id:
            parts.append(f"device_id={self.device_id}")
        parts.append(f"priority={self.priority.value}")
        parts.append(f"timestamp={self.timestamp.isoformat()}")
        if self.payload:
            parts.append(f"payload={json.dumps(self.payload, ensure_ascii=False)}")
        return " ".join(parts)


__all__ = [
    "BaseEvent",
    "EventSource",
    "EventPriority",
]
