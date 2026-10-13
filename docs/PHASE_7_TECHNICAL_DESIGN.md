# Phase 7: 事件总线 + 生产入口

> 状态: 设计阶段，代码未实现。
> 前置: Phase 6.6 Personality ✅ 已完成，Phase 6.3 Memory 3层分片 ✅ 已完成。
> 目标: 替换 `scripts/dev_chat.py`，成为真正的生产启动入口。
> **最新更新（2026-10-13 v0.7.1）**：
>   - §6 实施计划收紧 YAGNI 边界：Phase 7.1 只做 Server 高层 API，**不写任何 Adapter**
>   - 明确"未来设备接入 = 写独立脚本调高层 API，不属于 ButlerServer 核心代码"
>   - Phase 7.2 验收标准改为"演示脚本调 `event_bus.publish()`"，不再假设 HA 已接入

---

## 0. 关键约定：Reactive vs Proactive 的本质区别

> ⚠️ **本节是设计核心。** 后续 §1 的架构图、§6 实施计划都建立在此基础上。

### 0.1 一句话区别

| Loop | 入口 | 触发 | 默认行为 |
|---|---|---|---|
| **ReactiveLoop** | `ButlerServer.submit_reactive(input, session_id)` | 用户主动调 | **有问必答**（无 silent） |
| **ProactiveLoop** | `EventBus handler` → `ButlerServer._on_device_event(event)` | 设备事件经 EventBus 路由 | **有判必沉**（70%+ silent） |

**两条路径**：
- **路径 A（Reactive）**：用户 → InputAdapter → `submit_reactive()` → TaskDispatcher → ReactiveLoop → Orchestrator
- **路径 B（Proactive）**：设备 → InputAdapter → `EventBus.publish()` → EventBus consumer → `_on_device_event()` → ProactiveLoop → Orchestrator（仅在 should_respond=True 时）

### 0.2 三个层次的区分

| 层次 | Reactive | Proactive |
|---|---|---|
| **入口形态** | 用户/Adapter 显式调 `ButlerServer.submit_reactive()` | EventBus 推送触发 `_on_device_event(event)` |
| **循环结构** | `ReactiveLoop.run(input) → str`（无 silent 选项） | `ProactiveLoop.tick(event) → ProactiveResult(acted/silent)` |
| **调用栈** | InputAdapter → TaskDispatcher → ReactiveLoop → Orchestrator → Response | InputAdapter → EventBus → consumer → _on_device_event → ProactiveLoop → (Orchestrator) → AnswerRouter |

### 0.3 TaskScheduler 不是第三个 loop

`TaskScheduler` 和 `ConsolidationScheduler` 看起来像"第三类循环"，但它们**不参与用户对话回路**：

| 维度 | ReactiveLoop | ProactiveLoop | TaskScheduler |
|---|---|---|---|
| 触发者 | 用户/Adapter 显式 | EventBus handler | 后台周期 timer / `on_message_received` |
| 是否参与对话 | ✅ 主对话回路 | ✅ 主动回路 | ❌ 后台记忆维护 |
| 调 `orchestrator.ainvoke`? | ✅ 必须 | ✅ 只在 should_respond=True | ❌ 调 `segmenter.segment()`（不同 API） |
| 返回值 | `str`（管家响应） | `ProactiveResult`（acted/silent） | `dict`（扫描统计） |
| 输出 | 返回调用方 | 推到 AnswerRouter | 触发 AutoExtract 回调 |
| 是否要 LLM | ✅（管家 LLM） | ✅（管家 LLM） | ✅（segmenter LLM） |

**TaskScheduler 调的是 `TaskSegmenter.segment()`，不调 `ButlerOrchestrator.ainvoke()`**。它是"后台整理记忆"，和管家对话回路解耦。

---

## 1. 核心设计决策

### 1.1 总线并发模型：并发输入 → 串行任务

> ⚠️ **本图展示路径 A + 路径 B 的完整回路**。背景任务（TaskScheduler / ConsolidationScheduler）与对话回路解耦，不在此图。

```
                  ┌──────────────────────────────────────────┐
                  │          InputAdapters (并发)            │
                  │  - REPLAdapter                          │
                  │  - HomeAssistant WebSocket Adapter      │
                  │  - HTTP / WebSocket                     │
                  └────────┬───────────────────┬────────────┘
                           │                   │
                     路径 A │            路径 B │
                     直调  │            事件  │
                           │                   │
                           ▼                   ▼
                   ┌───────────────┐    ┌──────────────────┐
                   │TaskDispatcher │    │ InMemoryEventBus │
                   │ (串行化网关)   │    │ topic=device.*   │
                   │               │    │ topic=timer.*    │
                   └───────┬───────┘    └────────┬─────────┘
                           │                     │
                           │                     ▼
                           │          ┌──────────────────────┐
                           │          │ EventBus._consumer() │
                           │          │ (异步, 独立 task)    │
                           │          └────────┬─────────────┘
                           │                   │
                           ▼                   ▼
                  ┌─────────────────┐  ┌──────────────────────┐
                  │  ReactiveLoop   │  │ ButlerServer.        │
                  │  (有问必答)      │  │   _on_device_event() │
                  │                 │  │   (注册到 bus 的     │
                  │  调:             │  │    handler)          │
                  │  orch.ainvoke() │  └────────┬─────────────┘
                  │  返: str        │           │
                  └────────┬────────┘           ▼
                           │            ┌──────────────────────┐
                           │            │  ProactiveLoop       │
                           │            │  (有判必沉)           │
                           │            │                      │
                           │            │  调: reasoning +     │
                           │            │   orch.ainvoke()     │
                           │            │  返: ProactiveResult │
                           │            └────────┬─────────────┘
                           │                     │
                           │   ┌─────────────────┘
                           │   │
                           ▼   ▼
                    ┌────────────────────┐
                    │ ButlerOrchestrator │
                    │  (全局唯一)        │
                    │                    │
                    │  共用 graph._compiled │
                    └──────────┬─────────┘
                               │
                               ▼
                       ┌──────────────┐
                       │ AnswerRouter │
                       │ (输出路由)   │
                       └──────┬───────┘
                              │
              ┌───────────────┼───────────────┐
              ▼               ▼               ▼
       REPL/Terminal    TTS 音响        WebSocket App
       (Reactive 返)   (Proactive 推)    (双向)

【后台维护】(不参与对话回路，独立 task)
  - TaskScheduler (1h 周期) ─► TaskSegmenter ─► AutoExtract
  - ConsolidationScheduler (cron) ─► Consolidator
```

**设计理由**：

- Memory 层（`MemoryFacade`）基于 `aiosqlite` + Qdrant，**天然非线程安全**，串行化避免竞态
- 用户对话本质上是串行的（一个人说话，另一个人听），并发调用没有收益
- 串行化让 `ButlerOrchestrator` 全局只持有一份，`LangGraph` checkpointer 不需要分布式锁
- 并发输入 → 任务入队 → 依次执行 → 响应按顺序返回给各调用方（通过 `Future` / `asyncio.Event`）

### 1.2 两条路径的并发隔离

| 路径 | 入口 | 并发模型 | 是否串行化 |
|---|---|---|---|
| **路径 A (Reactive)** | `submit_reactive()` | 多个 InputAdapter 并发调入队 | ✅ **强串行**（TaskDispatcher） |
| **路径 B (Proactive)** | EventBus 推送 | EventBus 内部按 topic 独立 consumer | ⚠️ **同 topic 串行**（同 topic 顺序消费，跨 topic 独立） |

**为什么 Reactive 强串行而 Proactive 仅同 topic 串行**：

- Reactive 调 `ButlerOrchestrator.ainvoke()`，orchestrator 内部有可写状态（`_current_memory_block` 等），并发会污染
- Proactive 走 EventBus 自己的 consumer，consumer handler 内部只调 `ProactiveLoop.tick()`，tick() 内部 try/except + silent 降级，并发不污染 orchestrator 状态
- 但 Proactive 推送最终走 AnswerRouter → TTS，并发可能导致语音乱序，所以**同 topic 串行**是必要的

### 1.3 三类输入的并发隔离

| 输入源 | 并发模型 | 路径 | 说明 |
|---|---|---|---|
| REPL（本地终端） | 主线程 input() | A | input() 本身阻塞，但入队后立即返回 |
| HA WebSocket | 独立 asyncio Task | B | 持续监听，断线重连，事件入 EventBus |
| HTTP API | 每个请求一个 Task | A | Web 框架（FastAPI/uvicorn）并发接请求，任务入队 |

### 1.4 设备输出（推送）

管家产生的输出分两类：

| 类型 | 说明 | 路由 |
|---|---|---|
| **Reactive 响应** | 用户主动问的，响应给发起方 | 直接返回给 REPL/WebSocket caller |
| **Proactive 推送** | 设备事件触发，管家主动开口 | 推送到所有在线设备（AnswerRouter） |

---

## 2. 目录结构

```
smartbutler/
├── server/                        # 🆕 Phase 7 核心
│   ├── __init__.py
│   ├── bus.py                     # asyncio.EventBus 实现
│   ├── dispatcher.py              # 任务分发器（串行化网关）
│   ├── butler_server.py           # ButlerServer 核心：持有 orchestrator，驱动双 loop
│   ├── app.py                     # 生产入口：FastAPI / uvicorn 启动
│   ├── adapters/                  # 输入适配器
│   │   ├── __init__.py
│   │   ├── base.py                # InputAdapter 抽象基类
│   │   ├── repl.py                # REPL 适配器（替换 dev_chat）
│   │   ├── ha_websocket.py        # HomeAssistant WebSocket 适配器
│   │   └── http_ws.py            # HTTP + WebSocket 适配器（未来扩展）
│   └── output/                    # 输出路由
│       ├── __init__.py
│       ├── base.py                # OutputAdapter 抽象基类
│       ├── answer_router.py       # AnswerRouter：响应 → 路由到发起方
│       └── speaker.py             # TTS → 音响推送适配器
│
├── events/                        # Phase 7 骨架（现有）
│   ├── core/
│   │   ├── event_bus.py          # 🆕 实现（接口已定义）
│   │   ├── normalizer.py         # 🆕 实现
│   │   └── trigger.py            # 🆕 实现
│   ├── adapters/                 # 设备适配器
│   │   └── homeassistant.py      # 🆕 HA 设备适配器
│   └── routing/
│       ├── answer_router.py       # 🆕 实现（从 skeleton 填充）
│       └── presence.py            # 🆕 实现
│
└── thinking/loop/
    ├── orchestrator.py            # 现有，✅ 不改主体
    ├── proactive_loop.py          # 现有，✅ 不改主体
    └── reactive_loop.py          # 🆕 抽象：把 orchestrator.ainvoke() 包装成 loop
```

---

## 3. 核心组件详解

### 3.1 `ButlerServer`（核心调度器）

```python
# smartbutler/server/butler_server.py

class ButlerServer:
    """
    管家服务核心调度器。

    职责：
    1. 持有 ButlerOrchestrator（全局唯一，串行访问）
    2. 驱动 ReactiveLoop + ProactiveLoop
    3. 把任务分发器（Queue）的任务依次路由到对应 Loop
    4. 把 Loop 产出路由到 AnswerRouter

    不变量：
    - ButlerServer 只持有一份 ButlerOrchestrator
    - 所有 Loop 调用 orchestrator 都通过 ButlerServer 代理
    - Loop 不直接持有 orchestrator，避免多实例竞态
    """

    def __init__(
        self,
        orchestrator: ButlerOrchestrator,
        proactive_loop: ProactiveLoop,
        event_bus: EventBus,
        output_router: AnswerRouter,
        user_id: str = "default",
    ):
        self._orch = orchestrator
        self._proactive = proactive_loop
        self._bus = event_bus
        self._router = output_router
        self._user_id = user_id
        self._dispatcher = TaskDispatcher()

    async def start(self) -> None:
        """启动服务：启动 bus + 注册事件处理 + 启动 dispatcher"""
        await self._bus.start()
        # 订阅事件到 ProactiveLoop
        await self._bus.subscribe(
            topic="device.#",          # 所有设备事件
            handler=self._on_device_event,
        )
        await self._bus.subscribe(
            topic="timer.#",            # 定时器事件
            handler=self._on_timer_event,
        )
        # 启动分发器（开始处理队列中的任务）
        await self._dispatcher.start()

    async def stop(self) -> None:
        """优雅关闭：等分发器清空队列 + 停止 bus"""
        await self._dispatcher.stop()
        await self._bus.stop()

    # ---------- 外部入口（REPL / HTTP 调这里）----------

    async def submit_reactive(
        self,
        user_input: str,
        session_id: str,
        source: str = "repl",         # 用于 AnswerRouter 路由回去
        spoke_at: datetime | None = None,
    ) -> str:
        """
        提交 Reactive 任务。
        任务入队 → 串行等待 → 返回管家响应。
        """
        task = ReactiveTask(
            user_input=user_input,
            session_id=session_id,
            user_id=self._user_id,
            source=source,
            spoke_at=spoke_at or datetime.now(UTC),
        )
        return await self._dispatcher.enqueue(task)

    # ---------- 内部事件处理 ----------

    async def _on_device_event(self, event: BaseEvent) -> None:
        """设备事件 → 推 ProactiveLoop → 决定是否主动"""
        result = await self._proactive.tick(event)
        if result.acted:
            # 主动推送 → AnswerRouter
            await self._router.push_proactive(
                message=result.message,
                urgency=result.urgency,
                source_event_id=event.event_id,
            )
        await self._bus.ack(event)

    async def _on_timer_event(self, event: BaseEvent) -> None:
        """定时器事件 → 同设备事件"""
        await self._on_device_event(event)
```

### 3.2 `TaskDispatcher`（串行化网关）

```python
# smartbutler/server/dispatcher.py

from dataclasses import dataclass, field
import asyncio
from datetime import datetime
from typing import Any, Awaitable


@dataclass
class ReactiveTask:
    """Reactive 任务单元。"""
    user_input: str
    session_id: str
    user_id: str
    source: str                    # "repl" | "http" | "websocket"
    spoke_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    future: asyncio.Future[str] = field(default_factory=asyncio.Future)

    @property
    def task_id(self) -> str:
        return f"{self.source}:{self.session_id}:{self.spoke_at.isoformat()}"


class TaskDispatcher:
    """
    任务分发器：并发接收 → 串行执行 → 结果返回。

    设计原理：
    - 所有输入源并发调用 enqueue()，任务立即入 asyncio.Queue
    - 内部一个 worker 协程从队列取任务，串行执行
    - 任务完成时 resolve 对应的 Future，唤醒等待方

    不变量：
    - Queue 容量无上限（队列堆积代表输入超过处理能力）
    - 单个任务失败不影响其他任务（每个任务 try/except）
    - worker 崩溃后自动重启（with asyncio.timeout）
    """

    def __init__(self, on_task: Callable[[ReactiveTask], Awaitable[str]]):
        """
        Args:
            on_task: 实际执行业务逻辑的回调。
                     TaskDispatcher 不直接持有 ButlerServer，
                     由外部传入（解耦）。
        """
        self._on_task = on_task
        self._queue: asyncio.Queue[ReactiveTask] = asyncio.Queue()
        self._worker_task: asyncio.Task | None = None
        self._running = False

    async def start(self) -> None:
        self._running = True
        self._worker_task = asyncio.create_task(self._worker_loop())

    async def stop(self) -> None:
        self._running = False
        if self._worker_task:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass

    async def enqueue(self, task: ReactiveTask) -> str:
        """入队，返回管家响应（阻塞直到任务完成）。"""
        await self._queue.put(task)
        return await task.future  # 等待 worker resolve

    async def _worker_loop(self) -> None:
        """单协程串行处理队列中的任务。"""
        while self._running:
            try:
                # 取任务（可超时，以便检查 _running）
                task = await asyncio.wait_for(
                    self._queue.get(),
                    timeout=1.0,
                )
            except asyncio.TimeoutError:
                continue
            except Exception:
                continue

            try:
                result = await self._on_task(task)
                if not task.future.done():
                    task.future.set_result(result)
            except Exception as exc:
                _logger.error(
                    "dispatcher.task_failed",
                    task_id=task.task_id,
                    error=str(exc),
                )
                if not task.future.done():
                    task.future.set_exception(exc)
            finally:
                self._queue.task_done()
```

### 3.3 `InMemoryEventBus`（asyncio 实现）

```python
# smartbutler/server/bus.py

class InMemoryEventBus(EventBus):
    """
    asyncio 原生事件总线（Phase 7 初期实现）。
    Phase 7 后期可替换为 Redis Streams / NATS。

    与 EventBus 接口的关系：
    - publish → asyncio.Queue.put（广播给所有订阅者）
    - subscribe → 注册 handler 到内部 topic → handler map
    - start/stop → 管理 consumer tasks 的生命周期
    - ack → 内部 pending set 移除（at-least-once 语义）

    不变量实现：
    - publish 必须在 ack 之后才返回 ✅（同步返回，handler 是后台 task）
    - handler 抛异常时打 ERROR 但不停止消费 ✅（worker task 捕获）
    - 同一事件多次送达？通过 event_id dedup set 过滤 ✅
    """

    def __init__(self):
        self._handlers: dict[str, list[tuple[str, EventHandler]]] = {}  # topic → [(consumer_group, handler)]
        self._queues: dict[str, asyncio.Queue[BaseEvent]] = {}          # topic → queue
        self._consumer_tasks: dict[str, list[asyncio.Task]] = {}
        self._pending: set[str] = set()                                 # event_ids awaiting ack
        self._dedup: dict[str, datetime] = {}                          # event_id → first_seen
        self._running = False
        self._lock = asyncio.Lock()

    async def publish(self, event: BaseEvent, *, topic: str | None = None) -> None:
        effective_topic = topic or event.topic

        # Dedup（TTL 1 小时）
        now = datetime.now(UTC)
        if event.event_id in self._dedup:
            seen_at = self._dedup[event.event_id]
            if (now - seen_at).total_seconds() < 3600:
                _logger.debug("bus.dedup_dropped", event_id=event.event_id)
                return
        self._dedup[event.event_id] = now

        self._pending.add(event.event_id)

        # 广播到所有匹配 topic 的队列
        matching_queues = self._collect_matching_queues(effective_topic)
        for q in matching_queues:
            await q.put(event)

    async def subscribe(
        self,
        topic: str,
        handler: EventHandler,
        *,
        consumer_group: str = "default",
    ) -> None:
        async with self._lock:
            # 创建专属队列（同一 consumer_group 共享队列）
            if topic not in self._queues:
                self._queues[topic] = asyncio.Queue()
            queue = self._queues[topic]

            # 注册 handler
            self._handlers.setdefault(topic, []).append((consumer_group, handler))

    async def start(self) -> None:
        self._running = True
        # 为每个 topic 启动 consumer task
        async with self._lock:
            for topic, queue in self._queues.items():
                self._consumer_tasks[topic] = [
                    asyncio.create_task(self._consumer(topic, queue, handler))
                    for _, handler in self._handlers.get(topic, [])
                ]

    async def stop(self) -> None:
        self._running = False
        # 取消所有 consumer tasks
        for tasks in self._consumer_tasks.values():
            for t in tasks:
                t.cancel()
        await asyncio.gather(*[t for ts in self._consumer_tasks.values() for t in ts],
                             return_exceptions=True)
        self._consumer_tasks.clear()

    async def ack(self, event: BaseEvent) -> None:
        self._pending.discard(event.event_id)

    async def replay(self, topic: str, *, since: Any = None, limit: int = 100) -> AsyncIterator[BaseEvent]:
        # 实现 replay 接口（从 dedup log 读）
        ...

    async def _consumer(self, topic: str, queue: asyncio.Queue[BaseEvent], handler: EventHandler) -> None:
        while self._running:
            try:
                event = await queue.get()
            except asyncio.CancelledError:
                break
            except Exception:
                continue

            try:
                await handler(event)
                await self.ack(event)
            except Exception:
                _logger.error("bus.handler_error", topic=topic, event_id=event.event_id)
                # 不 stop，继续处理下一个事件
            finally:
                queue.task_done()

    def _collect_matching_queues(self, topic: str) -> list[asyncio.Queue[BaseEvent]]:
        """支持 topic 通配符（device.# → device.*）。"""
        queues = []
        for pattern, q in self._queues.items():
            if self._topic_matches(pattern, topic):
                queues.append(q)
        return queues

    @staticmethod
    def _topic_matches(pattern: str, topic: str) -> bool:
        """简单前缀匹配 + # 通配符。"""
        if pattern == "#":
            return True
        if pattern.endswith(".#"):
            prefix = pattern[:-2]
            return topic.startswith(prefix)
        return pattern == topic
```

### 3.4 `ReactiveLoop`（封装 orchestrator）

```python
# smartbutler/server/reactive_loop.py

class ReactiveLoop:
    """
    Reactive 循环：封装 ButlerOrchestrator.ainvoke()。

    职责：
    1. 接收 user_input，调用 orchestrator
    2. 捕获 orchestrator 返回的 ProactiveResult（如果有）
    3. 如果 enable_proactive_advice=True，把 proactive 建议追加到回复

    与 ButlerServer 的关系：
    - ButlerServer.submit_reactive() 调 ReactiveLoop.run()
    - ReactiveLoop.run() 调 orchestrator.ainvoke()
    """

    def __init__(
        self,
        orchestrator: ButlerOrchestrator,
        proactive_loop: ProactiveLoop,
        enable_proactive_advice: bool = True,
    ):
        self._orch = orchestrator
        self._proactive = proactive_loop
        self._enable_proactive_advice = enable_proactive_advice

    async def run(
        self,
        user_input: str,
        session_id: str,
        user_id: str,
        spoken_at: datetime,
    ) -> str:
        # 1. Reactive 响应
        reply = await self._orch.ainvoke(
            user_input,
            user_id=user_id,
            session_id=session_id,
            spoken_at=spoken_at,
        )

        # 2. Proactive 建议（Reactive 链尾追加）
        proactive_message: str | None = None
        if self._enable_proactive_advice:
            proactive_message = await self._proactive.advise(
                context=reply,
                current_answer=reply,
            )

        # 3. 拼接响应
        if proactive_message:
            reply = f"{reply}\n\n[小建议] {proactive_message}"

        return reply
```

### 3.5 `AnswerRouter`（输出路由）

```python
# smartbutler/server/output/answer_router.py

class AnswerRouter:
    """
    响应路由器：根据 source 把输出推到对应设备。

    设计：
    - Reactive 响应 → 推回发起方（REPL / WebSocket / HTTP caller）
    - Proactive 推送 → 推全屋（TTS → 音响 + WebSocket → app）

    不变量：
    - Reactive 和 Proactive 路由逻辑完全分离
    - Proactive 推送走多渠道（TTS + WS）
    - 每个在线连接维护一个 session_id → asyncio.Queue 映射
    """

    def __init__(
        self,
        tts_adapter: TTSAdapter | None = None,
    ):
        self._tts = tts_adapter
        # session_id → asyncio.Queue[str]（响应排队）
        self._reactive_queues: dict[str, asyncio.Queue[str]] = {}
        # 在线 WebSocket connections
        self._ws_connections: dict[str, asyncio.Queue] = {}
        self._lock = asyncio.Lock()

    # ---------- Reactive 路由 ----------

    async def register_reactive_session(self, session_id: str) -> asyncio.Queue[str]:
        """注册一个 Reactive session（REPL/WebSocket caller），返回响应队列。"""
        async with self._lock:
            if session_id not in self._reactive_queues:
                self._reactive_queues[session_id] = asyncio.Queue()
            return self._reactive_queues[session_id]

    async def push_reactive(self, session_id: str, message: str) -> None:
        """Reactive 响应推回给调用方。"""
        queue = self._reactive_queues.get(session_id)
        if queue is None:
            _logger.warning("router.unknown_session", session_id=session_id)
            return
        await queue.put(message)

    async def wait_reactive_response(self, session_id: str) -> str:
        """等待 Reactive 响应（对应 TaskDispatcher 的 future.resolve）。"""
        queue = self._reactive_queues.get(session_id)
        if queue is None:
            return "[无 session]"
        return await queue.get()

    # ---------- Proactive 推送 ----------

    async def push_proactive(
        self,
        message: str,
        urgency: ProactiveUrgency,
        source_event_id: str | None = None,
    ) -> None:
        """
        Proactive 推送：推全屋。

        路由策略（由 urgency 决定）：
        - LOW: 只推 TTS 音响（默认设备）
        - MED: 推 TTS + WebSocket 在线 app
        - HIGH: 推 TTS + 全量 WebSocket + 日志高亮
        """
        _logger.info(
            "router.proactive_push",
            urgency=urgency.value,
            message_len=len(message),
            source_event_id=source_event_id,
        )

        # TTS 推送（所有场景都有）
        if self._tts is not None:
            await self._tts.speak(message, urgency=urgency)

        # WebSocket 广播（MED / HIGH）
        if urgency in (ProactiveUrgency.MED, ProactiveUrgency.HIGH):
            await self._broadcast_ws(message)

    async def _broadcast_ws(self, message: str) -> None:
        for conn_id, queue in self._ws_connections.items():
            try:
                await queue.put(message)
            except Exception:
                pass

    # ---------- WebSocket 连接管理 ----------

    async def register_ws(self, conn_id: str) -> asyncio.Queue[str]:
        async with self._lock:
            self._ws_connections[conn_id] = asyncio.Queue()
            return self._ws_connections[conn_id]

    async def unregister_ws(self, conn_id: str) -> None:
        async with self._lock:
            self._ws_connections.pop(conn_id, None)
```

### 3.6 HomeAssistant 适配器

```python
# smartbutler/server/adapters/ha_websocket.py

class HomeAssistantAdapter(InputAdapter):
    """
    HomeAssistant WebSocket 适配器。

    HA 通过 WebSocket 暴露两类事件：
    1. state_changed：实体状态变化（device.* topic）
    2. automation_triggered：自动化触发（timer.* / automation.* topic）

    实现：
    - 连接 HA WebSocket API（需要 HA_TOKEN）
    - 接收事件 → 归一化 → EventBus.publish
    - 断线自动重连（指数退避）
    - 支持发送命令（调用 HA 服务）

    HA WebSocket API 参考：
    https://developers.home-assistant.io/docs/api/websocket/
    """

    def __init__(
        self,
        ha_url: str,
        ha_token: str,
        event_bus: EventBus,
        normalizer: EventNormalizer,
    ):
        self._ha_url = ha_url
        self._ha_token = ha_token
        self._bus = event_bus
        self._normalizer = normalizer
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._running = False
        self._reconnect_delay = 1.0
        self._max_reconnect_delay = 60.0

    async def start(self) -> None:
        self._running = True
        asyncio.create_task(self._connect_loop())

    async def stop(self) -> None:
        self._running = False
        if self._ws:
            await self._ws.close()

    async def _connect_loop(self) -> None:
        """连接循环：连接 → 监听 → 断开 → 退避重连。"""
        while self._running:
            try:
                await self._connect()
            except asyncio.CancelledError:
                break
            except Exception as e:
                _logger.error("ha.reconnect", error=str(e))

            # 指数退避
            await asyncio.sleep(self._reconnect_delay)
            self._reconnect_delay = min(
                self._reconnect_delay * 2,
                self._max_reconnect_delay,
            )

    async def _connect(self) -> None:
        import websockets
        from homeassistant_api import HomeAssistantAPI

        async with websockets.connect(self._ha_url) as ws:
            self._ws = ws
            self._reconnect_delay = 1.0  # 重置退避

            # 订阅 state_changed 事件
            await ws.send(json.dumps({
                "type": "subscribe_events",
                "event_type": "state_changed",
            }))

            async for raw in ws:
                if not self._running:
                    break
                msg = json.loads(raw)
                if msg.get("type") != "event":
                    continue

                event_data = msg.get("event", {}).get("data", {})
                entity_id = event_data.get("entity_id", "")

                # 归一化
                normalized = self._normalizer.normalize_ha_state_changed(
                    entity_id=entity_id,
                    new_state=event_data.get("new_state", {}),
                    old_state=event_data.get("old_state", {}),
                    user_id="primary_user",
                )
                if normalized is None:
                    continue

                await self._bus.publish(normalized)

    # ---------- 命令下发（给 HomeAgent Sub-Agent 调） ----------

    async def call_service(
        self,
        domain: str,
        service: str,
        entity_id: str | None = None,
        data: dict | None = None,
    ) -> bool:
        """调用 HA 服务（开关灯 / 调温度等）。"""
        if not self._ws:
            return False
        import websockets

        payload = {
            "type": "call_service",
            "domain": domain,
            "service": service,
            "data": {**(data or {}), **({"entity_id": entity_id} if entity_id else {})},
        }
        await self._ws.send(json.dumps(payload))
        return True
```

---

## 4. 生产入口 `smartbutler/server/app.py`

```python
# smartbutler/server/app.py
"""
生产入口：替换 scripts/dev_chat.py。

启动方式::

    python -m smartbutler.server.app

或 uvicorn::

    uvicorn smartbutler.server.app:app --host 0.0.0.0 --port 8000
"""

import asyncio
import signal
from contextlib import asynccontextmanager
from pathlib import Path

import structlog
import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

from smartbutler.config import load_llm_settings, load_storage_settings, load_memory_settings
from smartbutler.server import ButlerServer, InMemoryEventBus, TaskDispatcher
from smartbutler.server.adapters import REPLAdapter, HomeAssistantAdapter
from smartbutler.server.output import AnswerRouter, TTSAdapter
from smartbutler.server.reactive_loop import ReactiveLoop
from smartbutler.thinking.loop.orchestrator import ButlerOrchestrator
from smartbutler.thinking.loop.proactive_loop import ProactiveLoop
from smartbutler.emotion.memory.facade import MemoryFacade
from smartbutler.emotion.personality.loader import load_personality
from smartbutler.capabilities.llm import create_llm

_logger = structlog.get_logger(__name__)


# ---------- FastAPI App ----------

@asynccontextmanager
async def lifespan(app: FastAPI):
    # ---- 启动阶段 ----
    await server.start()
    _logger.info("server.started")

    yield

    # ---- 关闭阶段 ----
    await server.stop()
    _logger.info("server.stopped")


app = FastAPI(title="SmartButler API", lifespan=lifespan)

# 全局 server instance（lifespan 里初始化）
server: ButlerServer | None = None


# ---------- HTTP / WebSocket 端点 ----------

@app.post("/chat")
async def chat_post(request: ChatRequest) -> ChatResponse:
    """HTTP POST 聊天入口（curl / HTTP client 用）。"""
    if server is None:
        return ChatResponse(reply="[服务未启动]")

    reply = await server.submit_reactive(
        user_input=request.message,
        session_id=request.session_id or "http-default",
        source="http",
    )
    return ChatResponse(reply=reply)


@app.websocket("/ws/{session_id}")
async def websocket_endpoint(ws: WebSocket, session_id: str):
    """WebSocket 聊天入口（前端 app / 音响 app 用）。"""
    await ws.accept()

    if server is None:
        await ws.close(reason="server not started")
        return

    router = server._router  # 内部访问（简化写法）
    response_queue = await router.register_reactive_session(session_id)
    ws_conn_queue = await router.register_ws(f"ws-{session_id}")

    try:
        # 并行：接收消息 + 发送响应
        async def send_loop():
            while True:
                try:
                    msg = await response_queue.get()  # Reactive 响应
                    await ws.send_text(f"reactive:{msg}")
                except asyncio.CancelledError:
                    break

        async def recv_loop():
            while True:
                try:
                    text = await ws.receive_text()
                    if text.startswith("proactive_ack:"):
                        continue  # 忽略主动推送确认
                    reply = await server.submit_reactive(
                        user_input=text,
                        session_id=session_id,
                        source="websocket",
                    )
                    await ws.send_text(f"reactive:{reply}")
                except WebSocketDisconnect:
                    break

        await asyncio.gather(send_loop(), recv_loop(), return_exceptions=True)
    finally:
        router.unregister_ws(f"ws-{session_id}")


# ---------- REPL 入口 ----------

@app.get("/", response_class=HTMLResponse)
async def repl_root():
    """本地 REPL 界面（浏览器访问 http://localhost:8000）。"""
    return """
    <html><body>
    <h2>SmartButler REPL</h2>
    <input id='msg' placeholder='输入消息...' size=60 />
    <button onclick='send()'>发送</button>
    <pre id='output'></pre>
    <script>
    const ws = new WebSocket(`ws://${location.host}/ws/repl-1`);
    ws.onmessage = e => {
        if (e.data.startsWith('reactive:')) {
            document.getElementById('output').textContent += '管家> ' + e.data.slice(9) + '\\n';
        }
    };
    async function send() {
        const msg = document.getElementById('msg').value;
        document.getElementById('output').textContent += '用户> ' + msg + '\\n';
        document.getElementById('msg').value = '';
    }
    </script>
    </body></html>
    """


# ---------- CLI 入口 ----------

async def _repl_main(user_id: str = "default") -> None:
    """本地 REPL 模式（终端交互）。"""
    print("SmartButler REPL | 输入 /quit 退出")
    session_id = f"repl-{user_id}"
    router = server._router
    response_queue = await router.register_reactive_session(session_id)

    async def recv_and_print():
        while True:
            reply = await response_queue.get()
            print(f"管家> {reply}")

    recv_task = asyncio.create_task(recv_and_print())

    try:
        while True:
            try:
                line = input("用户> ").strip()
            except (EOFError, KeyboardInterrupt):
                break

            if line in ("/quit", "/exit", "/q"):
                break

            asyncio.create_task(server.submit_reactive(
                user_input=line,
                session_id=session_id,
                source="repl",
            ))
    finally:
        recv_task.cancel()

    await server.stop()


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="SmartButler Production Entry")
    parser.add_argument("--mode", choices=["repl", "web"], default="repl")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--ha-url", default=None)
    parser.add_argument("--ha-token", default=None)
    args = parser.parse_args()

    # 注册信号处理（优雅关闭）
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def _bootstrap():
        global server
        server = await _build_server(
            ha_url=args.ha_url,
            ha_token=args.ha_token,
        )
        await server.start()
        return server

    srv = loop.run_until_complete(_bootstrap())

    if args.mode == "web":
        uvicorn.run(app, host=args.host, port=args.port)
    else:
        loop.run_until_complete(_repl_main())

    loop.run_until_complete(srv.stop())
    loop.close()


# ---------- Server 构造（复用 dev_chat.py 的装配逻辑）----------

async def _build_server(
    ha_url: str | None = None,
    ha_token: str | None = None,
) -> ButlerServer:
    """完整装配 ButlerServer（复用 dev_chat.py 逻辑）。"""

    # 1. LLM
    llm_settings = load_llm_settings()
    llm = create_llm(llm_settings)

    # 2. MemoryFacade（复用 dev_chat.py 逻辑）
    storage = load_storage_settings()
    # ... 完整装配见 dev_chat.py _build_orchestrator
    memory_facade: MemoryFacade | None = _build_memory_facade(storage)

    # 3. Personality
    personality = load_personality()

    # 4. ButlerOrchestrator
    orchestrator = ButlerOrchestrator(
        llm=llm,
        llm_settings=llm_settings,
        memory_facade=memory_facade,
        personality=personality,
        enable_proactive_advice=True,
    )

    # 5. ProactiveLoop（复用 orchestrator 的 graph）
    proactive_loop = ProactiveLoop(
        graph=None,  # 懒获取: orchestrator._compiled
        user_id=memory_facade._user_id if memory_facade else "default",
    )

    # 6. ReactiveLoop
    reactive_loop = ReactiveLoop(
        orchestrator=orchestrator,
        proactive_loop=proactive_loop,
        enable_proactive_advice=True,
    )

    # 7. TaskDispatcher（串行化网关）
    dispatcher = TaskDispatcher(on_task=lambda task: reactive_loop.run(
        user_input=task.user_input,
        session_id=task.session_id,
        user_id=task.user_id,
        spoken_at=task.spoke_at,
    ))

    # 8. EventBus
    bus = InMemoryEventBus()

    # 9. OutputRouter + TTS
    tts_adapter = TTSAdapter() if _tts_available() else None
    router = AnswerRouter(tts_adapter=tts_adapter)

    # 10. ButlerServer
    server = ButlerServer(
        orchestrator=orchestrator,
        proactive_loop=proactive_loop,
        event_bus=bus,
        output_router=router,
        dispatcher=dispatcher,
    )

    # 11. HA 适配器（如果配置了）
    if ha_url and ha_token:
        ha_adapter = HomeAssistantAdapter(
            ha_url=ha_url,
            ha_token=ha_token,
            event_bus=bus,
            normalizer=EventNormalizer(),
        )
        await ha_adapter.start()

    return server
```

---

## 5. 与 dev_chat.py 的关系

| 维度 | `scripts/dev_chat.py` | `smartbutler/server/app.py` |
|---|---|---|
| **定位** | 开发 REPL（不入仓） | 生产入口 |
| **Orchestrator** | 每个 session 一个 | 全局唯一，串行 |
| **ProactiveLoop** | 不存在 | 集成 |
| **EventBus** | 无 | InMemoryEventBus |
| **HA 接入** | 无 | WebSocket 适配器 |
| **输出** | 打印到终端 | TTS + WebSocket + HTTP |
| **并发模型** | 单线程 REPL | 并发输入 → 串行任务 |

**迁移策略**：
1. `server/` 模块稳定后，在 `scripts/dev_chat.py` 顶部加一行 `DeprecationWarning`
2. 文档更新 README §2.7，指向 `server/app.py` 作为生产入口
3. `dev_chat.py` 保留作为轻量诊断工具（不启动 bus，只跑 orchestrator）

---

## 6. 实施计划

> **整体节奏**：先打通路径 A（Reactive 串行回路，能对话），再接入路径 B（Proactive 事件驱动，能主动开口）。后台任务（TaskScheduler / ConsolidationScheduler）保持现状不重写。

### Phase 7.0：文档先对齐（已完成 ✅）

- [x] 修正 §1.1 架构图：明确"路径 A 直调 vs 路径 B 经 EventBus"
- [x] 新加 §0：Reactive vs Proactive 的三个层次区分
- [x] 新加 §0.3：TaskScheduler 不是第三个 loop

### Phase 7.1：路径 A 骨架贯通（1-2 天）

> **目标**：能启动、能对话、能优雅退出。**只走路径 A，不接 EventBus，不写任何 Adapter**。

#### 7.1.1 范围（YAGNI 原则）

**做**：
- `TaskDispatcher`（串行化网关）
- `ReactiveLoop`（封装 orchestrator，含 Reactive→Proactive advice 钩子）
- `ButlerServer.submit_reactive()`（**高层 API 入口**，设备后续调它）
- `AnswerRouter`（Reactive 路由 + WebSocket session 注册）
- `smartbutler/server/app.py`（复用 dev_chat 装配逻辑）
- CLI `--mode repl` / `--mode web` 两个入口

**不做**（等设备到位再单独 PR）：
- ❌ `CameraInputAdapter` / `TTSOutputAdapter` / `HomeAssistantInputAdapter` 等任何 Adapter 基类/接口/Mock
- ❌ 音响 / 摄像头 / 麦克风 / HA 任何占位代码
- ❌ 任何"想象中的未来设备"接口

**理由**：高层 API（`submit_reactive()` / `event_bus.publish()`）是**稳定契约**，Adapter 是**易变实现**。现在写 Adapter 基类是 YAGNI——3 个月后协议可能完全不一样。

**未来接入示例**（不在 Phase 7.1 范围）：
- 摄像头 → 写独立脚本：ASR 转文字 → 调 `server.submit_reactive()` → 拿返回 → TTS 播出去
- HA 自动化 → 写独立脚本：监听 HA WS → 归一化 → 调 `event_bus.publish()` → EventBus 路由给 ProactiveLoop
- 这些脚本**不属于 ButlerServer 核心代码**，完全解耦

#### 7.1.2 任务清单

- [ ] 实现 `TaskDispatcher`（串行化网关）
- [ ] 实现 `ReactiveLoop`（封装 orchestrator，含 Reactive→Proactive advice 钩子）
- [ ] 实现 `ButlerServer.submit_reactive()`（高层 API 入口）
- [ ] 实现 `AnswerRouter`（Reactive 路由 + WebSocket session 注册）
- [ ] 写 `smartbutler/server/app.py` 入口（复用 dev_chat 装配逻辑）
- [ ] **路径检查点**：命令行 `--mode repl` 能对话；`--mode web` 浏览器访问能聊天

**验收标准**：
```bash
python -m smartbutler.server.app --mode repl
# 用户> 你好
# 管家> [响应]（走真实 LLM）
# Ctrl+C 优雅退出
```

**端到端单测**（必加）：
- test_reactive_serial.py：连发 3 条消息，验证响应顺序与发送顺序一致
- test_reactive_concurrent.py：3 个 asyncio task 并发调 submit_reactive，验证 Memory 写入不串
- test_high_level_api.py：直接调 `server.submit_reactive()`，验证不依赖任何 Adapter 也能工作

### Phase 7.2：路径 B 接入（2-3 天）

> **目标**：InMemoryEventBus 真实实现 + 演示脚本调 `event_bus.publish()` → ProactiveLoop → 主动开口。
> **重要**：HA 接入是**独立脚本**，不属于 ButlerServer 核心代码（见 §7.1.1 YAGNI 原则）。

- [ ] 实现 `InMemoryEventBus`（`smartbutler/server/bus.py`，含 topic 通配符 + dedup + ack）
- [ ] `ButlerServer._on_device_event()` handler，**从 EventBus 内部注册**（不暴露给外部 Adapter）
- [ ] 演示脚本：`scripts/demo_event_publish.py`（手动调 `event_bus.publish()` 模拟设备事件）
- [ ] **不做**：HA 适配器、任何设备 Adapter、任何占位代码

**验收标准**：
```bash
# 终端 1: 启动 server
python -m smartbutler.server.app --mode repl
# 管家> [等待输入...]

# 终端 2: 模拟设备事件
python -m scripts.demo_event_publish --event "主人回家了"

# 终端 1: 管家主动开口
# 管家> 主人回来了，今天累不累？
```

**端到端单测**（必加）：
- test_event_bus_topic_match.py：`device.door.opened` 匹配 `device.#` 通配符
- test_event_bus_dedup.py：同一 `event_id` publish 两次，handler 只调一次
- test_event_bus_handler_error_isolated.py：handler 抛异常不停止 consumer
- test_proactive_silent_default.py：默认 RuleBasedProactiveReasoning 70%+ silent
- test_high_level_publish_api.py：直接调 `event_bus.publish()`，验证不依赖任何 Adapter 也能工作

### Phase 7.3：ProactiveLoop 真实化（1-2 天）

> **目标**：ProactiveLoop 走 LLM 推理（不再是 rule-based），能调用 Memory + Personality。

- [ ] `ProactiveLoop` 接入 `MemoryFacade`（召回上下文，注入到 system prompt）
- [ ] `ProactiveLoop` 接入 `Personality`（渲染语气 + 行为准则）
- [ ] `ProactiveLoop.tick()` 改用 LLM 推理
- [ ] `RuleBasedProactiveReasoning` 降级后备保持不变（ADR-009 不变量）
- [ ] **路径检查点**：自定义 ProactiveLoop 能根据"主人情绪低落"历史产生"该吃早餐了"主动建议

### Phase 7.4：TTS 输出（1 天）

- [ ] 实现 `TTSAdapter`（Mock：打印到终端）
- [ ] 接入真实 TTS API（可选：Edge TTS / Azure TTS）
- [ ] **路径检查点**：Proactive 推送走 TTS，能听到音响播放

### Phase 7.5：Web 入口完善（1-2 天）

- [ ] FastAPI `/chat` 端点（HTTP POST）
- [ ] FastAPI `/ws/{session_id}` 端点（WebSocket）
- [ ] 浏览器 REPL 界面（HTML + JS）
- [ ] **路径检查点**：浏览器和 REPL 同时在线，管家响应正确路由

### Phase 7.6：收尾（1 天）

- [ ] `dev_chat.py` 加 DeprecationWarning
- [ ] 更新 README §2.7 和 §6 启动说明
- [ ] 全量单元测试通过（773 → ~850）
- [ ] 文档落盘：本设计文档

---

## 7. 关键设计权衡

### 7.1 Reactive 强串行 vs Proactive 同 topic 串行

> 详见 §0.1 和 §1.2，本节解释**为什么不同**。

| 维度 | Reactive (路径 A) | Proactive (路径 B) |
|---|---|---|
| 串行化机制 | `TaskDispatcher` 单 worker | EventBus consumer 同 topic 串行 |
| 串行化原因 | Orchestrator 内部有可写状态（`_current_memory_block` 等） | 避免 TTS/WebSocket 推送乱序 |
| 串行化粒度 | 全局单 worker | 按 topic 分组（同 topic 串行，跨 topic 独立） |
| 跨调用方影响 | 全部阻塞（用户 1 慢 → 用户 2 等） | 同 topic 阻塞（门锁事件不阻塞定时器） |

**为什么 Proactive 不强串行**：
- EventBus handler 内部只调 `ProactiveLoop.tick()`，tick() 本身 try/except + silent 降级
- 多个 device topic 互不相关（门锁 / 灯 / 空调），不应相互阻塞
- 但同 topic 串行是必要的：避免 TTS 语音乱序

### 7.2 为什么选 InMemoryEventBus 而不是 Redis Streams

| 维度 | InMemoryEventBus | Redis Streams |
|---|---|---|
| 依赖 | 零新增依赖 | 需要 Redis 进程 |
| 进程数 | 单进程 | 多进程（未来扩展） |
| 断线恢复 | 内存丢失 | 持久化 |
| 部署复杂度 | 低 | 中 |
| **适合场景** | Phase 7.1-7.3 | Phase 7.4+ 多实例部署 |

**决策**：Phase 7.1-7.3 用 InMemory，Phase 7.4+ 支持切换 Redis Streams（接口不变，替换实现类）。

### 7.2 为什么不用 FastAPI 本身做并发控制

FastAPI/Uvicorn 每个请求会启动一个 asyncio task，这些 task 并发调用 `ButlerOrchestrator`。

问题：`ButlerOrchestrator` 内部有状态（`_compiled` graph、`_current_memory_block` 等），并发调用虽然有 GIL 保护，但 LangGraph 的 checkpointer（SqliteSaver）在高并发下可能遇到 SQLite 锁。

**决策**：用 `TaskDispatcher` 在 `ButlerServer` 层串行化，而不是依赖 FastAPI 的并发模型。FastAPI 只负责接收请求 → 入队 → 返回 Future。

### 7.3 ProactiveLoop 为什么不需要串行化

ProactiveLoop 是"触发后不管"的异步任务，理论上可以并发执行。但因为 `AnswerRouter.push_proactive()` 涉及 TTS 和 WebSocket 广播，并发调用可能导致语音乱序。

**决策**：ProactiveLoop 的 `_on_device_event` handler 也走串行（通过 `EventBus` 内部的单 consumer 队列），避免语音乱序。

---

## 8. 配置项

```dotenv
# Phase 7: EventBus + Production Server
SMARTBUTLER_SERVER_MODE=repl|web                # 启动模式
SMARTBUTLER_SERVER_HOST=0.0.0.0
SMARTBUTLER_SERVER_PORT=8000

# HomeAssistant（可选，Phase 7.2+）
SMARTBUTLER_HA_URL=ws://192.168.1.x:8123/api/websocket
SMARTBUTLER_HA_TOKEN=xxxxx

# TTS（可选，Phase 7.4）
SMARTBUTLER_TTS_PROVIDER=edge|azure|none       # none = mock（打印到终端）
SMARTBUTLER_TTS_VOICE=zh-CN-XiaoxiaoNeural

# EventBus 切换（Phase 7.4+）
SMARTBUTLER_EVENTBUS_BACKEND=memory|redis|streams
SMARTBUTLER_EVENTBUS_REDIS_URL=redis://localhost:6379
```

---

## 9. 不变量（Phase 7 全程必须遵守）

> 本节是 §0.1 区分的**强约束版本**，每条都对应一个具体的反例。

1. **Orchestrator 全局唯一**： ButlerServer 只持有一份 orchestrator，所有 Loop 串行访问
   - ❌ 反例：开 2 个 `ButlerOrchestrator` 实例 → Memory 写入竞争
2. **TaskDispatcher 串行化**：所有 Reactive 任务入同一 Queue，单 worker 串行执行
   - ❌ 反例：多 worker 并发调 `orchestrator.ainvoke()` → `_current_memory_block` 被覆盖
3. **Proactive 不阻塞 Reactive**：ProactiveLoop 事件处理不占 TaskDispatcher 资源
   - ✅ 正确：Proactive 走 EventBus 自己的 consumer，独立 task
   - ❌ 反例：把 Proactive 也塞进 TaskDispatcher → 设备事件阻塞用户输入
4. **Proactive 同 topic 串行**：EventBus 同 topic 单 consumer，避免 TTS 乱序
   - ❌ 反例：同 topic 多 consumer 并发推送 → 语音乱序
5. **EventBus ack 必须在 handler 完成后**：at-least-once 语义，不丢事件
   - ❌ 反例：handler 内 await 之前先 ack → 异常时事件丢失
6. **Handler 异常不停止消费**：单个 handler 失败只打 ERROR log，继续处理下一个
   - ❌ 反例：handler 异常导致 consumer 协程退出 → 后续事件全丢
7. **Orchestrator 主体零修改**：不碰 `orchestrator.py` 主体，只扩展 `ReactiveLoop` 封装
8. **TaskScheduler 不算 loop**：TaskScheduler 调 `segmenter.segment()`，不调 `orchestrator.ainvoke()`，与对话回路解耦
   - ❌ 反例：把 TaskScheduler 接到 `submit_reactive()` 入口 → 后台任务混入对话回路
9. **降级链闭合**：HA 断线 → 自动重连；TTS 不可用 → 打印到终端 → 管家不崩
