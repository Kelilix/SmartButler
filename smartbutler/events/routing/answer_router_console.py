"""StrategyChainAnswerRouter — 策略链式 AnswerRouter(Phase 7.5 重构)。

## 核心思想

**路由策略可插拔**,每级策略独立负责"选下一个候选 device",失败 fallback 到下一级。

## 当前(7.5)默认策略链

```
1. EchoInputStrategy        — event.device_id(声学同址,麦克风=音响)
2. PresenceStrategy         — PresenceService 查活跃设备
3. FirstRegisteredStrategy  — 第一个注册的 adapter
4. 策略链全失败 → RouteResult(acted=False, silence_reason="all_strategies_failed")
```

## 演进路径(7.6 / 7.8)

- **7.6(VoiceAdapter + 声纹)**:策略链不变,event.device_id 现在能稳定填(speaker.living_room)
- **7.8(CameraAdapter)**:策略链升级为 [VisualNearestStrategy, EchoInputStrategy, ...],visual 优先
- 策略的添加/删除/顺序调整,**不影响 AnswerRouter 调用方** — 这是策略模式的核心收益

## SILENT / URGENT 优先级处理

SILENT/URGENT 走 fast-path,**不经过策略链**:
- SILENT:不推,只打 DEBUG 日志
- URGENT:asyncio.gather 推所有 adapter(烟雾报警类)

## 决策记录

2026-10-15:ConsoleAnswerRouter 重构为 StrategyChainAnswerRouter。
原因:原实现硬编码 echo/presence/first 顺序,7.8 加 visual 策略要改核心逻辑。
改为策略模式后,新策略只追加,不破坏现有行为。
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

import structlog

from smartbutler.events.core.event import BaseEvent, EventPriority
from smartbutler.events.routing.answer_router import (
    AnswerRouter,
    EchoInputStrategy,
    FirstRegisteredStrategy,
    PresenceStrategy,
    RoutingContext,
    RoutingReject,
    RoutingStrategy,
)
from smartbutler.events.routing.device_registry import DeviceRegistry
from smartbutler.events.routing.route_result import RouteResult
from smartbutler.server.adapters import ButlerReply, OutputAdapter

_logger = structlog.get_logger(__name__)


@dataclass
class _StrategyResult:
    """单个策略执行的中间结果。"""

    strategy_name: str
    adapter: OutputAdapter | None
    skipped_reason: str | None = None  # None=选了 / 字符串=为啥没选


class StrategyChainAnswerRouter(AnswerRouter):
    """策略链式 AnswerRouter。

    用法::

        from smartbutler.events.routing import (
            DeviceRegistry, SimplePresenceService,
            EchoInputStrategy, PresenceStrategy, FirstRegisteredStrategy,
        )
        from smartbutler.events.routing.answer_router_strategy import StrategyChainAnswerRouter

        presence = SimplePresenceService()
        registry = DeviceRegistry([speaker, phone])
        chain = [
            EchoInputStrategy(),
            PresenceStrategy(presence),
            FirstRegisteredStrategy(),
        ]
        router = StrategyChainAnswerRouter(registry=registry, strategies=chain)
        result = await router.route(event, "欢迎回家", priority=EventPriority.URGENT)

    7.8 扩展:在 chain 最前面插 VisualNearestStrategy 即可。
    """

    def __init__(
        self,
        registry: DeviceRegistry,
        strategies: list[RoutingStrategy],
    ) -> None:
        if not strategies:
            raise ValueError("策略链不能为空,至少需要一个 fallback 策略(FirstRegisteredStrategy)")
        self._registry = registry
        self._strategies = strategies

    async def route(
        self,
        event: BaseEvent,
        answer: str,
        *,
        priority: EventPriority | None = None,
    ) -> RouteResult:
        effective_priority = priority or event.priority

        # SILENT fast-path:不推
        if effective_priority == EventPriority.SILENT:
            _logger.debug(
                "answer_router.silent",
                event_id=event.event_id,
                topic=event.topic,
            )
            return RouteResult(acted=False, silence_reason="SILENT")

        # URGENT fast-path:推所有
        if effective_priority == EventPriority.URGENT:
            return await self._push_all(answer, event)

        # NORMAL:走策略链
        return await self._route_via_chain(event, answer, effective_priority)

    async def _route_via_chain(
        self,
        event: BaseEvent,
        answer: str,
        priority: EventPriority,
    ) -> RouteResult:
        """NORMAL 优先级:按策略链顺序选目标 adapter。"""
        ctx = RoutingContext(
            event=event,
            answer=answer,
            priority=priority,
            # 7.8 启动时由 CameraAdapter 注入
            nearest_visual_device_id=None,
        )

        # 记录每个策略的执行结果(日志可观测)
        strategy_log: list[_StrategyResult] = []

        for idx, strategy in enumerate(self._strategies):
            try:
                adapter = await strategy.select(ctx, self._registry)
            except RoutingReject as reject:
                # 策略主动拒绝(例:DND / 睡觉 / 设备找不到)— 不再 fallback,silence 退出
                # 7.5 老测试要求:走了 PresenceStrategy 但失败 → fallback_used=True
                is_fallback = idx > 0
                _logger.info(
                    "answer_router.rejected_by_strategy",
                    event_id=event.event_id,
                    strategy=strategy.name,
                    reason=str(reject),
                    fallback_used=is_fallback,
                )
                return RouteResult(
                    acted=False,
                    fallback_used=is_fallback,
                    silence_reason=f"rejected_by:{strategy.name}:{reject}",
                )
            except Exception as exc:  # noqa: BLE001
                # 策略自身异常 → 记日志 + 继续下一级
                _logger.warning(
                    "answer_router.strategy_error",
                    strategy=strategy.name,
                    error=str(exc),
                )
                strategy_log.append(
                    _StrategyResult(strategy.name, None, skipped_reason=f"error:{exc}")
                )
                continue

            if adapter is None:
                # 策略不适用或失败 → 下一级
                strategy_log.append(
                    _StrategyResult(strategy.name, None, skipped_reason="not_applicable")
                )
                continue

            # 策略选中了 adapter → 推
            result = await self._push_one(adapter, answer, event)
            # 当命中非首位策略(走 fallback 路径)时,标记 fallback_used=True
            # 保留 7.5 老实现的语义(老测试 test_normal_fallback_* 依赖此)
            is_fallback = idx > 0
            result = RouteResult(
                acted=result.acted,
                pushed_device_ids=result.pushed_device_ids,
                fallback_used=is_fallback or result.fallback_used,
                silence_reason=result.silence_reason,
                error_device_ids=result.error_device_ids,
            )
            _logger.info(
                "answer_router.routed",
                event_id=event.event_id,
                strategy=strategy.name,
                target_device=result.pushed_device_ids[0] if result.pushed_device_ids else None,
                acted=result.acted,
                fallback_used=result.fallback_used,
            )
            return result

        # 策略链全失败 → 静默
        _logger.warning(
            "answer_router.all_strategies_failed",
            event_id=event.event_id,
            chain=[s.name for s in self._strategies],
            strategy_results=[
                {"strategy": r.strategy_name, "skipped": r.skipped_reason}
                for r in strategy_log
            ],
        )
        return RouteResult(
            acted=False,
            silence_reason="all_strategies_failed",
        )

    async def _push_one(
        self, adapter: OutputAdapter, answer: str, event: BaseEvent
    ) -> RouteResult:
        """推一个 adapter。"""
        try:
            reply = ButlerReply(
                content=answer,
                user_id=event.user_id,
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


# === 7.5 默认工厂 ===

def build_default_chain_answer_router(
    registry: DeviceRegistry,
    presence_service,
) -> "StrategyChainAnswerRouter":
    """7.5 默认策略链:

    1. EchoInputStrategy(event.device_id 同址)
    2. PresenceStrategy(active_device)
    3. FirstRegisteredStrategy(fallback)
    """
    return StrategyChainAnswerRouter(
        registry=registry,
        strategies=[
            EchoInputStrategy(),
            PresenceStrategy(presence_service),
            FirstRegisteredStrategy(),
        ],
    )


__all__ = [
    "StrategyChainAnswerRouter",
    "build_default_chain_answer_router",
]


# === 兼容老构造签名 ===
# 老调用方写 ``ConsoleAnswerRouter(registry=..., presence_service=...)``。
# 新版参数是 ``strategies=...``,为了 30+ 个老测试零修改升级,
# 用一个薄子类保留老签名,内部走默认 7.5 策略链工厂。
class ConsoleAnswerRouter(StrategyChainAnswerRouter):
    """ConsoleAnswerRouter — 7.5 默认策略链(向后兼容老构造签名)。

    内部用 ``[EchoInputStrategy, PresenceStrategy, FirstRegisteredStrategy]`` 策略链。
    7.6 启动后:event.device_id 由 VoiceAdapter 稳定填充,EchoInputStrategy 自然命中。
    7.8 扩展:在策略链前插入 VisualNearestStrategy,行为平滑升级。
    """

    def __init__(
        self,
        registry: DeviceRegistry,
        presence_service,
    ) -> None:
        super().__init__(
            registry=registry,
            strategies=build_default_chain_answer_router(registry, presence_service)._strategies,
        )


__all__.append("ConsoleAnswerRouter")
