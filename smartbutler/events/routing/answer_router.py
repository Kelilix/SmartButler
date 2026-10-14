"""AnswerRouter 抽象接口（Phase 7）。

## 职责

EventTrigger 拿到管家 answer → AnswerRouter:
1. 查 PresenceService 拿当前用户活跃设备
2. 按 priority 决定路由策略
3. 调目标设备 adapter (TTS / push / 屏显)

## 路由策略矩阵

| priority | 正常情况 | 紧急情况 (URGENT) |
|----------|----------|------------------|
| NORMAL | 推活跃设备 | 推活跃设备 |
| URGENT | - | 推全屋设备,加大音量 |
| SILENT | 不推(打日志) | - |

## 不变量

- 必须 fallback:PresenceService 挂掉 → 推"默认设备"
- 必须 timeout:推设备 adapter 超时 → 切下一个目标设备
- 必须可观测:每次路由打 INFO 日志(target=xxx, fallback_used=xxx)
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from smartbutler.events.core.event import BaseEvent, EventPriority


class AnswerRouter(ABC):
    """答案路由抽象接口。"""

    @abstractmethod
    async def route(
        self,
        event: BaseEvent,
        answer: str,
        *,
        priority: EventPriority | None = None,
    ) -> bool:
        """路由 answer 到目标设备。

        Args:
            event: 触发本 answer 的原始事件(用于取 user_id / 设备上下文)
            answer: 管家产出的回答文本
            priority: 覆盖事件原始优先级(默认用 event.priority)

        Returns:
            - True: 成功路由
            - False: 路由失败(已 fallback)
        """
        ...


__all__ = ["AnswerRouter"]
