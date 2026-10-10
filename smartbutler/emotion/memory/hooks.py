"""MemoryHooks(Phase 6.2 P0)。

事件钩子系统,给 6.3 consolidation / 6.5 habit / Phase 6b ProactiveLoop 留接入点。

设计要点:
1. **单实例** —— facade 创建时注入,thinking / proactive 共享
2. **触发 try/except 隔离** —— 任一 callback 抛异常不影响主流程
3. **P0 阶段 callback 全 stub** —— 仅 ``logger.info``;6.3+ 挂真实实现
4. **4 个事件** —— 覆盖存储 / 召回 / 固化 / 习惯发现

事件清单:
- ``ON_STORE`` —— 长期记忆写入(每次 ``remember()`` 触发)
- ``ON_RECALL`` —— 长期记忆被召回(每次 ``recall()`` 触发,每条 hit 一次)
- ``ON_PROMOTE`` —— 6.3 固化触发(留口子,P0 不调)
- ``ON_HABIT_DETECTED`` —— 6.5 习惯发现(留口子,P0 不调)
"""

from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum
from typing import Any

from smartbutler.utils.logging import get_logger

logger = get_logger(__name__)


class MemoryEventType(StrEnum):
    """Memory 事件枚举。"""

    ON_STORE = "on_store"
    ON_RECALL = "on_recall"
    ON_PROMOTE = "on_promote"
    ON_HABIT_DETECTED = "on_habit_detected"


# callback 类型:接受 (key, ...) 等参数,返回 None
MemoryCallback = Callable[..., None]


class MemoryHooks:
    """Memory 事件钩子注册 + 触发器。"""

    def __init__(self) -> None:
        self._callbacks: dict[MemoryEventType, list[MemoryCallback]] = {
            t: [] for t in MemoryEventType
        }
        # 注册默认 stub(只记日志,后续 6.3 在此加 consolidation 触发)
        self.register(MemoryEventType.ON_STORE, self._default_on_store)
        self.register(MemoryEventType.ON_RECALL, self._default_on_recall)

    def register(
        self, event: MemoryEventType, callback: MemoryCallback
    ) -> None:
        """注册回调。同 event 可注册多个,按注册顺序触发。

        Args:
            event: 事件类型。
            callback: ``def cb(**kwargs) -> None`` 形式的回调,参数见各事件约定。
        """
        if not callable(callback):
            raise TypeError(f"callback 必须是 callable,得到 {type(callback).__name__}")
        self._callbacks[event].append(callback)

    def trigger(
        self, event: MemoryEventType, *args: Any, **kwargs: Any
    ) -> None:
        """触发指定事件的所有 callback。任一抛异常被捕获 + warn。"""
        for cb in self._callbacks[event]:
            try:
                cb(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "memory_hook_callback_failed",
                    hook_event=event.value,
                    callback=getattr(cb, "__name__", repr(cb)),
                    error=str(exc),
                )

    def callback_count(self, event: MemoryEventType) -> int:
        """(测试用) 返回某事件当前注册的 callback 数。"""
        return len(self._callbacks[event])

    # ---------- 默认 stub callbacks(只记日志) ----------

    @staticmethod
    def _default_on_store(*, key: str, content: str) -> None:
        """``on_store`` 默认 stub:只记日志。"""
        logger.info(
            "memory.on_store",
            key=key,
            content_preview=(content[:50] + "...") if len(content) > 50 else content,
        )

    @staticmethod
    def _default_on_recall(*, key: str, score: float) -> None:
        """``on_recall`` 默认 stub:只记日志。"""
        logger.info("memory.on_recall", key=key, score=round(score, 4))


__all__ = ["MemoryHooks", "MemoryEventType", "MemoryCallback"]
