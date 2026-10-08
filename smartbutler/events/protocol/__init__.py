"""设备接入协议（Phase 7）。

定义各类外部事件的标准协议 schema。DeviceAdapter 负责把厂商私有事件
转换成这些 schema,再走 EventBus。

## 协议稳定性

- 本目录下字段一旦敲定,Phase 7 启动后**不删不改**
- 新增字段: 允许(默认 None)
- 删字段 / 改语义: 需新 ADR
"""
from __future__ import annotations

from smartbutler.events.protocol.device_event import DeviceEvent
from smartbutler.events.protocol.timer_event import TimerEvent
from smartbutler.events.protocol.voice_event import VoiceEvent

__all__ = [
    "DeviceEvent",
    "VoiceEvent",
    "TimerEvent",
]
