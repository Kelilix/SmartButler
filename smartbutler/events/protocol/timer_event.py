"""定时器事件协议（Phase 7）。

cron / 闹钟 / 一次性定时器事件 schema。

## 典型 topic

- ``timer.scheduled``         — 定时器到点
- ``timer.alarm``             — 闹钟响
- ``timer.reminder``          — 提醒类
- ``timer.oneshot``           — 一次性任务完成

## 与其他协议的关系

定时器事件**不来自设备**,而是管家自己 / 用户配置的"主动行为"。
但仍走 EventBus 同一个通道,统一被 EventTrigger 消费。

## 关键字段

| 字段 | 必填 | 说明 |
|------|------|------|
| ``timer_id`` | ✅ | 定时器 ID(由 scheduler 注入) |
| ``timer_type`` | ✅ | 类型(cron / alarm / reminder / oneshot) |
| ``fire_at`` | ✅ | 计划触发时间 |
| ``actual_fire_at`` | ✅ | 实际触发时间(可能延迟) |
| ``recurrence`` | ❌ | 重复规则(cron 表达式) |
| ``payload`` | ✅ | 业务负载(用户当时配置的文本) |
"""
from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import Field

from smartbutler.events.core.event import BaseEvent, EventPriority, EventSource


class TimerType(StrEnum):
    """定时器类型。"""

    CRON = "cron"                # cron 重复任务
    ALARM = "alarm"              # 闹钟(单次)
    REMINDER = "reminder"        # 提醒(单次)
    ONESHOT = "oneshot"          # 一次性任务
    DELAYED = "delayed"          # 延时任务(N 秒后)


class TimerEvent(BaseEvent):
    """定时器事件。"""

    # 必填
    timer_id: str = Field(..., min_length=1, description="定时器 ID")
    timer_type: TimerType = Field(..., description="定时器类型")
    fire_at: datetime = Field(..., description="计划触发时间")
    actual_fire_at: datetime = Field(..., description="实际触发时间(可能延迟)")
    user_message: str = Field(
        ...,
        min_length=1,
        description="定时器触发时,管家要对用户说的话/做的事",
    )

    # 可选
    recurrence: str | None = Field(
        default=None,
        description="cron 表达式(cron 类型必填)",
    )
    timezone: str = Field(
        default="Asia/Shanghai",
        description="时区,IANA 名(如 Asia/Shanghai)",
    )

    @classmethod
    def make(
        cls,
        *,
        timer_id: str,
        timer_type: TimerType,
        fire_at: datetime,
        actual_fire_at: datetime,
        user_id: str,
        user_message: str,
        recurrence: str | None = None,
        timezone: str = "Asia/Shanghai",
        priority: EventPriority = EventPriority.NORMAL,
        payload: dict | None = None,
    ) -> "TimerEvent":
        """工厂方法。"""
        topic = f"timer.{timer_type.value}"
        return cls(
            source=EventSource.TIMER,
            topic=topic,
            user_id=user_id,
            device_id=None,  # 定时器无设备
            timer_id=timer_id,
            timer_type=timer_type,
            fire_at=fire_at,
            actual_fire_at=actual_fire_at,
            user_message=user_message,
            recurrence=recurrence,
            timezone=timezone,
            priority=priority,
            payload=payload or {},
            timestamp=actual_fire_at,
        )

    def to_user_input(self) -> str:
        """重写:定时器事件直接把 user_message 作为 LLM 输入。"""
        delay_seconds = (self.actual_fire_at - self.fire_at).total_seconds()
        return (
            f"[系统事件] 定时器触发 ({self.timer_type.value}): "
            f"{self.user_message} "
            f"(计划 {self.fire_at.isoformat()}, "
            f"实际 {self.actual_fire_at.isoformat()}, "
            f"延迟 {delay_seconds:.1f}s)"
        )


__all__ = ["TimerEvent", "TimerType"]
