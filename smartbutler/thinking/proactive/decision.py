"""Proactive / Reactive 双循环的"决策"和"结果"模型。

## 设计原则（参考 TECHNICAL_DESIGN.md §5.9 + ADR-009）

1. **沉默是一等公民**：``ProactiveResult.acted=False`` 必须能被上游正确处理
2. **可序列化**：所有字段 Pydantic BaseModel,方便日志/事件总线传输
3. **不可变**：``frozen=True`` —— 决策一旦产出,不再修改
4. **字段稳定**：这些字段是 Phase 5+ 的对外契约,后期修改需新增 ADR

## 三个模型

- ``ProactiveUrgency`` : 主动建议的紧急度(low / med / high),影响推送渠道
- ``ProactiveDecision`` : "该不该主动"的判断结果
- ``ProactiveResult``  : "管家做了什么"的结果(acted / silent)
"""
from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class ProactiveUrgency(StrEnum):
    """主动建议的紧急度。

    决定 ``AnswerRouter`` 路由策略:
    - LOW  — 推到默认设备(音响/手机)
    - MED  — 推到默认设备 + 加大音量
    - HIGH — 推全屋设备,紧急通知
    """

    LOW = "low"
    MED = "med"
    HIGH = "high"


class ProactiveDecision(BaseModel):
    """ProactiveReasoning 产出的"该不该主动"决策。

    Attributes:
        should_respond: 是否需要主动开口(``False`` = 沉默)
        reason: 决策理由(供日志 + LLM 后续生成时参考)
        urgency: 紧急度,影响推送渠道
        suggested_tone: 建议语气,如"温柔提醒"/"直接建议"/"沉默"
        context_hints: 给后续 LLM 的上下文提示
            (例: ``["用户在减肥", "用户不喜欢香菜"]``)
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    should_respond: bool = Field(..., description="是否需要主动开口")
    reason: str = Field(..., min_length=1, description="决策理由")
    urgency: ProactiveUrgency = Field(
        default=ProactiveUrgency.LOW,
        description="紧急度,影响推送渠道",
    )
    suggested_tone: str = Field(
        default="温柔提醒",
        description="建议语气",
    )
    context_hints: list[str] = Field(
        default_factory=list,
        description="给后续 LLM 的上下文提示",
    )

    @classmethod
    def silent(cls, reason: str = "default_silent") -> ProactiveDecision:
        """便捷构造器:沉默决策。

        70%+ 的 ProactiveReasoning 决策应该走这个——好管家话不多。
        """
        return cls(
            should_respond=False,
            reason=reason,
            urgency=ProactiveUrgency.LOW,
            suggested_tone="沉默",
            context_hints=[],
        )


class ProactiveResult(BaseModel):
    """ProactiveLoop 的最终结果。

    Attributes:
        acted: 是否产生了一条主动推送(``False`` = 沉默)
        message: 主动推送的消息内容(沉默时为 ``None``)
        push_channel: 推送渠道,如 ``"speaker"`` / ``"phone"`` / ``"email"``
            (``None`` = 走 AnswerRouter 默认路由)
        silence_reason: 沉默原因(``acted=False`` 时填)
            例: ``"default_silent"`` / ``"urgency_too_low"`` / ``"no_advice_to_give"``
        urgency: 紧急度(透传自 ProactiveDecision)
        related_event_id: 关联的 event_id,供 AnswerRouter / 日志关联
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    acted: bool = Field(..., description="是否产生了一条主动推送")
    message: str | None = Field(
        default=None,
        description="主动推送的消息内容(沉默时为 None)",
    )
    push_channel: str | None = Field(
        default=None,
        description="推送渠道;None 表示走 AnswerRouter 默认路由",
    )
    silence_reason: str | None = Field(
        default=None,
        description="沉默原因(acted=False 时必填)",
    )
    urgency: ProactiveUrgency = Field(
        default=ProactiveUrgency.LOW,
        description="紧急度(透传自 ProactiveDecision)",
    )
    related_event_id: str | None = Field(
        default=None,
        description="关联的 event_id,供 AnswerRouter / 日志关联",
    )

    @classmethod
    def silent(
        cls,
        reason: str = "default_silent",
        related_event_id: str | None = None,
    ) -> ProactiveResult:
        """便捷构造器:沉默结果。"""
        return cls(
            acted=False,
            message=None,
            push_channel=None,
            silence_reason=reason,
            urgency=ProactiveUrgency.LOW,
            related_event_id=related_event_id,
        )

    @classmethod
    def acted_with(
        cls,
        message: str,
        *,
        urgency: ProactiveUrgency = ProactiveUrgency.LOW,
        push_channel: str | None = None,
        related_event_id: str | None = None,
    ) -> ProactiveResult:
        """便捷构造器:主动推送结果。"""
        if not message or not message.strip():
            msg = "acted_with 不允许空 message"
            raise ValueError(msg)
        return cls(
            acted=True,
            message=message.strip(),
            push_channel=push_channel,
            silence_reason=None,
            urgency=urgency,
            related_event_id=related_event_id,
        )


__all__ = [
    "ProactiveUrgency",
    "ProactiveDecision",
    "ProactiveResult",
]
