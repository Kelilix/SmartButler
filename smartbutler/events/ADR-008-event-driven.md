# ADR-008：事件驱动架构（Phase 7）

> **状态**：草案（Phase 4-6 阶段冻结,Phase 7 启动时回看）
>
> **决策日期**：2026-10-08
>
> **决策人**：架构组

## 1. 背景

SmartButler 当前架构（Phase 4 已完成）只支持**主动调用模式**：
用户发请求 → HTTP/CLI 接口 → `ButlerOrchestrator.ainvoke()` → 返回答案。

但智能家居场景需要**被动触发模式**：
- 智能门锁被打开 → 管家主动打招呼
- 热水器温度到 → 管家主动通知
- 早晨 7 点 → 管家主动叫人起床
- 烟雾报警 → 管家紧急通知

## 2. 决策

### 决策 1：选事件驱动（Event-Driven），不选轮询（Polling）

| 维度 | 轮询 | **事件驱动** ✅ |
|------|------|-----------------|
| 设备 100 个时的 QPS | 100 QPS 持续 | 仅事件触发时 N 次 |
| 实时性 | 最差 1 个轮询周期 | ms 级 |
| 资源消耗 | 持续 CPU/网络 | 几乎为零 |
| 业界主流 | ❌ 罕见 | ✅ HA / 米家 / HomeKit / Alexa |
| 扩展性 | 设备越多越慢 | 设备越多越能并行 |

**结论**：选事件驱动。

### 决策 2：事件总线选 Redis Streams（暂定）

**候选**：

| 候选 | 优点 | 缺点 |
|------|------|------|
| **Redis Streams** ✅ | 已在 smartbutler/storage/ 有 RedisBackend 基础；轻量；ACK 机制 | 单机有限 |
| Postgres LISTEN/NOTIFY | 项目里已用 SQLite/Postgres，无额外依赖 | 不支持持久化历史 |
| NATS | 工业级；多语言 | 项目里 0 依赖,新增成本高 |
| Kafka | 工业级；持久化 | 重型，智能家居场景 overkill |
| MQTT | IoT 标配 | 需额外 broker |

**结论**：选 **Redis Streams**。理由：

1. smartbutler/storage/redis_backend.py 已存在基础
2. 支持 consumer group（多 trigger 节点负载均衡）
3. 支持消息回溯（重放历史事件）
4. Phase 7 真启动时如发现不够用,再切 NATS

### 决策 3：复用 ButlerOrchestrator.ainvoke，不另起一套

| 候选 | 优点 | 缺点 |
|------|------|------|
| **复用 ainvoke** ✅ | 一行不改主体;LLM 协议不变;Checkpointer 自然继承 | 返回类型要扩 |
| 另起 EventLoop | 完全独立 | 代码重复;LLM 资源浪费 |

**结论**：复用 ainvoke。**只改 1 处**：

```python
# 当前 (Phase 4)
async def ainvoke(...) -> str:  # 只返 string

# Phase 7.2 改造
async def ainvoke(...) -> ButlerResponse:  # 返结构 {answer, target_device, priority}
```

### 决策 4：parent_agent 协议 = `trigger:<source>`

事件触发的 ainvoke 调用必须用以下 parent_agent 值：

```
parent_agent = "trigger:device.door"
parent_agent = "trigger:device.water_heater"
parent_agent = "trigger:voice.wake_word"
parent_agent = "trigger:timer.scheduled"
```

LLM 通过这个字段知道"我是被事件叫起来的,不是人在说话"。

### 决策 5：事件归一化必做

事件进入管家前**必走** EventNormalizer，处理：

- **dedup** — 同一事件 N 秒内多次,只保留第一个
- **debounce** — 抖动事件（温度阈值附近反复触发）合并
- **filter** — 用户已离开/已知不感兴趣 → 丢弃
- **augment** — 补全上下文（时间、用户在场状态、前置事件链）

**不归一化直接喂 LLM = 事件风暴把管家打挂**。

### 决策 6：触发模式下禁用写操作工具

| 模式 | 允许的工具 | 禁止的工具 |
|------|-----------|-----------|
| `parent_agent="user"` | 全部 | - |
| `parent_agent="trigger:*"` | 只读工具 (web_fetch / memory_query) | 写操作工具 (calendar_create / 设备写控制) |

**理由**：管家被事件叫起来 → 反馈文本 → 路由设备。**不应该**在事件触发模式下
直接调写操作 —— 那会形成"事件→管家→写设备→又发事件→又叫管家"循环。

写操作必须经过用户在场的链路（用户说话确认）。

## 3. 完整数据流

```
┌──────────────┐
│ 设备 (门锁)   │ 状态变化
└──────┬───────┘
       │ (设备 SDK callback / webhook)
       ▼
┌──────────────┐
│ DeviceAdapter │ 适配成标准 Event 协议
└──────┬───────┘
       │ publish
       ▼
┌──────────────┐
│  EventBus    │ Redis Streams
│              │
│ topic:       │
│ device.door  │
└──────┬───────┘
       │ subscribe
       ▼
┌──────────────┐
│EventNormalizer│ dedup / debounce / filter / augment
└──────┬───────┘
       │ 标准化 event
       ▼
┌──────────────┐
│ EventTrigger │ 构造 user_input 文本
│              │ 调 orchestrator.ainvoke(
│              │   user_input="[系统事件] 门锁刚被打开...",
│              │   parent_agent="trigger:device.door"
│              │ )
└──────┬───────┘
       │ answer text
       ▼
┌──────────────┐
│AnswerRouter  │ 查 PresenceService
│              │ 选目标设备 (客厅音响)
└──────┬───────┘
       │ TTS / Push
       ▼
┌──────────────┐
│  目标设备     │ 主人听到："欢迎回家"
└──────────────┘
```

## 4. Phase 7 拆分

| 子阶段 | 内容 | 工作量 |
|--------|------|--------|
| 7.0 | 协议 + 抽象接口 (本 ADR 落地) | 1 周 |
| 7.1 | EventNormalizer + 简单 dedup | 1 周 |
| 7.2 | ainvoke 返 ButlerResponse | 1 天 |
| 7.3 | Redis Streams EventBus 实现 | 1 周 |
| 7.4 | HomeAssistant DeviceAdapter | 2 周 |
| 7.5 | PresenceService + AnswerRouter | 2 周 |
| 7.6 | 端到端测试 + 误触发兜底 | 1 周 |

**总计 8 周**。**不是 1 天**。

## 5. 不变量

1. ButlerOrchestrator 主体（ainvoke / astream）只在 Phase 7.2 改 return type，**不改逻辑**
2. 事件触发不绕过 EventNormalizer（违反则视为 bug）
3. parent_agent 协议一旦敲定**不修改**（新增 OK，删改需 ADR）
4. 写操作工具不暴露给 trigger 模式（白名单硬编码在 Phase 5 实施）

## 6. 与其他 ADR 的关系

| ADR | 关系 |
|-----|------|
| ADR-005（Supervisor） | EventTrigger 调 Sub-Agent 走同一 delegate_to_xxx 链路 |
| ADR-006（Skill ≠ Sub-Agent） | Event 触发后管家 LLM 可加载 Skill 处理 |
| ADR-007（Skills 格式） | 无直接关系 |

## 7. 未来回看清单

Phase 7 真正启动时回看本文档,确认：

- [ ] Redis Streams 是否仍是最佳选型
- [ ] parent_agent 协议是否仍合理
- [ ] 写操作工具白名单是否需要调整
- [ ] 是否需要引入"事件优先级"概念（normal/urgent/silent）
