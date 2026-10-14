"""事件核心抽象（Phase 7）。

本目录存放事件驱动架构的核心抽象接口,**Phase 4-6 不写实现**。
"""
from __future__ import annotations

from smartbutler.events.core.event import (
    BaseEvent,
    EventPriority,
    EventSource,
)
from smartbutler.events.core.event_bus import EventBus
from smartbutler.events.core.normalizer import EventNormalizer
from smartbutler.events.core.trigger import EventTrigger

__all__ = [
    "BaseEvent",
    "EventPriority",
    "EventSource",
    "EventBus",
    "EventNormalizer",
    "EventTrigger",
]
