"""RouteResult dataclass — AnswerRouter.route 的返回值(Phase 7.5 D1)。

替代原 ``AnswerRouter.route`` 的 ``bool`` 返回值。

为什么不用 bool:
- 返 bool 只能表示"成功 / 已 fallback"
- 无法告诉调用方:推了几个设备?推的哪几个?哪个失败?
- RouteResult 可观测性好,单测好写,日志字段化

决策记录:2026-10-14 D1。
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class RouteResult:
    """AnswerRouter.route 的返回值。"""

    # 是否有任何 OutputAdapter 被触达(False = SILENT / 全部失败)
    acted: bool
    # 实际推送到的 device_id 列表,顺序同 push 调用顺序
    pushed_device_ids: list[str] = field(default_factory=list)
    # PresenceService 返 None → 走了 fallback,调用方应打 INFO 日志
    fallback_used: bool = False
    # acted=False 时的原因(SILENT / 全部 push 失败 / device not found)
    silence_reason: str | None = None
    # 推送失败的 device_id(可能 device 离线 / 超时)
    error_device_ids: list[str] = field(default_factory=list)

    @property
    def success(self) -> bool:
        """推了且无错误。"""
        return self.acted and not self.error_device_ids

    def __str__(self) -> str:
        parts = [f"acted={self.acted}"]
        if self.pushed_device_ids:
            parts.append(f"pushed={self.pushed_device_ids}")
        if self.fallback_used:
            parts.append("fallback_used=True")
        if self.silence_reason:
            parts.append(f"reason={self.silence_reason!r}")
        if self.error_device_ids:
            parts.append(f"errors={self.error_device_ids}")
        return "RouteResult(" + ", ".join(parts) + ")"


__all__ = ["RouteResult"]
