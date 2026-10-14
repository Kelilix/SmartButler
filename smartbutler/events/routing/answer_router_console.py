"""ConsoleAnswerRouter — AnswerRouter 最小实现(Phase 7.5)。

职责:拿到 ``BaseEvent + answer + priority``,选目标 OutputAdapter 列表,然后 ``await`` push 完成。

改造点(D1/D2/D4):
- route() 返 RouteResult 而非 bool
- 注入 DeviceRegistry
- 注入 PresenceService
- route() 内部 await 所有 push 完成再返

路由策略矩阵(§4.1):

| priority | 正常情况 | 紧急情况 (URGENT) |
|----------|----------|------------------|
| NORMAL   | 推 PresenceService 选定的设备 | 推 PresenceService 选定的设备 |
| URGENT   | —                    | 推全屋设备,加大音量 |
| SILENT   | 不推(打 DEBUG 日志)  | — |

决策记录:2026-10-14 D1/D2/D4。
"""
from __future__ import annotations

import asyncio

import structlog

from smartbutler.events.core.event import BaseEvent, EventPriority
from smartbutler.events.routing.answer_router import AnswerRouter
from smartbutler.events.routing.device_registry import DeviceRegistry
from smartbutler.events.routing.presence import PresenceService
from smartbutler.events.routing.route_result import RouteResult
from smartbutler.server.adapters import ButlerReply, OutputAdapter

_logger = structlog.get_logger(__name__)


class ConsoleAnswerRouter(AnswerRouter):
    """AnswerRouter 最小实现:in-memory dict PresenceService + DeviceRegistry。

    用法::

        presence = SimplePresenceService()
        registry = DeviceRegistry([speaker1, phone])
        router = ConsoleAnswerRouter(registry=registry, presence_service=presence)
        result = await router.route(event, "欢迎回家", priority=EventPriority.URGENT)
    """

    def __init__(
        self,
        registry: DeviceRegistry,
        presence_service: PresenceService,
    ) -> None:
        self._registry = registry
        self._presence = presence_service

    async def route(
        self,
        event: BaseEvent,
        answer: str,
        *,
        priority: EventPriority | None = None,
    ) -> RouteResult:
        """路由 answer 到目标设备。

        内部 await 所有 push 完成再返(D4)。
        """
        effective_priority = priority or event.priority

        # SILENT:不推
        if effective_priority == EventPriority.SILENT:
            _logger.debug(
                "answer_router.silent",
                event_id=event.event_id,
                topic=event.topic,
            )
            return RouteResult(acted=False, silence_reason="SILENT")

        # URGENT:推所有
        if effective_priority == EventPriority.URGENT:
            return await self._push_all(answer, event)

        # NORMAL:按 PresenceService 选
        return await self._push_normal(answer, event)

    async def _push_normal(self, answer: str, event: BaseEvent) -> RouteResult:
        """NORMAL:按 PresenceService 选设备推送。"""
        presence = await self._presence.get_active_device(event.user_id)

        # PresenceService 返 None → fallback
        if presence is None:
            first = self._registry.first()
            if first is None:
                _logger.warning(
                    "answer_router.no_adapter_for_normal",
                    user_id=event.user_id,
                )
                return RouteResult(
                    acted=False,
                    silence_reason="no_output_adapter",
                )
            _logger.info(
                "answer_router.presence_fallback",
                user_id=event.user_id,
                device_id=self._registry._device_id(first),
            )
            result = await self._push_one(first, answer)
            return RouteResult(
                acted=result.acted,
                pushed_device_ids=result.pushed_device_ids,
                fallback_used=True,
                silence_reason=result.silence_reason,
                error_device_ids=result.error_device_ids,
            )

        # 检查 DND / sleeping
        if not presence.should_notify("normal"):
            _logger.debug(
                "answer_router.dnd_or_sleeping",
                user_id=event.user_id,
                device_id=presence.device_id,
            )
            return RouteResult(
                acted=False,
                silence_reason=f"dnd={presence.is_dnd} sleeping={presence.is_sleeping}",
            )

        # 找对应 adapter
        adapter = self._registry.get(presence.device_id)
        if adapter is None:
            _logger.warning(
                "answer_router.device_not_found",
                user_id=event.user_id,
                device_id=presence.device_id,
            )
            return RouteResult(
                acted=False,
                fallback_used=True,
                silence_reason=f"device_not_found:{presence.device_id}",
            )

        return await self._push_one(adapter, answer)

    async def _push_one(
        self, adapter: OutputAdapter, answer: str
    ) -> RouteResult:
        """推一个 adapter。"""
        try:
            reply = ButlerReply(
                content=answer,
                user_id="default",
                source="proactive",
            )
            await adapter.push(reply)
            return RouteResult(
                acted=True,
                pushed_device_ids=[self._registry._device_id(adapter)],
            )
        except Exception as exc:  # noqa: BLE001
            _logger.error(
                "answer_router.push_error",
                device_id=self._registry._device_id(adapter),
                error=str(exc),
            )
            return RouteResult(
                acted=False,
                pushed_device_ids=[],
                error_device_ids=[self._registry._device_id(adapter)],
                silence_reason=f"push_error:{exc}",
            )

    async def _push_all(self, answer: str, event: BaseEvent) -> RouteResult:
        """URGENT:推所有 adapter,并联 gather。"""
        adapters = self._registry.all()
        if not adapters:
            return RouteResult(acted=False, silence_reason="no_output_adapter")

        async def _safe(ad: OutputAdapter) -> tuple[str, bool, str | None]:
            try:
                reply = ButlerReply(
                    content=answer,
                    user_id=event.user_id,
                    source="proactive",
                )
                await ad.push(reply)
                return (self._registry._device_id(ad), True, None)
            except Exception as exc:  # noqa: BLE001
                return (self._registry._device_id(ad), False, str(exc))

        results = await asyncio.gather(
            *(_safe(ad) for ad in adapters),
            return_exceptions=True,
        )

        pushed: list[str] = []
        errors: list[str] = []
        reasons: list[str] = []

        for r in results:
            if isinstance(r, Exception):
                reasons.append(f"gather_exception:{r}")
                continue
            dev_id, ok, err = r
            if ok:
                pushed.append(dev_id)
            else:
                errors.append(dev_id)
                reasons.append(f"{dev_id}:{err}")

        acted = bool(pushed)
        reason = "; ".join(reasons) if reasons else None

        _logger.info(
            "answer_router.urgent",
            event_id=event.event_id,
            acted=acted,
            pushed=pushed,
            errors=errors,
        )

        return RouteResult(
            acted=acted,
            pushed_device_ids=pushed,
            error_device_ids=errors,
            silence_reason=reason,
        )


__all__ = ["ConsoleAnswerRouter"]
