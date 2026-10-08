"""ProactiveReasoning —— "该不该主动开口"的判定逻辑。

## 职责

接收一个归一化后的事件 ``BaseEvent``,产出 ``ProactiveDecision``。
**单一职责**:只判断要不要主动,不决定"说什么"——"说什么"是 ProactiveLoop 后续 ReAct 的事。

## 双轨部署(架构约束 #9:降级契约)

**当前已有两套实现,全部永久保留**——这是双轨部署,**不是**"占位 → 真实化"的替代关系:

1. **降级后备 (RuleBasedProactiveReasoning)** —— 4 条硬编码规则,SILENT/URGENT/同 topic dedup/默认沉默
2. **真实化版 (LLMProactiveReasoning)** —— Phase 6b 实现,读 Memory + 读 Persona + 调 LLM 综合判断

**降级语义**:
- ProactiveLoop 默认走真实化版(Phase 6b 上线后)
- 真实化版抛任何异常(LLM 服务挂 / key 失效 / timeout / 解析失败) → **自动降级到 RuleBasedProactiveReasoning**
- 降级后备**永不被删**——它是**安全网**,保障 URGENT 事件(漏水/烟雾/门铃等安全相关)
  在 LLM 不可用时仍能主动开口

**设计原则**:RuleBasedProactiveReasoning 行为**保守**——大多数事件都会 silent,
不会因为"降级版太激进"而打扰用户。

## 不变量

1. **默认 silent** — ``should_respond=False`` 是基线
2. **紧急事件例外** — ``EventPriority.URGENT`` 必主动
3. **可替换** — LLM 版可作为 ``ProactiveReasoning`` 实现注入,与降级后备并存
4. **降级路径不可断** — ProactiveLoop 必须在 LLM 故障时降级到 RuleBasedProactiveReasoning
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol

import structlog

from smartbutler.events.core.event import BaseEvent, EventPriority
from smartbutler.thinking.proactive.decision import (
    ProactiveDecision,
    ProactiveUrgency,
)

_logger = structlog.get_logger(__name__)


class ProactiveReasoning(Protocol):
    """ProactiveReasoning 协议(接口)。

    已有实现(双轨部署,架构约束 #9):
    - ``RuleBasedProactiveReasoning``:降级后备,LLM 故障时启用
    - ``LLMProactiveReasoning``(Phase 6b 实现):主路,读 Memory + 读 Persona + 调 LLM
    """

    async def should_respond(
        self,
        event: BaseEvent,
        *,
        now: datetime | None = None,
    ) -> ProactiveDecision:
        """判断事件 ``event`` 是否需要主动开口。

        Args:
            event: 归一化后的事件。
            now: 当前时间(可注入便于单测,默认 ``datetime.now(UTC)``)。

        Returns:
            ProactiveDecision —— 不可变,should_respond=False 即沉默。
        """
        ...


# 类型别名:可注入的 ProactiveReasoning 工厂/单例
ProactiveReasoningFactory = Callable[[], "ProactiveReasoning"]


class RuleBasedProactiveReasoning:
    """基于规则的 ProactiveReasoning——**降级后备**(架构约束 #9:降级契约)。

    判定规则(按优先级排序,首个命中即返):

    1. ``event.priority == SILENT`` → silent
    2. ``event.priority == URGENT`` → 主动(MED 起步;alarm 关键字升 HIGH)
    3. **同 user_id 5 分钟内同 topic 重复** → silent(防事件风暴)
    4. 其他 → silent(默认沉默)

    **本类永不被删**——它是 LLM 版 ProactiveReasoning 的**降级后备**:

    - ProactiveLoop 默认走 LLM 版(Phase 6b 上线后)
    - LLM 版抛任何异常(LLM 服务挂 / key 失效 / timeout / 解析失败) → 自动降级到本类
    - 降级期间仍能识别 URGENT 事件主动开口——不依赖 LLM 也能保底
    - 与"占位"语义不同:占位暗示"未来会被替换",降级后备暗示"长期共存、永久保留"
    """

    # 同 user 重复 topic 沉默窗口(秒)
    _DEDUP_WINDOW_SECONDS: int = 300

    def __init__(self) -> None:
        # 内存级 dedup 状态:key = (user_id, topic),value = last seen timestamp
        # Phase 7 接入 Redis 后,这里换 Redis-backed 实现
        self._seen: dict[tuple[str, str], datetime] = {}

    async def should_respond(
        self,
        event: BaseEvent,
        *,
        now: datetime | None = None,
    ) -> ProactiveDecision:
        """规则判定。"""
        now = now or datetime.now(UTC)

        # 规则 1: SILENT 优先级 → 沉默
        if event.priority == EventPriority.SILENT:
            _logger.debug(
                "proactive_reasoning.silent_priority",
                event_id=event.event_id,
                topic=event.topic,
            )
            return ProactiveDecision.silent(reason="event_priority_silent")

        # 规则 3: 5 分钟内同 (user_id, topic) 重复 → 沉默
        dedup_key = (event.user_id, event.topic)
        last_seen = self._seen.get(dedup_key)
        if last_seen is not None:
            elapsed = (now - last_seen).total_seconds()
            if elapsed < self._DEDUP_WINDOW_SECONDS:
                _logger.debug(
                    "proactive_reasoning.dedup_silent",
                    event_id=event.event_id,
                    topic=event.topic,
                    elapsed_seconds=elapsed,
                )
                return ProactiveDecision.silent(reason="dedup_window")
        self._seen[dedup_key] = now

        # 规则 2: URGENT 优先级 → 主动
        if event.priority == EventPriority.URGENT:
            urgency = ProactiveUrgency.HIGH
            # alarm / smoke / leak 关键字升 HIGH
            topic_lower = event.topic.lower()
            if any(kw in topic_lower for kw in ("alarm", "smoke", "leak", "alert")):
                urgency = ProactiveUrgency.HIGH
            else:
                urgency = ProactiveUrgency.MED

            _logger.info(
                "proactive_reasoning.urgent_acted",
                event_id=event.event_id,
                topic=event.topic,
                urgency=urgency.value,
            )
            return ProactiveDecision(
                should_respond=True,
                reason=f"event_priority_urgent: {event.topic}",
                urgency=urgency,
                suggested_tone="紧急通知",
                context_hints=[f"event.topic={event.topic}"],
            )

        # 规则 4: 默认沉默
        _logger.debug(
            "proactive_reasoning.default_silent",
            event_id=event.event_id,
            topic=event.topic,
            priority=event.priority.value,
        )
        return ProactiveDecision.silent(reason="default_silent")

    def reset_dedup_state(self) -> None:
        """清空 dedup 状态(单测 / 管理员工具用)。"""
        self._seen.clear()


# Phase 6b LLM 真实化版 —— 实际实现是类 LLMProactiveReasoning,见 Phase 6b 任务
# 当前保留这个函数用于单测/接口文档示例,真实化时换成 LLMProactiveReasoning 类
async def _llm_based_should_respond_placeholder(
    event: BaseEvent,
    *,
    now: datetime | None = None,  # noqa: ARG001
) -> ProactiveDecision:
    """LLM 版 ProactiveReasoning 占位(Phase 6b 实现)。

    预期实现(Phase 6b 启动后):
    1. 查 Memory: "用户最近在减肥 / 用户失业过"
    2. 查 Persona: "用户当前心情 / 用户偏好"
    3. 拼 LLM prompt: "事件 X + 用户状态 Y → 该不该说?"
    4. 返 ProactiveDecision

    当前:先不实现,直接 silent。
    重要:ProactiveLoop 上线后默认走 LLMProactiveReasoning(待实现),失败时降级到 RuleBasedProactiveReasoning。
    """
    return ProactiveDecision.silent(reason="phase6b_llm_not_implemented")


__all__ = [
    "ProactiveReasoning",
    "ProactiveReasoningFactory",
    "RuleBasedProactiveReasoning",
    "_llm_based_should_respond_placeholder",
]
