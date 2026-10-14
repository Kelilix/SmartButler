"""Task 边界检测(Phase 6.3 P1)。

设计要点:
1. **纯规则,无 LLM** —— 给短期记忆切 task,需要低延迟 + 可解释
2. **3 种边界规则**:
   - 规则 1:用户消息间隔 > N 分钟(默认 30)→ 新 task
   - 规则 2:管家明确回复"任务完成" + 用户首次发言 → 新 task
   - 规则 3:触发词检测(用户说"再做一件事"/"另外"等)→ 新 task
3. **API 极简** —— ``detect_boundary(prev_turn, current_turn) -> bool``
4. **可注入** —— ``TaskBoundaryDetector(threshold_minutes=30)`` 方便测试

为什么不调 LLM:
- 延迟:每次 LLM 调用 1-2s,会拖累 recall 路径
- 可解释:规则检测器输出 "30 min interval" vs LLM 输出"看起来是新话题",前者好调试
- 边界模糊无害:误判成"新 task"的最大代价 = 上一 task 摘要没带入,反正 recall 也会补
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, UTC
from typing import Any

from smartbutler.utils.logging import get_logger

logger = get_logger(__name__)


# 触发词(用户明确说"换话题"/"另外"等)
# 2026-10-10 扩展:加 "帮我"/"我要"/"请" 等任务导向词,
# 否则场景 1 第 3 轮 "出差,帮我订票" 不会被判 boundary
_TRIGGER_PHRASES = (
    "另外",
    "再做一件事",
    "再帮我",
    "对了",
    "顺便",
    "说起来",
    "换个话题",
    "聊点别的",
    "帮我",          # 🆕 任务导向
    "我要",          # 🆕 任务导向
    "请帮我",        # 🆕 任务导向
)

# 管家"任务完成"标志(用于规则 2)
_BUTLER_DONE_PATTERNS = re.compile(
    r"(任务完成|已完成|完成|搞定|好的,.*?完成|办妥了)",
)


@dataclass
class TurnInfo:
    """单轮对话信息(task 边界检测的输入)。"""

    role: str                     # "user" / "assistant" / "system"
    content: str
    timestamp: datetime | None = None  # None = 未知(测试时可用)


@dataclass
class BoundaryResult:
    """task 边界检测结果。"""

    is_boundary: bool
    reason: str                   # "interval" / "butler_done" / "trigger" / "none"
    detail: str = ""              # 详细说明(测试/审计用)


class TaskBoundaryDetector:
    """task 边界检测器(纯规则)。

    典型用法::

        det = TaskBoundaryDetector(threshold_minutes=30)
        result = det.detect(prev_turn=prev, current_turn=cur)
        if result.is_boundary:
            # 切新 task,只带上一 task 摘要
            ...
    """

    def __init__(self, threshold_minutes: int = 30) -> None:
        if threshold_minutes <= 0:
            raise ValueError(
                f"threshold_minutes 必须 > 0,得到 {threshold_minutes}"
            )
        self._threshold_minutes = threshold_minutes

    @property
    def threshold_minutes(self) -> int:
        return self._threshold_minutes

    def detect(
        self,
        prev_turn: TurnInfo | None,
        current_turn: TurnInfo,
    ) -> BoundaryResult:
        """检测 current_turn 是否是 task 起点。

        Args:
            prev_turn: 上一轮对话(None = 第一条消息,视为 task 起点)。
            current_turn: 当前轮对话。

        Returns:
            :class:`BoundaryResult`。
        """
        if prev_turn is None:
            return BoundaryResult(is_boundary=True, reason="none", detail="first_turn")

        # 规则 1:消息间隔
        if (
            prev_turn.timestamp is not None
            and current_turn.timestamp is not None
        ):
            delta = (current_turn.timestamp - prev_turn.timestamp).total_seconds()
            threshold_sec = self._threshold_minutes * 60
            if delta > threshold_sec:
                return BoundaryResult(
                    is_boundary=True,
                    reason="interval",
                    detail=f"{int(delta // 60)} min > {self._threshold_minutes} min",
                )

        # 规则 2:管家"任务完成" + 用户首次发言
        if prev_turn.role == "assistant" and current_turn.role == "user":
            if _BUTLER_DONE_PATTERNS.search(prev_turn.content):
                return BoundaryResult(
                    is_boundary=True,
                    reason="butler_done",
                    detail="butler replied done marker, user new turn",
                )

        # 规则 3:触发词
        if current_turn.role == "user":
            for phrase in _TRIGGER_PHRASES:
                if phrase in current_turn.content:
                    return BoundaryResult(
                        is_boundary=True,
                        reason="trigger",
                        detail=f"trigger phrase: {phrase}",
                    )

        return BoundaryResult(is_boundary=False, reason="none", detail="")


__all__ = ["TaskBoundaryDetector", "TurnInfo", "BoundaryResult"]
