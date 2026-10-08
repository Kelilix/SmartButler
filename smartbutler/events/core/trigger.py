"""EventTrigger 抽象接口（Phase 7）。

## 职责

EventNormalizer 输出的事件 → EventTrigger:
1. 构造虚拟 ``user_input`` 文本
2. 调 ``ButlerOrchestrator.ainvoke(...)``
3. 拿到 answer → 交给 ``AnswerRouter`` 路由回目标设备

## 关键约束

- **不阻塞总线**: EventTrigger 调 ainvoke 是异步的,总线 consumer 不能被 LLM 延迟卡住
- **限速**: 同一 user_id 60 秒内最多 N 次 trigger(防"事件风暴")
- **超时**: ainvoke 超时必须 fallback 到兜底文案,不能让总线消费死锁
- **限流触发**: Normalizer 返 None 时不调用 ainvoke

## 复用 vs 重写

- **复用** ainvoke —— 继承 Phase 4 的 ButlerOrchestrator,**一行不改主体**
- **不重写** EventLoop —— LLM 资源浪费

## 不变量

- parent_agent 必须是 ``trigger:<source>.<subtype>`` 格式
- 失败必须 ack 事件(不阻塞总线),只是 answer 用兜底文案
- 限速超限直接 ack + 打 WARN 日志,不调 ainvoke
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from smartbutler.events.core.event import BaseEvent


class EventTrigger(ABC):
    """事件触发器抽象接口。"""

    @abstractmethod
    async def handle(self, event: BaseEvent) -> str | None:
        """处理一个归一化后的事件,返回管家产出的回答文本。

        Args:
            event: 归一化后的事件(不是原始事件)

        Returns:
            - ``str`` — 管家产出的 answer,交给下游 AnswerRouter 路由
            - ``None`` — 本触发器不处理该事件(silently skip)
                          例如 voice wake 但用户没说话,正常返 None

        Raises:
            触发器本身不抛异常(必须内部兜底)
            否则总线 consumer 会被反复重试同一条事件
        """
        ...

    @abstractmethod
    async def should_handle(self, event: BaseEvent) -> bool:
        """判断本触发器是否关心该事件(topic 匹配 + 限速检查)。

        在 handle 之前先调,避免无谓的 user_input 构造。
        """
        ...


__all__ = ["EventTrigger"]
