"""Proactive / Reactive 路由触发条件。

## 职责

定义 ``EventTrigger.route(event) → LoopType`` 的判定规则。
**单一职责**:只判断该走哪条循环,不关心循环内部如何跑。

## 路由规则（参考 ADR-009 §2.2）

**VOICE 源走 Reactive**(用户主动说话 → 管家必须回应 → TTS)。

理由:
- **voice.command** 是**用户消息**——管家必须回应(沉默是 bug)
- ReactiveLoop 直接调 ``ButlerOrchestrator.ainvoke(user_msg)``,绕过 Proactive 默认 silent
- 与设计假设一致:"用户消息走 Reactive,设备事件走 EventBus",但当时没意识到 voice 也属于"用户消息"

**EventBus 上 DEVICE / TIMER / WEBHOOK → Proactive**(管家主动观察是否值得说话)。

理由:
- 设备事件 / 定时器 / 外部回调**不是用户消息**,管家需要"主动判断"
- 默认 silent(沉默优先),仅当 URGENT 优先级或真要主动说才触发

如果未来有"internal"等新 EventSource,加进 ``_PROACTIVE_SOURCES`` 即可。

## 与 ReactiveLoop 的关系

ReactiveLoop 处理 VOICE 源(用户语音)。其他源走 ProactiveLoop。

## 不变量

1. **确定性**:同一 event 多次调用结果一致(纯函数)
2. **可扩展**:新增 EventSource 时只改 ``_PROACTIVE_SOURCES`` / ``_REACTIVE_SOURCES``,不改调用方
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


# 🆕 Phase 8:VOICE 源走 Reactive(用户语音 → 管家必须回应)
# 设备/定时器/外部回调走 Proactive(管家主动判断要不要说)
_PROACTIVE_SOURCES: frozenset[EventSource] = frozenset(
    {
        EventSource.DEVICE,
        EventSource.TIMER,
        EventSource.WEBHOOK,
    },
)

_REACTIVE_SOURCES: frozenset[EventSource] = frozenset(
    {
        EventSource.VOICE,
    },
)


def route(event: BaseEvent) -> LoopType:
    """根据 event.source 路由到对应的循环。

    路由规则(Phase 8):
        - VOICE 源 → Reactive(用户语音,管家必须回应)
        - DEVICE/TIMER/WEBHOOK 源 → Proactive(管家主动观察)
        - 未识别 source → 兜底 Proactive(宁可沉默不可骚扰)

    Args:
        event: 归一化后的事件(来自 EventNormalizer)。

    Returns:
        LoopType.REACTIVE 或 LoopType.PROACTIVE

    Examples:
        >>> e = BaseEvent(source=EventSource.VOICE, topic="voice.command", ...)
        >>> route(e)
        <LoopType.REACTIVE: 'reactive'>

        >>> e = BaseEvent(source=EventSource.DEVICE, topic="device.door.opened", ...)
        >>> route(e)
        <LoopType.PROACTIVE: 'proactive'>

        >>> e = BaseEvent(source=EventSource.TIMER, topic="timer.alarm.ring", ...)
        >>> route(e)
        <LoopType.PROACTIVE: 'proactive'>
    """
    if event.source in _REACTIVE_SOURCES:
        return LoopType.REACTIVE
    if event.source in _PROACTIVE_SOURCES:
        return LoopType.PROACTIVE
    # 兜底:未识别的 source 一律走 Proactive
    # (理由:宁可沉默不可骚扰,Proactive 默认 silent)
    return LoopType.PROACTIVE


__all__ = [
    "LoopType",
    "route",
]
