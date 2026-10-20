"""DefaultEventTrigger(Phase 7.4)。

## 职责

拿到归一化后的事件,**决定走哪条循环** + **限速** + **调用** + **兜底**。

## 路由规则(沿用 ADR-009 §5.9.3 + smartbutler.thinking.proactive.triggers.route)

- ``EventSource.DEVICE`` / ``VOICE`` / ``TIMER`` / ``WEBHOOK`` → ProactiveLoop.tick()
- 兜底:未识别 source 一律走 Proactive(默认 silent,沉默优先)
- 当前 EventBus 入口**全部走 Proactive**(无 user/interface 入口)

## 限速

- 同一 user_id 默认 ``max_per_minute=20``(超出 → ack + WARN,不调循环)
- 内存表 ``dict[user_id, list[float]]`` 维护时间戳

## 兜底

- ``handle`` 内部 try/except,**不抛异常**(沿用 trigger.py:65-67 注释)
- 失败 → 返 fallback 文本 ``"[error] 管家暂时无法处理该事件"`` + 记 ERROR 日志
- ack:成功后 EventBus 调 ``bus.ack(event)``(内存版 no-op,但接口调用保留)

## 不变量

- 触发器不抛异常(否则 EventBus consumer 会反复重试同一条事件)
- Proactive 触发的工具不暴露写操作(防"事件→管家→写设备→又发事件"循环)
- silent → 返回 ``None``,不下发到 OutputAdapter
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque
from typing import Any

from smartbutler.events.core.event import BaseEvent
from smartbutler.events.core.trigger import EventTrigger
from smartbutler.thinking.proactive.triggers import route as route_loop
from smartbutler.thinking.proactive.decision import ProactiveResult
from smartbutler.utils.logging import get_logger

logger = get_logger(__name__)


class DefaultEventTrigger(EventTrigger):
    """默认事件触发器(Phase 7.4)。

    Args:
        orchestrator: ButlerOrchestrator 实例(必须能调 proactive_tick / ainvoke)
        max_per_minute: 同一 user_id 1 分钟内最多处理多少事件,默认 20
        window_sec: 限速时间窗(秒),默认 60.0
    """

    def __init__(
        self,
        *,
        orchestrator: Any,
        max_per_minute: int = 20,
        window_sec: float = 60.0,
    ) -> None:
        if max_per_minute <= 0:
            raise ValueError(f"max_per_minute 必须 > 0,得到 {max_per_minute}")
        if window_sec <= 0:
            raise ValueError(f"window_sec 必须 > 0,得到 {window_sec}")
        self._orchestrator = orchestrator
        self._max_per_minute = max_per_minute
        self._window_sec = window_sec
        # 限速表:user_id → 时间戳双端队列
        self._buckets: dict[str, deque[float]] = defaultdict(deque)
        # 单进程多 consumer 协程并发保护
        self._lock = asyncio.Lock()
        # 统计
        self.stats: dict[str, int] = {
            "received": 0,
            "rate_limited": 0,
            "proactive_called": 0,
            "reactive_called": 0,
            "silent": 0,
            "errors": 0,
        }

    async def should_handle(self, event: BaseEvent) -> bool:
        """判断本触发器是否关心该事件(限速检查)。

        Returns:
            True = 在限速内,继续 handle
            False = 超限,ack + WARN,不调循环
        """
        async with self._lock:
            self.stats["received"] += 1
            now = time.monotonic()
            bucket = self._buckets[event.user_id]
            # 清理窗口外的旧时间戳
            cutoff = now - self._window_sec
            while bucket and bucket[0] < cutoff:
                bucket.popleft()
            # 限速判断
            if len(bucket) >= self._max_per_minute:
                self.stats["rate_limited"] += 1
                logger.warning(
                    "event_trigger.rate_limited user_id=%s topic=%s "
                    "max_per_minute=%d current=%d",
                    event.user_id, event.topic, self._max_per_minute, len(bucket),
                )
                return False
            # 通过限速 → 记录时间戳
            bucket.append(now)
            return True

    async def handle(self, event: BaseEvent) -> str | None:
        """处理一个归一化后的事件,返回管家产出的回答文本。

        Returns:
            - ``str`` — 管家产出的 answer,交给下游 AnswerRouter 路由
            - ``None`` — silent(沉默优先,不下发)

        不变量:本方法**不抛异常**(沿用 trigger.py 不变量)。

        Phase 8 路由规则:
            - VOICE 源 → Reactive(用户语音,管家必须回应)
            - DEVICE/TIMER/WEBHOOK 源 → Proactive(管家主动观察)
        """
        try:
            loop_type = route_loop(event)
            if loop_type.value == "proactive":
                self.stats["proactive_called"] += 1
                result = await self._orchestrator.proactive_tick(
                    event, user_id=event.user_id,
                )
                if not isinstance(result, ProactiveResult):
                    # 防呆:ProactiveLoop 应该返 ProactiveResult,若不是视为 silent
                    logger.error(
                        "event_trigger.proactive_unexpected_result type=%s",
                        type(result).__name__,
                    )
                    self.stats["silent"] += 1
                    return None
                if not result.acted:
                    # 沉默优先(沿用不变量 #5)
                    self.stats["silent"] += 1
                    logger.debug(
                        "event_trigger.silent event_id=%s reason=%s",
                        event.event_id, getattr(result, "silence_reason", ""),
                    )
                    return None
                # acted=True → 拿 message
                message = getattr(result, "message", None) or ""
                if not message.strip():
                    self.stats["silent"] += 1
                    return None
                return message
            else:
                # 🆕 Phase 8:Reactive 路由(VOICE 源)
                # 用户语音 → 直接调 ainvoke(管家必须回应,不静默)
                self.stats["reactive_called"] += 1
                # 提取 ASR 文本作为 user_input(VoiceEvent 已重写 to_user_input)
                user_input = event.to_user_input()
                # 🆕 7.6 声纹:透传 speakers(VoiceAdapter 已填充)
                reply = await self._orchestrator.ainvoke(
                    user_input,
                    user_id=event.user_id,
                    parent_agent=event.parent_agent_tag,
                    speakers=list(event.speakers) if event.speakers else None,
                )
                if not reply or not reply.strip():
                    self.stats["silent"] += 1
                    return None
                return reply
        except Exception as exc:  # noqa: BLE001
            self.stats["errors"] += 1
            logger.error(
                "event_trigger.handle_error event_id=%s topic=%s error=%s exc_type=%s",
                event.event_id, event.topic, str(exc), type(exc).__name__,
            )
            # 兜底:返 fallback 文本(不抛)
            return f"[error] 管家暂时无法处理该事件(topic={event.topic})"

    def reset(self) -> None:
        """清空限速表(单测 / 运维用)。"""
        self._buckets.clear()
        self.stats = {
            "received": 0,
            "rate_limited": 0,
            "proactive_called": 0,
            "reactive_called": 0,
            "silent": 0,
            "errors": 0,
        }

    def snapshot(self) -> dict[str, Any]:
        """导出当前状态(可观测性 / 单测断言用)。"""
        return {
            "max_per_minute": self._max_per_minute,
            "window_sec": self._window_sec,
            "active_users": len(self._buckets),
            "stats": dict(self.stats),
        }


__all__ = ["DefaultEventTrigger"]
