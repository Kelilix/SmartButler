"""EventTrigger 抽象接口(Phase 7)。

## 职责（修订后,参考 ADR-009）

EventNormalizer 输出的事件 → EventTrigger:

1. 调 ``smartbutler.thinking.proactive.route(event)`` 决定走哪条循环
2. 走 Reactive → 构造 user_input 文本,调 ``ButlerOrchestrator.ainvoke(...)``
3. 走 Proactive → 调 ``ButlerOrchestrator.proactive_tick(event)``
4. 拿到 answer / ProactiveResult → 交给 ``AnswerRouter`` 路由回目标设备

## 关键约束

- **不阻塞总线**: EventTrigger 调 ainvoke/proactive_tick 是异步的,总线 consumer 不能被 LLM 延迟卡住
- **限速**: 同一 user_id 60 秒内最多 N 次 trigger(防"事件风暴")
- **超时**: ainvoke/proactive_tick 超时必须 fallback 到兜底文案,不能让总线消费死锁
- **限流触发**: Normalizer 返 None 时不调用循环

## 双循环路由（替代原"复用 ainvoke"方案）

旧方案:把事件伪装成 user_input,复用 ainvoke 入口
新方案(ADR-009 §5.9.3):
- EventSource == user/interface → ReactiveLoop.ainvoke()(后续扩展,当前未启用)
- EventSource == device/timer/voice/webhook → ProactiveLoop.tick()(默认沉默优先)

`smartbutler.thinking.proactive.triggers.route(event)` 是路由逻辑的单一来源。
EventTrigger 仅负责"调用路由 + 处理结果",不重复实现路由规则。

## 不变量

- parent_agent 必须是 ``trigger:<source>.<subtype>`` 格式(ProactiveLoop 内部已用)
- 失败必须 ack 事件(不阻塞总线),只是 answer 用兜底文案
- 限速超限直接 ack + 打 WARN 日志,不调循环
- Proactive 触发的工具不暴露写操作(防"事件→管家→写设备→又发事件"循环)
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from smartbutler.events.core.event import BaseEvent


class EventTrigger(ABC):
    """事件触发器抽象接口(Phase 7)。

    实现类应:
    1. 调 ``smartbutler.thinking.proactive.route(event)`` 路由
    2. 按路由结果调 ReactiveLoop 或 ProactiveLoop
    3. 兜底 + 限速 + 超时保护
    """

    @abstractmethod
    async def handle(self, event: BaseEvent) -> str | None:
        """处理一个归一化后的事件,返回管家产出的回答文本。

        路由逻辑(参考 ADR-009 §5.9.3):
        - source == user/interface  → ReactiveLoop.ainvoke()
        - source == device/timer/voice/webhook → ProactiveLoop.tick()
        - ProactiveLoop 返回 ProactiveResult(可能 silent → 本方法返 None)

        Args:
            event: 归一化后的事件(不是原始事件)

        Returns:
            - ``str`` — 管家产出的 answer,交给下游 AnswerRouter 路由
            - ``None`` — 本触发器不处理该事件(silently skip,或 Proactive silent)

        Raises:
            触发器本身不抛异常(必须内部兜底)
            否则总线 consumer 会被反复重试同一条事件
        """
        ...

    @abstractmethod
    async def should_handle(self, event: BaseEvent) -> bool:
        """判断本触发器是否关心该事件(topic 匹配 + 限速检查)。

        在 handle 之前先调,避免无谓的路由 / 循环调用。
        """
        ...


__all__ = ["EventTrigger"]
