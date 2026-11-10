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

    def __init__(
        self,
        *,
        history_maxsize: int = 1000,
        generation_tracker: Any | None = None,
    ) -> None:
        """
        Args:
            history_maxsize: 历史事件保留条数。
            generation_tracker: 用户输入代次追踪器(L1/L2 打断用)。
                None → 不做代次追踪,行为与改造前一致。
        """
        self._subscriptions: list[tuple[str, str, EventHandler]] = []
        # (pattern, consumer_group, handler)
        self._queue: asyncio.Queue[BaseEvent] = asyncio.Queue()
        self._history: list[BaseEvent] = []
        self._history_maxsize = history_maxsize
        self._consumer_task: asyncio.Task[None] | None = None
        self._stopped = True
        # 🆕 2026-11-10:代次追踪(L1/L2 打断)
        self._generation_tracker = generation_tracker

    # ----------------- 代次追踪(L1/L2 打断) -----------------

    @property
    def generation_tracker(self) -> Any:
        """用户输入代次追踪器(见 :class:`EventBus` 说明)。"""
        return self._generation_tracker

    async def drop_stale_pending(self) -> int:
        """丢弃队列里**已过期**的待处理事件(L1)。

        ⚠️ 这不是必须的 —— 消费端在处理前检查代次也能达到同样效果。
        但在**队列层**丢弃有两个好处:
            1. 过期事件根本不会占用 LLM 的处理时间
            2. 队列不会因为堆积旧事件而"看起来很忙"

        什么时候用:
            - 队列积压时(用户连续说了很多句),定期清一次
            - 或者干脆每 publish 一次就清一次(开销小,队列本来就不长)

        Returns:
            被丢弃的事件数。
        """
        tracker = self._generation_tracker
        if tracker is None:
            return 0

        # 把队列里的事件取出来检查,过期的丢弃,不过期的放回去
        kept: list[BaseEvent] = []
        dropped = 0
        while True:
            try:
                evt = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            generation = getattr(evt, "generation", 0)
            if generation and tracker.is_stale(generation):
                dropped += 1
                tracker.note_dropped_stale()
                continue
            kept.append(evt)

        # 放回去(保持顺序)
        for evt in kept:
            self._queue.put_nowait(evt)

        if dropped:
            _logger.info(
                "in_memory_eventbus.dropped_stale_pending",
                dropped=dropped,
                kept=len(kept),
            )
        return dropped

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

        # 🆕 2026-11-10 (L1):入队前先清掉队列里已过期的事件。
        #
        # 场景:用户连说三句 → 队列里 seq=1,2,3。LLM 正在处理 seq=1。
        #       此时 publish(seq=3) → 先把队列里的 seq=2 清掉(它已过时),
        #       只留 seq=3 → 消费端接下来只处理 seq=3。
        #
        # 为什么在这里做(而不是只在消费端检查):
        #   消费端检查 = 过期事件仍会占一次 handler 调用(虽然不进 LLM),
        #   队列层清理 = 过期事件**根本没进过队列**,更彻底。
        #   两个都做:队列层清理省内存,消费端检查兜底(防止漏清)。
        await self.drop_stale_pending()

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

            # 🆕 2026-11-10 (L1 兜底):取出后再检查一次代次。
            #
            # 为什么 publish 时清过一次还要再查:
            #   时序 —— publish(seq=1) → 队列[seq=1] → consumer 取出 seq=1
            #   → **此时**用户说了 seq=2(publish)→ 队列清理时 seq=1 已经被取走了,
            #   清理不到它 → seq=1 仍会被处理。
            #
            #   这个窗口正好覆盖"LLM 正在算 seq=1,用户又说了 seq=2"的场景 ——
            #   也正是我们要丢弃 seq=1 的场景。
            tracker = self._generation_tracker
            if tracker is not None:
                generation = getattr(event, "generation", 0)
                if generation and tracker.is_stale(generation):
                    tracker.note_dropped_stale()
                    _logger.info(
                        "in_memory_eventbus.skip_stale_event",
                        event_id=event.event_id,
                        topic=event.topic,
                        generation=generation,
                        latest_seq=tracker.latest_seq(),
                    )
                    # no-op ack(与下面正常路径保持一致)
                    try:
                        await self.ack(event)
                    except Exception:  # noqa: BLE001
                        pass
                    continue

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
