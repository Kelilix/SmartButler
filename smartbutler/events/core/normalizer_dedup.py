"""DedupNormalizer(Phase 7.4)。

## 职责

基于 ``event_id`` + ``topic`` 的事件去重。
- 同 ``event_id``(或同 topic + window 内)→ 只保留第一个
- 超过窗口的相同 event_id 视为新事件(放行)
- LRU 上限 ``max_size=10000``(防内存泄漏)

## 为什么必须

不归一化直接喂 LLM = **事件风暴把管家打挂**。
- 烟雾报警 1 秒推 60 个相同 event_id 事件
- 门锁 5 秒内反复触发 5 次
- 智能音响误唤醒推 3 个 voice.wake_word

## 不做(留 Phase 7.4+ 后续)

- Debounce(按门铃 N 次合成 1 次)—— 需要按 (topic, device_id, payload 关键字段) 算 fingerprint
- Filter(用户已知不感兴趣 → 丢弃)—— 需要 PresenceService + 用户偏好
- Augment(补全上下文)—— 需要历史事件关联

## 不变量

- Normalizer **不修改**原事件(BaseEvent frozen) → 返回原事件或 None
- 返 None = 丢弃,返原事件 = 放行
- 单次 normalize 目标 < 1ms(纯内存 dict,无 IO)
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from typing import Any

from smartbutler.events.core.event import BaseEvent
from smartbutler.events.core.normalizer import EventNormalizer
from smartbutler.utils.logging import get_logger

logger = get_logger(__name__)


class DedupNormalizer(EventNormalizer):
    """事件去重 Normalizer(Phase 7.4)。

    Args:
        window_sec: 同一 event_id 视为重复的时间窗口(秒),默认 5.0
        max_size: LRU 上限,超过按 LRU 淘汰最早的 key,默认 10000
    """

    def __init__(self, *, window_sec: float = 5.0, max_size: int = 10000) -> None:
        if window_sec <= 0:
            raise ValueError(f"window_sec 必须 > 0,得到 {window_sec}")
        if max_size <= 0:
            raise ValueError(f"max_size 必须 > 0,得到 {max_size}")
        self._window_sec = window_sec
        self._max_size = max_size
        # OrderedDict 维护插入顺序,LRU 淘汰时 popitem(last=False) 拿最早的
        self._seen: OrderedDict[str, float] = OrderedDict()
        # 单进程多 consumer 协程并发保护
        self._lock = asyncio.Lock()
        # 统计(可观测)
        self.stats: dict[str, int] = {
            "received": 0,
            "deduped": 0,
            "passed": 0,
            "evicted": 0,
        }

    async def normalize(self, event: BaseEvent) -> BaseEvent | None:
        """归一化:同 event_id 在 window 内已见过 → 丢弃(None)。

        Returns:
            - ``None`` — 重复事件,丢弃
            - ``event``(原事件)— 放行
        """
        async with self._lock:
            self.stats["received"] += 1
            now = time.monotonic()
            # 1) 淘汰过期 key(扫一遍 OrderedDict,数量受 max_size 限制)
            self._evict_expired(now)
            # 2) 检查 event_id 是否在窗口内已见过
            key = event.event_id
            if key in self._seen:
                # 计算距离第一次的间隔
                first_seen = self._seen[key]
                if (now - first_seen) <= self._window_sec:
                    self.stats["deduped"] += 1
                    logger.debug(
                        "dedup_normalizer.drop event_id=%s topic=%s age=%.2fs",
                        key, event.topic, now - first_seen,
                    )
                    return None
                # 超过窗口 → 视为新事件(刷新时间戳)
            # 3) 放行,记录首次见到时间
            self._seen[key] = now
            # 4) 超过 max_size → LRU 淘汰最早的
            while len(self._seen) > self._max_size:
                evicted_key, _ = self._seen.popitem(last=False)
                self.stats["evicted"] += 1
                logger.debug(
                    "dedup_normalizer.evict event_id=%s (max_size=%d)",
                    evicted_key, self._max_size,
                )
            self.stats["passed"] += 1
            return event

    def _evict_expired(self, now: float) -> None:
        """淘汰过期的 seen key(超过 window_sec 的)。

        ⚠️ 不在 lock 内调用,normalize() 入口处持有 lock 调它。
        """
        expired_keys: list[str] = []
        for k, ts in list(self._seen.items()):
            if (now - ts) > self._window_sec:
                expired_keys.append(k)
            else:
                # 有序 dict 早期插入 = 早期时间,只要第一个没过期,后面也大概率没过期
                break
        for k in expired_keys:
            self._seen.pop(k, None)

    def reset(self) -> None:
        """清空 seen 表(单测 / 运维用)。"""
        self._seen.clear()
        self.stats = {
            "received": 0,
            "deduped": 0,
            "passed": 0,
            "evicted": 0,
        }

    def snapshot(self) -> dict[str, Any]:
        """导出当前状态(可观测性 / 单测断言用)。"""
        return {
            "window_sec": self._window_sec,
            "max_size": self._max_size,
            "seen_count": len(self._seen),
            "stats": dict(self.stats),
        }


__all__ = ["DedupNormalizer"]
