"""SmartButler 事件驱动模块（Phase 7）。

## 模块定位

本模块承接 SmartButler 的"被动触发"能力 —— 管家从「被用户叫起来」扩展为
「被外部事件叫起来」。包括但不限于：

- 智能家居设备状态变化（门锁、灯、空调、热水器...）
- 语音唤醒（智能音响、麦克风阵列）
- 定时器 / 闹钟
- 外部 webhook（IoT 平台、智能家居云）

## 与现有模块的关系

```
┌──────────────────────────────────────────────────────┐
│  events/                                              │
│                                                       │
│   DeviceAdapter → publish → EventBus                 │
│                                         │             │
│                                         ▼             │
│                                  EventNormalizer      │
│                                         │             │
│                                         ▼             │
│                                   EventTrigger        │
│                                         │             │
│                                         ▼             │
│                              ButlerOrchestrator       │
│                              (复用 thinking/loop/)    │
│                                         │             │
│                                         ▼             │
│                                  AnswerRouter         │
│                                         │             │
│                                         ▼             │
│                              目标设备 (TTS/Push)      │
└──────────────────────────────────────────────────────┘
```

## 关键设计原则（详见 ADR-008）

1. **复用 ainvoke** — 不修改 ButlerOrchestrator 主体,只扩展
2. **协议优先** — 设备接入必须走标准 Event 协议,不允许私接
3. **归一化必做** — 事件进入管家前先经过 EventNormalizer (dedup/debounce)
4. **LLM 协议** — `parent_agent="trigger:<source>"` 让 LLM 知道是被事件叫起的

## Phase 4-6 的状态

本目录 Phase 4-6 阶段**只放协议 + 抽象接口**,不写实现。
实现代码留到 Phase 7 真正启动时按接口填充,不阻塞当前进度。

## 文件清单

- ``core/event.py`` — 事件基类与协议字段
- ``core/event_bus.py`` — 事件总线抽象接口
- ``core/trigger.py`` — 事件触发器抽象接口
- ``core/normalizer.py`` — 事件归一化器抽象接口
- ``protocol/*`` — 设备/语音/定时器事件协议
- ``adapters/base.py`` — 设备接入抽象基类
- ``routing/answer_router.py`` — 答案路由抽象接口
- ``routing/presence.py`` — 用户在场服务抽象接口
- ``ADR-008-event-driven.md`` — 事件驱动决策记录
"""
from __future__ import annotations

__all__: list[str] = []
__phase__: str = "7"  # 实现的 Phase
__status__: str = "scaffold-only"  # 当前只放骨架,不放实现
