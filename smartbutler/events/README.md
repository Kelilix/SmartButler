# SmartButler 事件驱动模块（Phase 7）

> **状态**：骨架阶段（Phase 4-6）— 只有协议 + 抽象接口 + ADR，**不写实现**。
>
> **启动实现条件**：Phase 7 启动 + HomeAssistant 等设备接入环境就绪后。

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

**核心承诺**：本模块**一行不改 ButlerOrchestrator 主体**。**只改一处**：Phase 7 启动时把
`ainvoke` 的返回类型从 `str` 升级到 `ButlerResponse`（带 `target_device` 字段），
让 EventTrigger 能路由回设备。

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

## 给 Phase 4-6 维护者的话

- **不要 import 本模块** — Phase 4-6 阶段没有调用方
- **不要在本模块写实现** — 等 Phase 7 启动时按接口填充
- **不要改 ButlerOrchestrator 主体** — 等 Phase 7.2 改 return type

## 启动 Phase 7 的检查清单

- [ ] Phase 5（Skill loader）已完成
- [ ] Phase 6（情感/记忆）已完成
- [ ] HomeAssistant 测试环境就绪
- [ ] 事件总线选型敲定（推荐 Redis Streams / NATS / Postgres LISTEN）
- [ ] PresenceService 选型敲定（推荐先简化为"用户配置的固定设备"）
