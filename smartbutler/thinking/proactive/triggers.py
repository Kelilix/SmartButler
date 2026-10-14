"""Proactive / Reactive 路由触发条件。

## 职责

定义 ``EventTrigger.route(event) → LoopType`` 的判定规则。
**单一职责**:只判断该走哪条循环,不关心循环内部如何跑。

## 路由规则（参考 ADR-009 §2.2）

**EventBus 推送的事件 → 全部走 Proactive**。

理由:
- EventBus 推送的事件**不是用户消息**(用户消息走 HTTP/WebSocket 直调 ainvoke)
- EventBus 推送的事件来自**设备/定时器/外部回调**——管家应该"主动判断"
- 兜底:未识别的 source 一律走 Proactive(宁可沉默不可骚扰)

如果未来有"internal"等新 EventSource,加进 ``_PROACTIVE_SOURCES`` 即可。

## 与 ReactiveLoop 的关系

ReactiveLoop **不经过 EventBus**——用户通过 HTTP/WebSocket 直接调
``ButlerOrchestrator.ainvoke(user_msg)``,不构造 BaseEvent。
所以 ``route()`` 的输入**天然只有非用户来源**。

## 不变量

1. **确定性**:同一 event 多次调用结果一致(纯函数)
2. **可扩展**:新增 EventSource 时只改 ``_PROACTIVE_SOURCES``,不改调用方
3. **可单测**:规则集中在一个地方,容易覆盖
4. **保守**:未识别的 source 走 Proactive(默认 silent),不打扰
"""
from __future__ import annotations

from enum import StrEnum

from smartbutler.events.core.event import BaseEvent, EventSource


class LoopType(StrEnum):
    """循环类型。

    决定 EventTrigger 路由到哪条循环:
    - REACTIVE  : 走 ReactiveLoop.ainvoke()   (Phase 4 已有)
    - PROACTIVE : 走 ProactiveLoop.tick()     (Phase 5+ 新建)
    """

    REACTIVE = "reactive"
    PROACTIVE = "proactive"


# EventBus 推送的 EventSource 全部走 Proactive
# 唯一对现有代码的侵入点:新增 EventSource 时,只需在这里加一行
_PROACTIVE_SOURCES: frozenset[EventSource] = frozenset(
    {
        EventSource.DEVICE,
        EventSource.VOICE,
        EventSource.TIMER,
        EventSource.WEBHOOK,
    },
)


def route(event: BaseEvent) -> LoopType:
    """根据 event.source 路由到对应的循环。

    **当前规则**:EventBus 推送的所有事件 → Proactive。

    Args:
        event: 归一化后的事件(来自 EventNormalizer)。

    Returns:
        LoopType.REACTIVE 或 LoopType.PROACTIVE
        (当前实现下,EventBus 事件**永远**返 Proactive)

    Examples:
        >>> e = BaseEvent(source=EventSource.DEVICE, topic="device.door.opened", ...)
        >>> route(e)
        <LoopType.PROACTIVE: 'proactive'>

        >>> e = BaseEvent(source=EventSource.TIMER, topic="timer.alarm.ring", ...)
        >>> route(e)
        <LoopType.PROACTIVE: 'proactive'>
    """
    if event.source in _PROACTIVE_SOURCES:
        return LoopType.PROACTIVE
    # 兜底:未识别的 source 一律走 Proactive
    # (理由:宁可沉默不可骚扰,Proactive 默认 silent)
    return LoopType.PROACTIVE


__all__ = [
    "LoopType",
    "route",
]
