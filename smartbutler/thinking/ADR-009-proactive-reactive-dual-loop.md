# ADR-009: Proactive / Reactive 双循环架构

- **状态**: 🆕 提议中
- **日期**: 2026-10-08
- **修订**: 替换 §5.8 (Phase 7) 中"复用 ainvoke"方案

## 1. 背景

### 1.1 当前架构

`thinking/loop/ButlerOrchestrator.ainvoke()` 是唯一入口——**只支持"用户问 → 管家答"**。

Phase 7 计划 (§5.8) 通过 `parent_agent="trigger:device.door"` 字段**复用** ainvoke 入口，
让事件"看起来像用户消息"——再通过 parent_agent 字段区分"事件触发" vs "用户主动"。

### 1.2 这个方案的问题

复用 ainvoke 带来**4 个根本性冲突**：

| 维度 | ainvoke 假设 | Proactive 场景 |
|------|-------------|----------------|
| **触发者** | 永远有用户输入 | 可能没用户 |
| **沉默** | 不允许（用户问了必须答） | 70%+ 应该沉默（"好管家话不多"）|
| **回复对象** | 当前用户 | 可能是"事件源"设备 + 用户 |
| **路由** | 单一 HTTP/WebSocket 入口 | 多种设备（音响/手机/邮件）|

**强行复用** = **用错抽象**。把"主动服务"塞进"被动回答"的语义里，
要么丢沉默、要么丢路由灵活性。

### 1.3 用户决策

> **"proactive 应该是跟 reactive 并列的，另一个循环"** —— 用户原话

> **"proactive 是 thinking 的一种思考模式，不是外层包装"** —— 用户原话

## 2. 决策

### 2.1 核心架构

**ProactiveLoop 和 ReactiveLoop 是 `thinking/loop/` 下的两条并列循环**。

```
                          ┌──────────────────────────┐
                          │   EventBus (events)      │
                          └────────────┬─────────────┘
                                       │
                            EventTrigger.route(event)
                                       │
                          ┌────────────┴────────────┐
                          ▼                          ▼
              ┌────────────────────┐    ┌────────────────────────┐
              │  ReactiveLoop      │    │  ProactiveLoop         │
              │  (Phase 4 已有)     │    │  (Phase 5+ 新建)        │
              │                    │    │                        │
              │  Trigger: user     │    │  Trigger: device/timer │
              │  Output: 答用户     │    │  Output: 主动推送/沉默  │
              └────────────────────┘    └────────────────────────┘
                          │                          │
                          │ (允许内部调用)            │
                          └──────────┬───────────────┘
                                     ▼
                             AnswerRouter
                        (音响 / 推送 / 邮件)
```

### 2.2 EventTrigger 路由规则

```python
def route(event: BaseEvent) -> LoopType:
    if event.source in {"user", "interface"}:
        return LoopType.REACTIVE
    return LoopType.PROACTIVE
```

| Event.source | 路由 | 理由 |
|---|---|---|
| `user` / `interface` | Reactive | 有人主动问，必须答 |
| `device` | Proactive | 设备事件，无人问 |
| `timer` | Proactive | 定时器触发 |
| `voice` (唤醒但没说话) | Proactive | 可能静默 |
| `webhook` | Proactive | 外部回调 |
| `internal` | Proactive | 内部状态变化 |

### 2.3 ProactiveLoop 结构

```python
class ProactiveLoop:
    """主动循环——管家自己观察、思考、决定要不要开口。"""

    async def tick(self, event: BaseEvent) -> ProactiveResult:
        # 1. 触发判断: 这事儿要不要主动说?
        decision = await self.reasoning.should_respond(event)

        if not decision.should_respond:
            return ProactiveResult.silent()

        # 2. 决定说什么: 走 ReAct 拿工具
        message = await self.react_chain.run(
            trigger=decision,
            event=event,
        )

        # 3. 主动推送
        return ProactiveResult.acted(message=message, urgency=decision.urgency)
```

### 2.4 Reactive → Proactive 内部调用

Reactive 链尾**允许**调 Proactive 拿"主动建议"——但**仅在特定场景**触发。

```python
class ReactiveLoop:
    async def run(self, user_msg):
        # 1. 正常 ReAct 答用户
        answer = await self.react_chain.run(user_msg)

        # 2. 规则触发: 用户问"该做什么" / "吃什么" → 追加建议
        if self._should_request_proactive(user_msg):
            advice = await self.proactive_loop.advise(
                context=user_msg, current_answer=answer
            )
            if advice:
                return answer + advice

        return answer
```

**判定规则**（**不调 LLM**——避免开销）：

| 用户消息模式 | 触发 Proactive 建议 |
|---|---|
| "该吃/做/喝什么" | ✅ |
| "我该做/怎么办" | ✅ |
| "几点了" / "天气" | ❌ |
| 其他 | ❌ |

**未来升级**：Phase 6 emotion 上线后，规则可升级为 Persona 驱动判断。

### 2.5 沉默支持 (Proactive 独有)

```python
class ProactiveResult(BaseModel):
    acted: bool
    message: str | None = None
    push_channel: str | None = None   # "speaker" / "phone" / "email"
    silence_reason: str | None = None # 沉默时填: "default_silent" / "urgency_too_low" / ...

    @classmethod
    def silent(cls, reason: str = "default_silent") -> "ProactiveResult":
        return cls(acted=False, silence_reason=reason)
```

**默认 70%+ 概率应该 silent**——`should_respond` 是"严格条件"，不是"默认通过"。

### 2.6 parent_agent 协议（替换 §5.8 方案）

**旧方案**: `parent_agent="trigger:device.door"` —— 复用 ainvoke + 内部判断
**新方案**: `parent_agent` 字段**保留**（语义不变），但**不再作为路由依据**

```python
# ReactiveLoop 入口
async def ainvoke(self, user_input, parent_agent="user"):
    # 不需要检查 parent_agent,因为 EventTrigger 已经路由过了

# ProactiveLoop 入口
async def proactive_tick(self, event):
    # parent_agent 已经在 event.parent_agent_tag 里
    # LLM 通过 messages 里看到 "parent_agent=trigger:device.door" 知道是被事件叫起
    ...
```

**关键变化**：
- ❌ **不再靠 parent_agent 字段路由**（避免语义混淆）
- ✅ **EventTrigger.route() 显式路由**
- ✅ `parent_agent` 保留作为**信息字段**——LLM 看到知道"我是被事件叫起"

## 3. 架构影响

### 3.1 模块布局

```
smartbutler/
├── thinking/
│   ├── loop/
│   │   ├── reactive_loop.py        ← Phase 4 已有 (重构抽 BaseLoop)
│   │   ├── proactive_loop.py       ← 🆕 Phase 5
│   │   ├── base_loop.py            ← 🆕 抽象基类
│   │   ├── loop_type.py            ← 🆕 LoopType 枚举
│   │   ├── graph.py                ← 保留 (Reactive 专用)
│   │   └── orchestrator.py         ← 双入口
│   ├── proactive/                  ← 🆕 主动专属
│   │   ├── reasoning.py            # ProactiveReasoning: 该不该主动
│   │   ├── decision.py             # ProactiveDecision 模型
│   │   └── result.py               # ProactiveResult 模型
│   ├── reasoning/                  ← 已有 (共享)
│   ├── decision/                   ← 已有 (共享)
│   ├── memory_access/              ← 已有 (共享)
│   └── prompt/                     ← 已有 (共享)
│
├── events/
│   ├── core/
│   │   └── trigger.py              ← Phase 7.1 实现 route()
│   └── routing/
│       └── answer_router.py        ← Phase 7.5 (Proactive 输出用)
```

### 3.2 共享与独占

| 能力 | ReactiveLoop | ProactiveLoop |
|------|--------------|---------------|
| LLM 调用 | ✅ | ✅ |
| Tool 调用 | ✅ | ✅ |
| Memory 访问 | ✅ | ✅ |
| Personality 注入 | ✅ | ✅ |
| Skill 注入 | ✅ | ✅ |
| **沉默支持** | ❌ | ✅ |
| **主动推送路由** | ❌ | ✅ (AnswerRouter) |
| **触发判断** | ❌ | ✅ (ProactiveReasoning) |
| **写操作** | ✅ | ⚠️ 建议只读 (防循环) |

### 3.3 Phase 7 §5.8 修订

| §5.8 旧方案 | 新方案 |
|---|---|
| 复用 ainvoke + parent_agent 路由 | **双入口：ainvoke / proactive_tick** |
| EventTrigger 构造 user_input 文本 | **EventTrigger.route() 路由** |
| return ButlerResponse | **Reactive: 返 str; Proactive: 返 ProactiveResult** |
| 主体 ButlerOrchestrator 改 return type | **主体不动，新增 proactive_tick 方法** |

**好处**：
- ButlerOrchestrator 主体**只新增方法，不改 ainvoke 签名**
- 事件触发**不再伪装用户消息**——干净
- 沉默支持**作为 Proactive 一等公民**——不再 hack

## 4. 现在实现 vs Phase 6 推迟

| 内容 | 现在实现 | 推迟到 Phase 6 |
|------|----------|----------------|
| ProactiveLoop 框架 | ✅ | |
| ProactiveReasoning 占位 | ✅ | |
| EventTrigger.route() | ✅ | |
| ButlerOrchestrator.proactive_tick() 入口 | ✅ | |
| Reactive → Proactive 内部调用（规则触发）| ✅ | |
| 最小可用场景（定时器沉默判断）| ✅ | |
| 真实 Memory 检索 | | ✅ |
| Persona 驱动主动建议 | | ✅ |
| 多模态感知 (Phase 9) | | ✅ |
| 真实设备接入 (Phase 8) | | ✅ |

**理由**：
- ProactiveLoop 是 ReactiveLoop 的**镜像**结构，复用现有 thinking/ 底层
- 不依赖 emotion/memory 的真实数据
- Phase 5/6 上线后，**不重构**——只填实现

## 5. 不变量

1. **ProactiveLoop 不调 ReactiveLoop**——主动不打断自己
2. **ReactiveLoop 可调 ProactiveLoop**——在用户对话中追加建议
3. **沉默是一等公民**——`ProactiveResult.acted=False` 必须能被上游正确处理
4. **写操作不暴露给 Proactive 触发的工具**——防"事件→管家→写设备→又发事件"循环
5. **parent_agent 字段语义不变**——只是不再作为路由依据
6. **两条循环共享 thinking/decision/ + tools/ + memory_access/**——不重复造轮子

## 6. 验证

- [ ] `EventTrigger.route()` 单测：user 走 Reactive，其他走 Proactive
- [ ] `ProactiveLoop.tick()` 单测：默认 silent；should_respond=True 时调 react
- [ ] `ReactiveLoop._should_request_proactive()` 单测：规则匹配
- [ ] 1 个 e2e：定时器事件 → ProactiveLoop → 沉默
- [ ] 1 个 e2e：定时器事件 → ProactiveLoop → 主动推送

## 7. 修订影响

| 文档 | 章节 | 修订 |
|------|------|------|
| `TECHNICAL_DESIGN.md` | §3.2.5 thinking | + proactive/ 子模块 + reactive/proactive 镜像 |
| `TECHNICAL_DESIGN.md` | §5.8 事件驱动 | **重写**为双循环路由方案 |
| `TECHNICAL_DESIGN.md` | §6.2 中期扩展 | 主动服务从"中期"提前到 Phase 5+ |
| `README.md` | 当前进度 | + thinking/proactive/ + reactive/proactive 双循环 |
| `events/ADR-008-event-driven.md` | §3 与架构关系 | 指向 ADR-009 替代 |
