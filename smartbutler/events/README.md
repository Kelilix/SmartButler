# SmartButler 事件驱动模块（Phase 7）

> **状态**:Phase 7 子阶段推进中(7.0+7.1+7.2+7.3 已落库 / 7.4 下一站)。
> **详细方案**:`docs/PHASE_7_TECHNICAL_DESIGN.md`(本模块上下游,含子阶段拆分 + 工作量 + 端到端测试用例)。
> **关联 ADR**:`ADR-008-event-driven.md`(事件驱动决策)+ `../../thinking/ADR-009-proactive-reactive-dual-loop.md`(双循环路由)

## 这是什么

把管家从「被用户问才回答」升级为「被外部事件叫起来主动响应」。

### 典型场景

| 场景 | 触发源 | 管家响应 |
|------|--------|----------|
| 门锁被打开 | `device.door.opened` | 推音响："主人回来了，今天累不累？" |
| 热水器温度到 | `device.water_heater.reached_temp` | 推音响："水烧好了，可以洗澡了" |
| 用户说"打开电热水器" | `voice.wake_word + asr_text` | 调 HomeAgent 开热水器 |
| 早晨 7:00 | `timer.scheduled` | 推音响："早，7 点了" |
| 烟雾报警 | `device.smoke_detector.alarm` | **urgent** 推全屋："检测到异常！" |

## 与现有架构的关系

```
events/                    ←  本模块（Phase 7）
    │
    ├──► thinking/loop/    ButlerOrchestrator.ainvoke()   ← 复用,不修改
    │
    ├──► thinking/loop/    parent_agent="trigger:..."     ← 已有字段,直接用
    │
    ├──► thinking/loop/    iteration_count + tool 白名单  ← Phase 5/6 改造
    │
    └──► interface/        HTTP/WebSocket 用户入口         ← 共存,事件走事件口
```

**核心承诺**:本模块**一行不改 ButlerOrchestrator 主体**。`ainvoke` 继续返 `str`,
`ButlerResponse` 升级**不需要**——`ButlerReply` 已在 `server/adapters/__init__.py` 包装成 dataclass,
含 `source="reactive"/"proactive"` 区分 + `related_event_id` 关联,完全够用。

## 目录结构

```
events/
├── __init__.py             对外 API 暴露
├── README.md               本文件
├── ADR-008-event-driven.md 事件驱动决策记录
│
├── core/                   核心抽象
│   ├── event.py            事件基类
│   ├── event_bus.py        事件总线接口
│   ├── trigger.py          事件触发器接口
│   └── normalizer.py       事件归一化器接口
│
├── protocol/               设备/语音/定时器协议
│   ├── device_event.py
│   ├── voice_event.py
│   └── timer_event.py
│
├── adapters/               设备接入
│   └── base.py             DeviceAdapter 抽象基类
│
├── routing/                反馈路由
│   ├── answer_router.py
│   └── presence.py
│
└── tests/
    └── test_protocol_stub.py  协议字段纯数据结构测试
```

## 已落库 / 待补(按子阶段)

### ✅ 7.0 协议 + 抽象(全部落库)

- `core/event.py` — `BaseEvent` + `EventSource`(4 枚举)+ `EventPriority`(3 枚举)
- `core/event_bus.py` — `EventBus` 抽象 + `EventBusError`
- `core/normalizer.py` — `EventNormalizer` 抽象(0 行实现)
- `core/trigger.py` — `EventTrigger` 抽象(0 行实现)
- `protocol/device_event.py` — `DeviceEvent` + `DeviceType`(14 枚举)
- `protocol/voice_event.py` — `VoiceEvent` + `VoiceAction`(4 枚举)
- `protocol/timer_event.py` — `TimerEvent` + `TimerAction`
- `routing/answer_router.py` — `AnswerRouter` 抽象(0 行实现)
- `routing/presence.py` — `PresenceService` 抽象 + `PresenceInfo`(0 行实现)
- `adapters/base.py` — `DeviceAdapter` 抽象
- `tests/test_protocol_stub.py` — 协议字段纯数据结构测试

### ✅ 7.1+7.2+7.3 Server + 内存总线(全部落库,在 `server/` 而非本目录)

- `InMemoryEventBus`(`server/bus.py`)— 内存版 EventBus(通配符 + history + replay)
- `ButlerServer`(`server/runtime.py`)— submit_reactive / submit_proactive / start / stop
- `ServerConfig` — event_bus 注入 + enable_event_bus 开关
- `ReactiveSubmission` / `ProactiveSubmission` / `ButlerReply` — 数据类
- `InputAdapter` / `OutputAdapter` 抽象
- `InMemoryInputAdapter` / `InMemoryOutputAdapter` / `PrintOutputAdapter` — Stub 实现
- `build_default_server` — 默认 InMemoryEventBus 工厂

### 🆕 下一站 7.4(中间环节真实化,~1 周)

- `core/normalizer_dedup.py` — DedupNormalizer(基于 event_id + topic 去重,window 5s)
- `core/trigger_default.py` — DefaultEventTrigger(route + 限速 20/min + 兜底)
- `server/runtime.py:298-322` `_on_bus_event` 改造 — 走 Normalizer + Trigger(目前直接 proactive_tick,**旁路了抽象**)
- `tests/server/test_event_bus_e2e.py` — 5 case:dedup / 限速 / 异常兜底 / source 路由

### ⏳ 后续 7.5-7.8

详见 `docs/PHASE_7_TECHNICAL_DESIGN.md` §2.1 子阶段总表。
