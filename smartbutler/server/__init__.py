"""SmartButler Server 总线层（Phase 7.1）。

## 目标

把 ButlerOrchestrator + EventBus + InputAdapter/OutputAdapter 装到一起,
对外只暴露 4 个高层入口,业务方不必再管 LangGraph / asyncio.EventBus / Adapters 各自怎么连。

## 四个高层入口

| 方法                          | 用途                                     | 调用方                          |
|-------------------------------|------------------------------------------|--------------------------------|
| ``submit_reactive(user_input)`` | 用户主动说话(文本)                       | dev_chat REPL / 智能音箱 adapter |
| ``submit_proactive(event)``    | 事件触发的主动循环                       | EventBus consumer / HA bridge   |
| ``start()``                    | 启动后台任务(EventBus consumer、scheduler)| 应用启动入口                     |
| ``stop()``                     | 优雅关闭(flush in-flight)               | 应用关闭信号                    |

## 与现有模块的关系

```
┌──────────────────────────────────────────────────────────┐
│  server/                                                 │
│                                                           │
│   InputAdapter/OutputAdapter ─┐                          │
│   EventBus.publish ───────────┼──► ButlerServer          │
│   Direct user input ──────────┘      │                  │
│                                       ▼                  │
│                            ButlerOrchestrator             │
│                            (复用 thinking/loop/)          │
│                                       │                  │
│                                       ▼                  │
│                                  OutputAdapter(s)         │
└──────────────────────────────────────────────────────────┘
```

## Phase 7.1 范围

- ✅ 写 ``ButlerServer`` 包装类
- ✅ 写 ``InputAdapter`` / ``OutputAdapter`` 抽象接口(Stub 实现)
- ✅ 写内存版 ``EventBus``(同步实现,便于本地跑通)
- ✅ 改 ``dev_chat`` 走 ``ButlerServer.submit_reactive``
- ✅ 写单测
- ❌ **不**接 Redis / NATS / Kafka(留接口不接实现)
- ❌ **不**改 LLM 多模态(等真用到了)
- ❌ **不**改 Memory 多模态(等真用到了)
- ❌ **不**接真实 HA / 设备(只留接口)

## 不变量

- ``submit_reactive`` 必须能并发调用(多个 REPL / 多个 user 同时聊)
- ``start()`` / ``stop()`` 必须幂等
- 任何底层异常必须降级到「答: [error] xxx」,不能把 server 整挂
"""
from __future__ import annotations

from smartbutler.server.runtime import ButlerServer, ServerConfig, build_default_server
from smartbutler.server.adapters import (
    ButlerReply,
    InputAdapter,
    OutputAdapter,
    ProactiveSubmission,
    ReactiveSubmission,
)
from smartbutler.server.bus import InMemoryEventBus

__all__: list[str] = [
    # 核心
    "ButlerServer",
    "ServerConfig",
    "build_default_server",
    # 数据类
    "ReactiveSubmission",
    "ProactiveSubmission",
    "ButlerReply",
    # Adapter 抽象
    "InputAdapter",
    "OutputAdapter",
    # 内存版实现
    "InMemoryEventBus",
]

__phase__: str = "7.1"
__status__: str = "scaffold"
