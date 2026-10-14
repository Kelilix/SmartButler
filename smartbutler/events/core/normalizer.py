"""EventNormalizer 抽象接口（Phase 7）。

## 职责

事件进入管家前**必走** EventNormalizer,处理:
- **dedup** — 同一事件 N 秒内多次,只保留第一个
- **debounce** — 抖动事件(温度阈值附近反复触发)合并
- **filter** — 用户已离开/已知不感兴趣 → 丢弃
- **augment** — 补全上下文(时间、用户在场状态、前置事件链)

## 为什么必做

不归一化直接喂 LLM = **事件风暴把管家打挂**。
- 烟雾报警连续触发 60 次/分钟 → 60 次 ainvoke → LLM token 爆炸
- 门锁开启 5 秒内反复触发 → 5 次"欢迎回家" → 烦人

## 链式 vs 单一

默认实现是单一 Normalizer(集中规则),Phase 7 启动时如需可改成 Pipeline(链式)。

## 不变量

- Normalizer **不修改**原事件(BaseEvent frozen) → 返回新事件或 None
- 返 None = 丢弃该事件
- 返新事件 = 替换,继续走下游
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from smartbutler.events.core.event import BaseEvent


class EventNormalizer(ABC):
    """事件归一化器抽象接口。"""

    @abstractmethod
    async def normalize(self, event: BaseEvent) -> BaseEvent | None:
        """归一化事件。

        Args:
            event: 原始事件

        Returns:
            - ``None`` — 丢弃该事件
            - ``BaseEvent`` — 归一化后的事件(可能是原事件本身,可能是新构造的)

        实现要求:
        - 必须快速(单次 < 1ms 目标,不能调 LLM)
        - 必须幂等(同一事件多次调用,结果一致)
        - 必须可观测(每个决策打 DEBUG 日志:dedup_key=xxx, decision=drop)
        """
        ...


__all__ = ["EventNormalizer"]
