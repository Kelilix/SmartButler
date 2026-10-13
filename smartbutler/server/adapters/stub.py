"""Stub / 内存版 Adapter 实现（Phase 7.1）。

## 用途

- **InMemoryInputAdapter** — REPL / 单测用,把 :class:`ReactiveSubmission`
  主动 ``enqueue`` 进去,server consumer 协程拉出来送进 orchestrator
- **InMemoryOutputAdapter** — 接收 :class:`ButlerReply`,存进内存列表,
  单测 / dev_chat 用来"管家说了什么"的可观测

Stub 实现**不**接 HA / 智能音箱 / 任何外部系统,纯进程内通信,
便于:

1. 本地起 server,REPL 灌消息进来,看输出
2. 单测里完全可控地造输入 + 验输出

## 不变量

- 输入 / 输出适配器都是 ``async`` 协议,但实现可以是"纯内存零等待"
- ``enqueue_reactive`` 是同步方法(便于单测 / dev 灌数据),
  ``start()`` 启的后台协程会从 queue 拉
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import structlog

from smartbutler.server.adapters import (
    ButlerReply,
    InputAdapter,
    OutputAdapter,
    ProactiveSubmission,
    ReactiveSubmission,
)

_logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Input
# ---------------------------------------------------------------------------


class InMemoryInputAdapter(InputAdapter):
    """内存版输入适配器 —— dev_chat / 单测用。

    用法::

        adapter = InMemoryInputAdapter()
        adapter.set_sink(submit_reactive=..., submit_proactive=...)
        await adapter.start()
        await adapter.enqueue_reactive(ReactiveSubmission(user_input="hi"))
        # 后台协程会把 "hi" 送进 submit_reactive
        ...
        await adapter.stop()

    也可直接 ``await adapter.send_now(sub)`` 同步等回包(不走后台协程),
    单测里更直观。
    """

    def __init__(self, *, maxsize: int = 1024) -> None:
        self._queue: asyncio.Queue[ReactiveSubmission] = asyncio.Queue(maxsize=maxsize)
        self._submit_reactive: (
            Callable[[ReactiveSubmission], Awaitable[ButlerReply]] | None
        ) = None
        self._submit_proactive: (
            Callable[[ProactiveSubmission], Awaitable[None]] | None
        ) = None
        self._consumer_task: asyncio.Task[None] | None = None
        self._stopped = False

    def set_sink(
        self,
        submit_reactive: Callable[[ReactiveSubmission], Awaitable[ButlerReply]],
        submit_proactive: Callable[[ProactiveSubmission], Awaitable[None]],
    ) -> None:
        self._submit_reactive = submit_reactive
        self._submit_proactive = submit_proactive

    async def start(self) -> None:
        if self._consumer_task is not None:
            return  # 幂等
        self._stopped = False
        self._consumer_task = asyncio.create_task(
            self._consume_loop(),
            name="InMemoryInputAdapter.consumer",
        )

    async def stop(self) -> None:
        self._stopped = True
        if self._consumer_task is not None:
            self._consumer_task.cancel()
            try:
                await self._consumer_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._consumer_task = None

    async def enqueue_reactive(self, sub: ReactiveSubmission) -> None:
        """把一个 reactive 提交塞进队列(后台协程会处理)。

        单测里如果想要"立刻拿答复",用 :meth:`send_now`。
        """
        if self._stopped:
            raise RuntimeError("InMemoryInputAdapter 已 stop,不能 enqueue")
        await self._queue.put(sub)

    async def send_now(self, sub: ReactiveSubmission) -> ButlerReply:
        """不走队列,直接走 sink(单测 / 阻塞场景用)。

        Returns:
            server 给出的答复。
        """
        if self._submit_reactive is None:
            raise RuntimeError("InMemoryInputAdapter 未 set_sink")
        return await self._submit_reactive(sub)

    def publish_proactive(self, sub: ProactiveSubmission) -> None:
        """把 proactive 提交"投递"给 sink(后台 task 拉)。

        与 reactive 不同:proactive 是 fire-and-forget(没答复),同步推就行。
        """
        if self._submit_proactive is None:
            raise RuntimeError("InMemoryInputAdapter 未 set_sink")
        if self._stopped:
            raise RuntimeError("InMemoryInputAdapter 已 stop")
        asyncio.create_task(self._submit_proactive(sub))

    async def _consume_loop(self) -> None:
        """后台协程:从 queue 拉 reactive 提交,送进 sink。"""
        assert self._submit_reactive is not None
        while not self._stopped:
            try:
                sub = await self._queue.get()
            except asyncio.CancelledError:
                break
            try:
                await self._submit_reactive(sub)
            except Exception as exc:  # noqa: BLE001
                _logger.error(
                    "in_memory_input_consumer.error",
                    error=str(exc),
                    exc_type=type(exc).__name__,
                )


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


class InMemoryOutputAdapter(OutputAdapter):
    """内存版输出适配器 —— 收到答复就存进列表,便于单测断言。

    用法::

        out = InMemoryOutputAdapter()
        # server 启动时 register
        # ... 跑 ...
        assert out.replies[0].content == "..."
    """

    def __init__(self) -> None:
        self.replies: list[ButlerReply] = []

    async def push(self, reply: ButlerReply) -> None:
        self.replies.append(reply)

    def clear(self) -> None:
        self.replies.clear()

    def last(self) -> ButlerReply | None:
        if not self.replies:
            return None
        return self.replies[-1]


class PrintOutputAdapter(OutputAdapter):
    """stdout 输出适配器 —— dev_chat 演示用。

    把答复打到 stderr(避免污染 REPL 主循环的 stdout),
    格式::

        [butler→stdout] user=xxx content=...
    """

    def __init__(self, *, stream: Any = None) -> None:
        import sys

        self._stream = stream if stream is not None else sys.stderr

    async def push(self, reply: ButlerReply) -> None:
        try:
            line = (
                f"[butler→stdout] user={reply.user_id} "
                f"session={reply.session_id} source={reply.source} "
                f"content={reply.content!r}\n"
            )
            self._stream.write(line)
            self._stream.flush()
        except Exception as exc:  # noqa: BLE001
            _logger.warning(
                "print_output_adapter.push_failed",
                error=str(exc),
            )


__all__ = [
    "InMemoryInputAdapter",
    "InMemoryOutputAdapter",
    "PrintOutputAdapter",
]
