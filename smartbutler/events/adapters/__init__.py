"""设备接入适配器（Phase 7）。

各厂商 / 各平台的设备 SDK 接入层。

## 职责

DeviceAdapter:
1. 接收厂商私有事件(SDK callback / webhook / MQTT message / ...)
2. 转成标准 BaseEvent 协议
3. 调 EventBus.publish() 推入总线

## Phase 7 计划接入

| Adapter | 来源 | 协议 |
|---------|------|------|
| ``HomeAssistantAdapter`` | Home Assistant REST + WebSocket | 内部 HA events |
| ``MijiaAdapter`` | 米家云 API | webhook |
| ``MockAdapter`` | 测试用 | 内存事件 |

## 抽象基类

本目录只放抽象接口,具体实现 Phase 7 启动时按平台添加。
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from smartbutler.events.core.event import BaseEvent
from smartbutler.events.core.event_bus import EventBus


class DeviceAdapter(ABC):
    """设备接入抽象基类。"""

    def __init__(self, event_bus: EventBus) -> None:
        self._event_bus = event_bus

    @abstractmethod
    async def start(self) -> None:
        """启动 adapter(连接 SDK / 订阅 webhook / 建 WebSocket)。"""
        ...

    @abstractmethod
    async def stop(self) -> None:
        """停止 adapter(断开连接 / 取消订阅)。"""
        ...

    @abstractmethod
    def supports_topic(self, topic: str) -> bool:
        """判断本 adapter 是否能处理该 topic(用于路由分发)。"""
        ...

    @abstractmethod
    async def transform_to_event(self, raw: object) -> BaseEvent:
        """把厂商私有事件转成标准 BaseEvent。

        实现要求:
        - 入参必须严格校验(厂商 SDK 经常给 None / 缺字段)
        - 转换失败抛 AdapterTransformError(上层会记日志 + 跳过)
        - 转换成功事件必须带 device_id + user_id(否则 publish 会被 EventBus 拒)
        """
        ...


class AdapterTransformError(ValueError):
    """设备事件转 BaseEvent 失败(数据格式错误 / 缺关键字段)。"""


__all__ = ["DeviceAdapter", "AdapterTransformError"]
