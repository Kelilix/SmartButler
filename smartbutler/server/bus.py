"""内存版 EventBus（Phase 7.1）。

## 为什么先做内存版

Phase 7.1 范围:**不接 Redis / NATS / Kafka**(留接口不接实现)。

为了"先跑通端到端",本文件提供:

- ``InMemoryEventBus`` — 同进程内的 pub/sub,实现 :class:`smartbutler.events.core.event_bus.EventBus`
- 支持 topic 通配符订阅(只支持 ``*`` 后缀,例如 ``"device.door.*"``)
- 支持 ``publish`` / ``subscribe`` / ``start`` / ``stop`` / ``ack`` / ``replay``
- ``ack`` 是 no-op(内存版没有持久化 ack 状态)
- ``replay`` 返回已 publish 的事件历史(按 topic 过滤 + limit)

## 不变量

- publish 后,subscribe 注册的 handler 会在下一次 consumer tick 时被调
- handler 抛异常 → log + 继续(不停止 consumer)
- 同事件可能被多次送达(无 dedup,跟 EventBus 抽象一致 — 消费方自己幂等)
- start / stop 必须幂等
"""
from __future__ import annotations

import asyncio
import fnmatch
from collections.abc import AsyncIterator
from typing import Any

import structlog

from smartbutler.events.core.event import BaseEvent
from smartbutler.events.core.event_bus import EventBus, EventBusError, EventHandler

_logger = structlog.get_logger(__name__)


def _topic_matches(pattern: str, topic: str) -> bool:
    """topic 模式匹配。

    - 完全相等 → 匹配
    - 含 ``*`` → fnmatch 风格通配(支持 ``*`` 任意段 / ``?`` 单字符)
    - 无 ``*`` 但含 ``.`` → 视为"前两段精确 + 后段任意"(兼容 ADR-008 §3 的
      ``"device.door.*"`` 风格)
    """
    if pattern == topic:
        return True
    if "*" in pattern:
        return fnmatch.fnmatchcase(topic, pattern)
    # 兼容: pattern = "device.door" → 匹配 "device.door.opened"
    if "." in pattern and topic.startswith(pattern + "."):
        return True
    return False


class InMemoryEventBus(EventBus):
    """内存版 EventBus — 满足抽象接口,无外部依赖。"""

    def __init__(self, *, history_maxsize: int = 1000) -> None:
        self._subscriptions: list[tuple[str, str, EventHandler]] = []
        # (pattern, consumer_group, handler)
        self._queue: asyncio.Queue[BaseEvent] = asyncio.Queue()
        self._history: list[BaseEvent] = []
        self._history_maxsize = history_maxsize
        self._consumer_task: asyncio.Task[None] | None = None
        self._stopped = True

    # ----------------- EventBus 协议 -----------------

    async def publish(
        self, event: BaseEvent, *, topic: str | None = None
    ) -> None:
        if self._stopped:
            raise EventBusError("EventBus 未 start,不能 publish")
        # 不可变事件 — topic 覆写用新事件
        actual_event = event
        if topic is not None and topic != event.topic:
            actual_event = event.model_copy(update={"topic": topic})
        # 历史
        self._history.append(actual_event)
        if len(self._history) > self._history_maxsize:
            self._history = self._history[-self._history_maxsize :]
        await self._queue.put(actual_event)

    async def subscribe(
        self,
        topic: str,
        handler: EventHandler,
        *,
        consumer_group: str = "default",
    ) -> None:
        if self._stopped:
            raise EventBusError("EventBus 未 start,不能 subscribe")
        self._subscriptions.append((topic, consumer_group, handler))

    async def start(self) -> None:
        if not self._stopped:
            return  # 幂等
        self._stopped = False
        self._consumer_task = asyncio.create_task(
            self._consume_loop(), name="InMemoryEventBus.consumer"
        )

    async def stop(self) -> None:
        self._stopped = True
        if self._consumer_task is not None:
            self._consumer_task.cancel()
            try:
                await self._consumer_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._consumer_task = None

    async def ack(self, event: BaseEvent) -> None:
        # 内存版:无持久化 ack 状态,no-op
        return None

    async def replay(
        self,
        topic: str,
        *,
        since: Any | None = None,
        limit: int = 100,
    ) -> AsyncIterator[BaseEvent]:
        """返回历史事件(浅拷贝,按 topic 过滤)。"""
        # 内存版忽略 since(无时间索引)
        matched: list[BaseEvent] = []
        for evt in reversed(self._history):
            if _topic_matches(topic, evt.topic):
                matched.append(evt)
            if len(matched) >= limit:
                break
        # 倒序回放,顺序正常
        for evt in reversed(matched):
            yield evt

    # ----------------- 运维 / 单测辅助 -----------------

    @property
    def history_size(self) -> int:
        """历史事件数(单测断言用)。"""
        return len(self._history)

    def clear_history(self) -> None:
        """清空历史(单测用)。"""
        self._history.clear()

    def subscription_count(self) -> int:
        """当前订阅数(单测用)。"""
        return len(self._subscriptions)

    # ----------------- 内部 -----------------

    async def _consume_loop(self) -> None:
        while not self._stopped:
            try:
                event = await self._queue.get()
            except asyncio.CancelledError:
                break
            for pattern, _group, handler in list(self._subscriptions):
                if not _topic_matches(pattern, event.topic):
                    continue
                try:
                    await handler(event)
                except Exception as exc:  # noqa: BLE001
                    _logger.error(
                        "in_memory_eventbus.handler_error",
                        pattern=pattern,
                        topic=event.topic,
                        event_id=event.event_id,
                        error=str(exc),
                        exc_type=type(exc).__name__,
                    )
            # no-op ack
            try:
                await self.ack(event)
            except Exception:  # noqa: BLE001
                pass


__all__ = [
    "InMemoryEventBus",
    "_topic_matches",
]
