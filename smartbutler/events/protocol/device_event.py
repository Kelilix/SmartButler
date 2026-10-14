"""设备事件协议（Phase 7）。

智能门锁 / 灯 / 空调 / 热水器 / 烟雾报警器等 IoT 设备的事件 schema。

## 典型 topic

- ``device.door.opened``      — 门锁打开
- ``device.door.closed``      — 门锁关闭
- ``device.light.turned_on``  — 灯打开
- ``device.ac.temperature_changed`` — 空调温度变化
- ``device.water_heater.reached_temp`` — 热水器温度到达
- ``device.smoke_detector.alarm``     — 烟雾报警(**URGENT**)

## 关键字段

| 字段 | 必填 | 说明 |
|------|------|------|
| ``device_type`` | ✅ | 设备类型,枚举(DOOR / LIGHT / AC / WATER_HEATER / SMOKE_DETECTOR / ...) |
| ``device_id`` | ✅ | 设备唯一 ID(由 adapter 注入) |
| ``action`` | ✅ | 动作(opened / closed / turned_on / reached_temp / alarm / ...) |
| ``previous_state`` | ❌ | 前一状态(用于去重 + 事件链) |
| ``current_state`` | ✅ | 当前状态 |
| ``metrics`` | ❌ | 度量(温度 / 湿度 / 电量 / 信号强度) |
"""
from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import Field

from smartbutler.events.core.event import BaseEvent, EventPriority, EventSource


class DeviceType(StrEnum):
    """设备类型枚举。

    设备 adapter 必须在入参时校验 device_type 合法(避免脏数据)。
    新增设备类型时,同步加进 EventNormalizer 的处理规则。
    """

    DOOR = "door"                       # 智能门锁
    WINDOW = "window"                   # 智能窗
    LIGHT = "light"                     # 智能灯
    AC = "ac"                           # 空调
    HEATER = "heater"                   # 暖气
    WATER_HEATER = "water_heater"       # 热水器
    SMOKE_DETECTOR = "smoke_detector"   # 烟雾报警器
    GAS_DETECTOR = "gas_detector"       # 燃气报警器
    MOTION_SENSOR = "motion_sensor"     # 人体传感器
    CAMERA = "camera"                   # 摄像头
    SPEAKER = "speaker"                 # 智能音响(设备维度)
    THERMOSTAT = "thermostat"           # 恒温器
    PLUG = "plug"                       # 智能插座
    CURTAIN = "curtain"                 # 智能窗帘
    UNKNOWN = "unknown"


class DeviceEvent(BaseEvent):
    """设备事件。

    DeviceAdapter 必须保证:
    - ``device_type`` 是合法枚举值
    - ``topic`` 与 device_type + action 一致(便于路由)
    - ``payload`` 只放原始厂商数据,业务字段提到顶层
    """

    # 必填字段
    device_type: DeviceType = Field(..., description="设备类型枚举")
    action: str = Field(..., min_length=1, description="设备动作,推荐英文下划线")
    current_state: dict[str, Any] = Field(
        default_factory=dict,
        description="当前状态,如 {'locked': False, 'battery': 0.85}",
    )

    # 可选字段
    previous_state: dict[str, Any] | None = Field(
        default=None,
        description="前一状态(增量事件才有,全量事件 None)",
    )
    metrics: dict[str, float] | None = Field(
        default=None,
        description="度量(温度/湿度/电量),如 {'temperature': 65.0, 'humidity': 0.45}",
    )

    # --- 构造辅助 ---

    @classmethod
    def make(
        cls,
        *,
        device_type: DeviceType,
        device_id: str,
        action: str,
        user_id: str,
        current_state: dict[str, Any] | None = None,
        previous_state: dict[str, Any] | None = None,
        metrics: dict[str, float] | None = None,
        priority: EventPriority = EventPriority.NORMAL,
        payload: dict[str, Any] | None = None,
        timestamp: datetime | None = None,
    ) -> DeviceEvent:
        """工厂方法:从设备 adapter 调用,自动填 source/topic/device_id。"""
        topic = f"device.{device_type.value}.{action}"
        return cls(
            source=EventSource.DEVICE,
            topic=topic,
            user_id=user_id,
            device_id=device_id,
            device_type=device_type,
            action=action,
            current_state=current_state or {},
            previous_state=previous_state,
            metrics=metrics,
            priority=priority,
            payload=payload or {},
            timestamp=timestamp or datetime.now().astimezone(),
        )

    def to_user_input(self) -> str:
        """重写:产出更自然的设备事件描述。"""
        parts: list[str] = [
            f"[系统事件] 设备事件: {self.device_type.value} {self.action}",
            f"device_id={self.device_id}",
            f"user_id={self.user_id}",
        ]
        if self.current_state:
            parts.append(f"state={self.current_state}")
        if self.metrics:
            parts.append(f"metrics={self.metrics}")
        parts.append(f"priority={self.priority.value}")
        return " ".join(parts)


__all__ = ["DeviceEvent", "DeviceType"]
