"""ButlerServer — 总线层主类（Phase 7.1）。

## 职责

把 ButlerOrchestrator + EventBus + InputAdapter/OutputAdapter 装到一起,
对外只暴露 4 个高层入口:

| 方法 | 用途 |
|---|---|
| ``start()`` | 启动 EventBus consumer + 注入 adapter sink + 启动 scheduler |
| ``stop()`` | 优雅关闭 |
| ``submit_reactive(sub)`` | Reactive 入口(用户主动说话)|
| ``submit_proactive(sub)`` | Proactive 入口(事件触发主动循环)|

## 关键不变性

1. ``submit_reactive`` 必须是 async(因为里头调 orchestrator.ainvoke)
2. 多个 OutputAdapter 并联广播(任一抛异常不影响其他)
3. EventBus 抛异常 → 降级 silent(不能挂 server)
4. Proactive 触发 ProactiveResult.silent → 不下发到 OutputAdapter(沉默优先)

## Phase 7.1 范围(简化)

- EventBus 用 ``InMemoryEventBus``
- Proactive 通道先只挂"占位"consumer(接到事件就 publish 给 EventBus,
  便于后续 EventTrigger 接管)
- Reactive 通道直接走 orchestrator.ainvoke
- 没有真实 EventTrigger 也不影响 dev_chat 用
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import structlog

from smartbutler.events.core.event import BaseEvent, EventPriority, EventSource
from smartbutler.events.core.event_bus import EventBus
from smartbutler.server.adapters import (
    ButlerReply,
    InputAdapter,
    OutputAdapter,
    ProactiveSubmission,
    ReactiveSubmission,
)
from smartbutler.server.bus import InMemoryEventBus
from smartbutler.thinking.loop.orchestrator import ButlerOrchestrator

_logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


class ServerConfig:
    """Server 运行配置(简化版,Phase 7.1 只保留必要开关)。"""

    __slots__ = (
        "event_bus",
        "enable_event_bus",
        "auto_subscribe_proactive",
    )

    def __init__(
        self,
        *,
        event_bus: EventBus | None = None,
        enable_event_bus: bool = True,
        auto_subscribe_proactive: bool = True,
    ) -> None:
        """
        Args:
            event_bus: 注入的事件总线(默认 InMemoryEventBus)。
            enable_event_bus: 是否启用 EventBus(关掉时 Server 只走直接入口)。
            auto_subscribe_proactive: start() 时是否自动注册 Proactive 消费者
                (把 EventBus 上的事件转给 ProactiveLoop)。
        """
        self.event_bus = event_bus
        self.enable_event_bus = enable_event_bus
        self.auto_subscribe_proactive = auto_subscribe_proactive


# ---------------------------------------------------------------------------
# ButlerServer
# ---------------------------------------------------------------------------


class ButlerServer:
    """总线层主类。

    用法::

        orch = ButlerOrchestrator(llm=...)
        server = ButlerServer(orchestrator=orch)
        await server.start()
        # 1) 直接入口
        reply = await server.submit_reactive(
            ReactiveSubmission(user_input="你好", user_id="u1")
        )
        # 2) 事件入口
        await server.submit_proactive(
            ProactiveSubmission(topic="device.door.opened", user_id="u1")
        )
        await server.stop()
    """

    def __init__(
        self,
        *,
        orchestrator: ButlerOrchestrator,
        config: ServerConfig | None = None,
        input_adapter: InputAdapter | None = None,
        output_adapters: list[OutputAdapter] | None = None,
    ) -> None:
        """
        Args:
            orchestrator: 已构造的 ButlerOrchestrator(可带 memory / short_term)。
            config: Server 配置(None → 默认开 EventBus + 自动订阅 Proactive)。
            input_adapter: 可选,挂一个输入适配器(repl / HA / 智能音箱)。
            output_adapters: 可选,挂多个输出适配器(广播语义)。
        """
        self._orchestrator = orchestrator
        self._config = config or ServerConfig()
        self._event_bus: EventBus = self._config.event_bus or InMemoryEventBus()
        self._input_adapter: InputAdapter | None = input_adapter
        self._output_adapters: list[OutputAdapter] = list(output_adapters or [])

        # 内部状态
        self._started = False
        self._stopped = True
        # ProactiveLoop 缓存:按 user_id 复用
        self._proactive_loops: dict[str, Any] = {}

    # ----------------- 高层入口 -----------------

    async def start(self) -> None:
        """启动 server(EventBus + 注入 adapter sink + 自动订阅 Proactive)。"""
        if self._started:
            return  # 幂等
        self._started = True
        self._stopped = False

        # 1) EventBus
        if self._config.enable_event_bus:
            await self._event_bus.start()
            if self._config.auto_subscribe_proactive:
                await self._event_bus.subscribe(
                    "*",
                    self._on_bus_event,
                    consumer_group="butler-server-proactive",
                )
            _logger.info(
                "butler_server.started",
                event_bus=type(self._event_bus).__name__,
                output_count=len(self._output_adapters),
                has_input=self._input_adapter is not None,
            )
        else:
            _logger.info(
                "butler_server.started event_bus_disabled",
                output_count=len(self._output_adapters),
                has_input=self._input_adapter is not None,
            )

        # 2) Input adapter
        if self._input_adapter is not None:
            self._input_adapter.set_sink(
                submit_reactive=self.submit_reactive,
                submit_proactive=self._on_proactive_from_adapter,
            )
            await self._input_adapter.start()

    async def stop(self) -> None:
        """优雅关闭。"""
        if not self._started:
            return  # 幂等
        if self._stopped:
            return
        self._stopped = True
        if self._input_adapter is not None:
            await self._input_adapter.stop()
        if self._config.enable_event_bus:
            await self._event_bus.stop()
        self._started = False
        _logger.info("butler_server.stopped")

    async def submit_reactive(self, sub: ReactiveSubmission) -> ButlerReply:
        """Reactive 入口 — 用户主动说话 / 智能音箱识别 / REPL 输入。

        走 ``ButlerOrchestrator.ainvoke``,失败降级到错误答复(不挂 server)。
        """
        try:
            from datetime import datetime

            spoken_at = sub.spoken_at or datetime.now(UTC)
            text = await self._orchestrator.ainvoke(
                sub.user_input,
                user_id=sub.user_id,
                session_id=sub.session_id,
                parent_agent=sub.parent_agent,
                spoken_at=spoken_at,
            )
            reply = ButlerReply(
                content=text,
                user_id=sub.user_id,
                session_id=sub.session_id,
                source="reactive",
                metadata=dict(sub.metadata),
            )
        except Exception as exc:  # noqa: BLE001
            _logger.error(
                "butler_server.submit_reactive.error",
                error=str(exc),
                exc_type=type(exc).__name__,
                user_id=sub.user_id,
            )
            reply = ButlerReply(
                content=f"[error] {exc}",
                user_id=sub.user_id,
                session_id=sub.session_id,
                source="reactive",
            )
        await self._dispatch_reply(reply)
        return reply

    async def submit_proactive(self, sub: ProactiveSubmission) -> None:
        """Proactive 入口 — 外部设备 / 定时器 / 智能音箱唤醒。

        内部行为:
        1. 把 ProactiveSubmission 包成 BaseEvent,publish 到 EventBus
        2. 已订阅的 consumer(默认就是 server 自己)会调
           :meth:`ButlerOrchestrator.proactive_tick`
        3. 产出的 ProactiveResult 走 OutputAdapter(silent 就不下发)
        """
        try:
            event = self._to_base_event(sub)
        except Exception as exc:  # noqa: BLE001
            _logger.error(
                "butler_server.submit_proactive.to_event_error",
                error=str(exc),
                exc_type=type(exc).__name__,
            )
            return
        if not self._config.enable_event_bus:
            # 关闭 bus 时,直走 ProactiveLoop
            await self._on_bus_event(event)
            return
        try:
            await self._event_bus.publish(event, topic=event.topic)
        except Exception as exc:  # noqa: BLE001
            _logger.error(
                "butler_server.submit_proactive.publish_error",
                error=str(exc),
                exc_type=type(exc).__name__,
            )

    # ----------------- 适配器挂载 -----------------

    def add_output(self, adapter: OutputAdapter) -> None:
        """运行时挂一个输出适配器(广播语义)。"""
        self._output_adapters.append(adapter)

    def set_input(self, adapter: InputAdapter) -> None:
        """运行时换输入适配器(下次 start 生效;若已 start,需要 stop+start 才会拉新 sink)。"""
        self._input_adapter = adapter

    # ----------------- 内部 -----------------

    def _to_base_event(self, sub: ProactiveSubmission) -> BaseEvent:
        """ProactiveSubmission → BaseEvent。"""
        try:
            source = EventSource(sub.source)
        except ValueError:
            source = EventSource.DEVICE
        try:
            priority = EventPriority(sub.priority)
        except ValueError:
            priority = EventPriority.NORMAL
        return BaseEvent(
            event_id=f"evt_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S_%f')}_{sub.user_id}",
            source=source,
            topic=sub.topic,
            user_id=sub.user_id,
            timestamp=datetime.now(UTC),
            priority=priority,
            device_id=sub.device_id,
            payload=dict(sub.payload),
        )

    async def _on_proactive_from_adapter(
        self, sub: ProactiveSubmission
    ) -> None:
        """InputAdapter → Proactive 桥接(纯走 submit_proactive)。"""
        await self.submit_proactive(sub)

    async def _on_bus_event(self, event: BaseEvent) -> None:
        """EventBus consumer 默认 handler:转给 ProactiveLoop。"""
        try:
            result = await self._orchestrator.proactive_tick(
                event, user_id=event.user_id
            )
        except Exception as exc:  # noqa: BLE001
            _logger.error(
                "butler_server.bus_event.tick_error",
                event_id=event.event_id,
                topic=event.topic,
                error=str(exc),
                exc_type=type(exc).__name__,
            )
            return
        # silent → 不下发
        if not getattr(result, "acted", False):
            return
        message = getattr(result, "message", None) or ""
        if not message.strip():
            return
        reply = ButlerReply(
            content=message,
            user_id=event.user_id,
            session_id=f"proactive-{event.event_id}",
            source="proactive",
            related_event_id=event.event_id,
        )
        await self._dispatch_reply(reply)

    async def _dispatch_reply(self, reply: ButlerReply) -> None:
        """广播给所有 OutputAdapter,任一失败不影响其他。"""
        if not self._output_adapters:
            return
        await asyncio.gather(
            *(self._safe_push(ad, reply) for ad in self._output_adapters),
            return_exceptions=True,
        )

    async def _safe_push(
        self, adapter: OutputAdapter, reply: ButlerReply
    ) -> None:
        try:
            await adapter.push(reply)
        except Exception as exc:  # noqa: BLE001
            _logger.error(
                "butler_server.output_adapter.push_error",
                adapter=type(adapter).__name__,
                error=str(exc),
                exc_type=type(exc).__name__,
            )


# ---------------------------------------------------------------------------
# 工厂方法
# ---------------------------------------------------------------------------


def build_default_server(
    *,
    orchestrator: ButlerOrchestrator,
    output_adapters: list[OutputAdapter] | None = None,
    input_adapter: InputAdapter | None = None,
) -> ButlerServer:
    """便捷工厂:默认 InMemoryEventBus + 给定 adapters。

    单测 / dev_chat 用。
    """
    cfg = ServerConfig(
        event_bus=InMemoryEventBus(),
        enable_event_bus=True,
        auto_subscribe_proactive=True,
    )
    return ButlerServer(
        orchestrator=orchestrator,
        config=cfg,
        input_adapter=input_adapter,
        output_adapters=output_adapters,
    )


__all__ = [
    "ButlerServer",
    "ServerConfig",
    "build_default_server",
]
