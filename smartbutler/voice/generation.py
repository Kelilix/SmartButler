"""语音输入代次追踪(Generation Tracking)—— L1/L2 打断的基础设施。

## 要解决的问题(2026-11-10)

用户连续说话时,管家会"把之前说的依次说完":

    用户:"帮我查下明天天气"      → gen=1 → LLM 处理中(10s)
    用户:"对了,顺便订张去上海机票" → gen=2 → 排队
    用户:"对了,我的身份证号是..."  → gen=3 → 排队
    10|                                       ↓
    LLM 算完 gen=1 → 播报 → 用户:"我不想听这个"
    LLM 算完 gen=2 → 播报 → 用户:"不是这个"
    LLM 算完 gen=3 → 播报 → 用户:"我早换了话题"

根因有两层:
1. **队列层**:EventBus 单消费者串行处理,LLM 慢 → 事件排队(积压)
2. **播放层**:LLM 算完就播,不管期间用户有没有说新的

## 方案:代次时间戳(generation = 单调递增的毫秒时间戳)

给每次用户输入分配一个**严格单调递增**的整数代次。这个整数的语义是
**"这次发言的 monotonic 毫秒时间戳"**。

    20|
    用户:"查天气"(gen=T1) → 用户:"订机票"(gen=T2, T2 > T1)
    → 队列里 gen=T1 被丢弃,只处理 gen=T2 ✅

### 为什么用时间戳而不是简单计数器

用**整数毫秒时间戳**而不是裸的 `seq += 1`,换来三个能力:

1. **自带时间语义** —— 直接知道"这次发言距今多久",不必再查旁路字典
2. **跨进程可比** —— 时间戳在重启后依然有意义(计数器会归零)
3. **日志可读** —— 日志里能直接看到"这句是 14:23:45 说的",排查问题直观

### 严格单调递增的保证

⚠️ ``time.monotonic()`` 在极端情况下可能返回**相同值**(时钟精度限制),
这会破坏"代次大小 == 发言先后"的等价关系。因此签发时强制:

    ts = max(int(now * 1000), previous + 1)

即"同一毫秒内的两次发言"会退化成 `T, T+1`(1ms 间隔)。
真人说话不可能快到 1ms 内两次,这个兜底在实际场景永远不会触发,
但保证了不变式:``generation`` 严格递增 ⇒ 可直接用大小比较判断先后。

### L1(不积压):队列层丢弃

消费事件前检查代次 —— 如果队列里这条已经不是最新的,直接丢弃,
**不进 LLM**(省算力,也避免 LLM 被过时上下文污染)。

    用户:"查天气"(gen=T1) → 用户:"订机票"(gen=T2)
    → 队列里 gen=T1 被丢弃,只处理 gen=T2 ✅

### L2(不播过期回复):播放前检查

LLM 算完之后、播报之前再检查一次 —— 如果期间用户又说了话,
这条回复已经**过时**,不该播。

    LLM 算 gen=T1 (10s) → 期间用户说了 gen=T2 → T1 过期 → 不播 ✅

## 过期策略:严格 vs 宽松

**严格模式**(:attr:`GenerationTracker.stale_window_seconds` = 0,默认):
只要有更新的输入就算过期。适合"用户明确换话题"的场景。

**宽松模式**(:attr:`GenerationTracker.stale_window_seconds` > 0):
只有"最近窗口内出现的更新输入"才算过期。避免用户随口一句"嗯"
就把整条回复作废。

⚠️ 宽松判定的关键是看 **"最新那次更新距今多久"**,而不是
"两个代次之间隔了多久":

    T=0   用户:"帮我查天气"(gen=T1)
    T=5   用户:"嗯"(gen=T2)
    T=100 LLM 算完 T1

    - 两个代次间隔 = 5s ≤ window(30s) → 若按"间隔"判 → 过期 ❌
    - 最新更新距今 = 95s > window(30s)  → 按"距今"判 → 不过期 ✅

后者的语义才对:"95 秒前用户随口说了句'嗯'"**不该**让这条回复作废。
本模块的宽松判定统一采用"距今"口径。

## 线程/协程安全

用 ``asyncio.Lock`` 保护状态变更 —— EventBus 的 consumer 是单协程,
但 publish 可能来自别的协程(如麦克风采集循环)。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

#: 单调时钟起点可能非常接近 0 → 用它作为"未知代次"的哨兵值不可靠。
#: 实际签发的代次至少从这个值起步,保证 0 永远表示"无代次"。
_MIN_GENERATION = 1


@dataclass
class GenerationState:
    """代次追踪状态快照(可观测用)。"""

    #: 最新一次用户输入的代次(单调递增的毫秒时间戳)
    latest_seq: int = 0
    #: 最新一次用户输入的时间戳(``time.monotonic()`` 原始浮点秒)
    latest_at: float = 0.0
    #: 各代次的创建时间(用于宽松过期判断)
    issued_at: dict[int, float] = field(default_factory=dict)
    #: 统计
    stats: dict[str, int] = field(default_factory=lambda: {
        "issued": 0,       # 分配了多少个代次
        "dropped_stale": 0,  # L1:队列层丢弃(过期事件)
        "dropped_speech": 0,  # L2:播放层丢弃(过期回复)
    })


class GenerationTracker:
    """语音输入代次追踪器。

    Usage:
        tracker = GenerationTracker()

        # 用户说了新话 → 分配代次
        gen = tracker.issue()
        await bus.publish(event)   # event 里带上 gen(或用 tracker 查)

        # L1:消费前检查
        if tracker.is_stale(gen):
            tracker.note_dropped_stale()
            return   # 丢弃,不进 LLM

        # LLM 处理...

        # L2:播放前检查
        if tracker.is_stale(gen):
            tracker.note_dropped_speech()
            return   # 不播这条回复
    """

    def __init__(self, *, stale_window_seconds: float = 0.0) -> None:
        """构造。

        Args:
            stale_window_seconds: 宽松过期窗口(秒)。
                0 = 严格模式(只要有更新的输入就算过期)。
                >0 = 只有"最近该时间窗内出现的更新输入"才算过期
                (避免用户随口一句"嗯"就让整条回复作废)。
        """
        self._lock = asyncio.Lock()
        self._latest_seq = 0
        self._latest_at = 0.0
        self._issued_at: dict[int, float] = {}
        self._stale_window = stale_window_seconds
        self._stats = {
            "issued": 0,
            "dropped_stale": 0,
            "dropped_speech": 0,
        }

    # ------------------------------------------------------------------
    # 核心 API
    # ------------------------------------------------------------------

    async def issue(self) -> int:
        """分配一个新代次号(用户又说话了)。

        Returns:
            严格单调递增的**毫秒时间戳**(``time.monotonic()`` 基准)。
            ``0`` 保留表示"无代次"。
        """
        async with self._lock:
            now = time.monotonic()
            ts_ms = int(now * 1000)
            # ⚠️ 严格单调递增保护:同一毫秒内的两次发言必须区分开,
            # 否则"代次大小 == 发言先后"的等价关系会破。
            # 真人说话间隔远大于 1ms,这个兜底实际不会触发。
            if ts_ms <= self._latest_seq:
                ts_ms = self._latest_seq + 1
            if ts_ms < _MIN_GENERATION:
                ts_ms = _MIN_GENERATION

            self._latest_seq = ts_ms
            self._latest_at = now
            self._issued_at[ts_ms] = now
            self._stats["issued"] += 1
            # 清理过老的记录(只保留最近 100 个,防内存泄漏)
            if len(self._issued_at) > 100:
                cutoff = self._latest_seq - 100
                self._issued_at = {
                    k: v for k, v in self._issued_at.items() if k > cutoff
                }
            return ts_ms

    async def issue_for(self, event: Any) -> Any:
        """给一个事件打上代次号(并返回该事件)。

        ⚠️ **推荐用这个方法**,不要手写 ``generation=await tracker.issue()``。

        原因:代次号有两个来源 —— tracker 的签发 和 事件上的字段。
        如果两者不一致(比如手写事件时写死了数字),过期判断会失准:
            tracker.latest_seq() == 1,但事件上写着 generation=2
            → ``is_stale(2)`` 判定为"不是最新的"→ 事件被误丢。

        由 tracker 统一签发,保证两边永远一致。

        Args:
            event: 任意支持 ``model_copy`` 的 pydantic 模型
                (通常是 :class:`VoiceEvent`)。

        Returns:
            带好 ``generation`` 的事件副本。
        """
        gen = await self.issue()
        return event.model_copy(update={"generation": gen})

    def latest_seq(self) -> int:
        """当前最新代次号(0 = 还没发过言)。"""
        return self._latest_seq

    def is_stale(self, seq: int, *, now: float | None = None) -> bool:
        """判断某个代次是否已过期。

        Args:
            seq: 待检查的代次号。
            now: 当前时间(None → 用 ``time.monotonic()``)。仅宽松模式用。

        Returns:
            True = 已有更新的输入,这条已过时。
        """
        if seq <= 0:
            return False
        if seq >= self._latest_seq:
            return False
        if self._stale_window <= 0:
            return True
        # 宽松模式:只看"**最新那次更新距今多久**"——
        # 用户 30 秒前随口说的"嗯"不该让一条回复作废。
        reference = now if now is not None else time.monotonic()
        if (reference - self._latest_at) > self._stale_window:
            return False
        return True

    def is_stale_since(self, seq: int, *, now: float | None = None) -> bool:
        """宽松判断:从代次 ``seq`` 之后是否有过**有效**的新输入。

        与 :meth:`is_stale` 的区别:忽略那些发生在"很久以前"的更新 ——
        用于"回复算完时,如果用户是 30 秒前才说的新话,仍然播报"的场景。

        Args:
            seq: 基线代次号。
            now: 当前时间(None → 用 ``time.monotonic()``)。

        Returns:
            True = 有过更新的输入。
        """
        if seq <= 0:
            return False
        if seq >= self._latest_seq:
            return False
        reference = now if now is not None else time.monotonic()
        if self._stale_window > 0:
            # 只有最近窗口内的更新才算
            if (reference - self._latest_at) > self._stale_window:
                return False
        return True

    # ------------------------------------------------------------------
    # 统计
    # ------------------------------------------------------------------

    def note_dropped_stale(self) -> None:
        """记一次 L1 丢弃(队列层:过期事件没进 LLM)。"""
        self._stats["dropped_stale"] += 1

    def note_dropped_speech(self) -> None:
        """记一次 L2 丢弃(播放层:过期回复没播)。"""
        self._stats["dropped_speech"] += 1

    def snapshot(self) -> dict[str, object]:
        """导出状态(可观测 / 单测断言用)。"""
        return {
            "latest_seq": self._latest_seq,
            "stale_window_seconds": self._stale_window,
            "stats": dict(self._stats),
        }

    def reset(self) -> None:
        """重置(单测 / 运维用)。"""
        self._latest_seq = 0
        self._latest_at = 0.0
        self._issued_at.clear()
        self._stats = {
            "issued": 0,
            "dropped_stale": 0,
            "dropped_speech": 0,
        }


__all__ = ["GenerationTracker", "GenerationState"]
