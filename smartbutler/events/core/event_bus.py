"""EventBus 抽象接口（Phase 7）。

## 职责

- **publish** — DeviceAdapter 调,把事件推入总线
- **subscribe** — EventTrigger / EventNormalizer 调,订阅感兴趣 topic
- **ack** — 处理成功后确认,失败重试/进死信

## 选型

Phase 7 启动时实现,**优先 Redis Streams**（详见 ADR-008 §2）。
本接口与具体实现解耦,Phase 7 后期可平滑切到 NATS / Kafka / Postgres LISTEN。

## 不变量

- publish 必须在 ack 之后才返回（at-least-once 语义）
- subscribe 必须在 handler 抛出异常时打 ERROR 但不停止消费
- 同一事件可能多次送达（consumer 必须幂等,通常靠 event_id dedup）
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from smartbutler.events.core.event import BaseEvent

# handler 签名: 接 BaseEvent, 异步, 无返回值
EventHandler = Callable[[BaseEvent], Awaitable[None]]


class EventBus(ABC):
    """事件总线抽象接口。"""

    @abstractmethod
    async def publish(self, event: BaseEvent, *, topic: str | None = None) -> None:
        """发布事件。

        Args:
            event: 事件实例
            topic: 可选,覆盖 event.topic。允许 adapter 用同一事件发到不同 topic。

        Raises:
            EventBusError: 总线不可用时
        """
        ...

    @abstractmethod
    async def subscribe(
        self,
        topic: str,
        handler: EventHandler,
        *,
        consumer_group: str = "default",
    ) -> None:
        """订阅 topic。

        Args:
            topic: 事件 topic,支持通配符(实现层决定语法)
            handler: 异步处理函数
            consumer_group: 消费者组(同一组内负载均衡,跨组广播)

        注意:
            - 本方法只注册订阅,不启动消费
            - 实际消费由 start() 启动,stop() 停止
        """
        ...

    @abstractmethod
    async def start(self) -> None:
        """启动后台消费循环。

        调用后,所有 subscribe 注册的 handler 才会被真正调用。
        """
        ...

    @abstractmethod
    async def stop(self) -> None:
        """停止消费循环,优雅关闭(等当前 handler 跑完)。"""
        ...

    @abstractmethod
    async def ack(self, event: BaseEvent) -> None:
        """确认事件已处理(从 pending 队列移除)。"""
        ...

    @abstractmethod
    async def replay(
        self,
        topic: str,
        *,
        since: Any | None = None,
        limit: int = 100,
    ) -> AsyncIterator[BaseEvent]:
        """回放历史事件(调试/重放用)。

        Args:
            topic: 过滤 topic
            since: 起始时间(实现层决定类型,timestamp 或 sequence id)
            limit: 最多返回多少条
        """
        ...

    # -------------------------------------------------------------------------
    # 🆕 2026-11-10:代次追踪(Generation)—— L1/L2 打断
    # -------------------------------------------------------------------------
    #
    # 为什么放在 EventBus 上:
    #   代次号是**事件流**的概念 —— 它的作用就是回答
    #   "队列里这条事件 / 这条待播的回复,还新鲜吗?"
    #   而 EventBus 是发布端(VoiceAdapter)和消费端(ButlerServer)
    #   **都能访问**的公共对象 —— 挂在它上面两边都能拿到。
    #
    # 挂在 VoiceAdapter 上不行:ButlerServer 拿不到 VoiceAdapter。
    # 用全局变量不行:多设备/多实例会串。

    @property
    def generation_tracker(self) -> Any:
        """用户输入代次追踪器(可选)。

        实现方可以返回 ``None``(不支持代次追踪)——
        此时所有过期检查自动跳过,行为退化为改造前的老逻辑。

        返回类型用 ``Any`` 是为了避免 core 层反向依赖 voice 层
        (core 是底层,voice 是上层)。具体类型见
        :class:`smartbutler.voice.generation.GenerationTracker`。
        """
        return None


class EventBusError(RuntimeError):
    """事件总线操作失败(连接断开 / 权限不足 / 序列化失败)。"""


__all__ = [
    "EventBus",
    "EventHandler",
    "EventBusError",
]
