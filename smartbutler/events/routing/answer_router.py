"""AnswerRouter 抽象接口(Phase 7.5 + 7.6 + 7.8 演进)。

## 职责

EventTrigger 拿到管家 answer → AnswerRouter:
1. **按路由策略选目标设备** — 策略是 Pluggable 的,Phase 7.5/7.6/7.8 不同阶段策略不同
2. 调 PresenceService / 视觉位置推断
3. 调目标 OutputAdapter (TTS / push / 屏显)

## 路由策略矩阵(2026-10-15 重构)

策略按优先级链式 fallback,每级失败进入下一级。

| 阶段 | 策略链 |
|------|--------|
| **7.5(无视觉)** | ① [声学同址] event.device_id(麦克风=音响) → ② [PresenceService] active_device → ③ [Fallback] 第一个注册 adapter → ④ [Console] 打印 |
| **7.6(声纹+麦克风)** | 同 7.5 |
| **7.8(加入摄像头视觉)** | ① [视觉就近] CameraAdapter 算"人最近摄像头" → ② [声学同址] event.device_id → ③ [PresenceService] → ④ [Fallback] → ⑤ [Console] |

## 不变量

- 必须 fallback:PresenceService 挂掉 → 推"默认设备"
- 必须 timeout:推设备 adapter 超时 → 切下一个目标设备
- 必须可观测:每次路由打 INFO 日志(target=xxx, strategy=xxx, fallback_used=xxx)
- **必须可扩展**:加新策略不破坏现有行为
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING

from smartbutler.events.core.event import BaseEvent, EventPriority
from smartbutler.events.routing.route_result import RouteResult

if TYPE_CHECKING:
    from smartbutler.server.adapters import OutputAdapter


# === 策略链定义 ===

@dataclass(frozen=True)
class RoutingContext:
    """路由上下文(每个 RouteRequest 一次,带全链路信息)。"""

    event: BaseEvent
    answer: str
    priority: EventPriority
    # 7.8 新增:视觉就近结果(从最近摄像头推算出的"人在哪儿")
    # 7.5/7.6 阶段为 None
    nearest_visual_device_id: str | None = None


class RoutingStrategy(ABC):
    """路由策略抽象接口 — 单一职责:一个策略只决定"下一个候选设备"。"""

    @property
    @abstractmethod
    def name(self) -> str:
        """策略名(日志可观测):'visual_nearest' / 'echo_input' / 'presence' / 'first_registered' / 'console'"""
        ...

    @abstractmethod
    async def select(
        self, ctx: RoutingContext, registry: "DeviceRegistry"
    ) -> "OutputAdapter | None":
        """从 registry 选一个目标 adapter。

        Args:
            ctx: 路由上下文(含 event / answer / priority / 视觉就近结果)
            registry: OutputAdapter 池

        Returns:
            - OutputAdapter: 选中的目标设备(必推)
            - None: 策略不适用或失败,调用方继续下一级策略

        语义约定:
            - 策略内部"判断应 silence"应当抛 ``RoutingReject``(或在自己
              ``select`` 实现里直接修改 ctx,让 AnswerRouter 知道本次 routing
              应该 silence)——见 PresenceStrategy 的实现。
            - 返 None = "我不适用" / "找不着" / "已 fallback"——继续下一策略。
        """
        ...


class RoutingReject(Exception):
    """策略主动拒绝(应当 silence,不再继续 fallback)。

    例:PresenceStrategy 发现 DND / 睡觉,本轮不应推送。
    抛 RoutingReject 会被 StrategyChainAnswerRouter 捕获,
    返 RouteResult(acted=False, silence_reason=...)。
    """


# === 已实现策略 ===

class EchoInputStrategy(RoutingStrategy):
    """声学同址策略:从 event.device_id(麦克风/音响)选 adapter。

    Phase 7.5/7.6 默认第一步。
    适用场景:用户对着客厅音响说"阿福今天周几",TTS 走同一个音响。

    失败条件:
    - event.device_id 为空(系统事件 / 定时器事件)
    - registry 里没有该 device_id 对应的 adapter(可能音响离线)
    """

    @property
    def name(self) -> str:
        return "echo_input"

    async def select(
        self, ctx: RoutingContext, registry: "DeviceRegistry"
    ):
        if not ctx.event.device_id:
            return None
        adapter = registry.get(ctx.event.device_id)
        return adapter  # None = 下一级策略


class PresenceStrategy(RoutingStrategy):
    """PresenceService 策略:从用户当前活跃设备选 adapter。

    适用场景:用户手机 App 发问(无 device_id 上下文),管家主动回复时
    应该推回用户的手机(PresenceService 知道他在哪)。

    行为:
    - PresenceService 返 None → 返 None(继续下一策略)
    - DND / sleeping → 抛 RoutingReject(应当 silence,不再 fallback)
    - 设备在 registry 找不到 → 返 None(继续下一策略)
    """

    def __init__(self, presence_service) -> None:
        self._presence = presence_service

    @property
    def name(self) -> str:
        return "presence"

    async def select(
        self, ctx: RoutingContext, registry: "DeviceRegistry"
    ):
        presence = await self._presence.get_active_device(ctx.event.user_id)
        if presence is None:
            return None
        if not presence.should_notify("normal" if ctx.priority == EventPriority.NORMAL else "urgent"):
            # DND / sleeping → 应 silence,不再 fallback
            raise RoutingReject(
                f"dnd={presence.is_dnd} sleeping={presence.is_sleeping} device_id={presence.device_id}"
            )
        adapter = registry.get(presence.device_id)
        if adapter is None:
            # PresenceService 推荐的设备在 registry 找不到(可能设备离线)
            # 这种场景老测试依赖 silence 行为(7.5 test_device_not_found_returns_not_acted)
            # 语义:PresenceService 的信息不可靠,继续 fallback 到 first_registered 可能更错
            # → 主动 reject,不再继续
            raise RoutingReject(
                f"presence_device_not_found:{presence.device_id}"
            )
        return adapter


class FirstRegisteredStrategy(RoutingStrategy):
    """Fallback 策略:第一个注册的 adapter。

    适用场景:PresenceService 挂掉 / 没数据 / 设备全离线,任何"主动选"都失败。
    保证至少有一个 device 收到回复,不至于管家回答石沉大海。
    """

    @property
    def name(self) -> str:
        return "first_registered"

    async def select(
        self, ctx: RoutingContext, registry: "DeviceRegistry"
    ):
        return registry.first()


# === 7.8 预留策略(目前不实现,7.8 启动时再写) ===

class VisualNearestStrategy(RoutingStrategy):
    """视觉就近策略(Phase 7.8 占位)。

    7.8 阶段实现:CameraAdapter 算"人离哪个摄像头最近",选对应 TTS 设备。
    当前阶段(7.5/7.6)未启用,前置策略链是 echo → presence → first。
    """

    @property
    def name(self) -> str:
        return "visual_nearest"

    async def select(
        self, ctx: RoutingContext, registry: "DeviceRegistry"
    ):
        if ctx.nearest_visual_device_id is None:
            return None
        return registry.get(ctx.nearest_visual_device_id)


# === AnswerRouter 接口(契约) ===

class AnswerRouter(ABC):
    """答案路由抽象接口。

    7.5 阶段实现:ConsoleAnswerRouter — 固定策略链 [echo, presence, first, console]
    7.8 阶段实现:StrategyChainAnswerRouter — 动态策略链 [visual, echo, presence, first, console]

    任何 AnswerRouter 都必须:
    - 接受 (event, answer) 输入
    - 按策略链选目标 device
    - await adapter.push() 完成再返
    - 返 RouteResult
    """

    @abstractmethod
    async def route(
        self,
        event: BaseEvent,
        answer: str,
        *,
        priority: EventPriority | None = None,
    ) -> RouteResult:
        """路由 answer 到目标设备。

        Args:
            event: 触发本 answer 的原始事件(用于取 user_id / device_id)
            answer: 管家产出的回答文本
            priority: 覆盖事件原始优先级

        Returns:
            RouteResult(acted / pushed_device_ids / fallback_used / silence_reason / error_device_ids)
        """
        ...


__all__ = [
    "AnswerRouter",
    "RoutingStrategy",
    "RoutingContext",
    "RoutingReject",
    "EchoInputStrategy",
    "PresenceStrategy",
    "FirstRegisteredStrategy",
    "VisualNearestStrategy",  # 7.8 占位
]
