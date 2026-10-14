# Phase 7 技术方案:事件驱动 + 路由

> **范围**:Phase 7 全周期(7.0 协议 / 7.1 Server / 7.2 结构化 reply / 7.3 内存总线 / 7.4 Normalizer+Trigger / 7.5 AnswerRouter+OutputAdapter / 7.6 VoiceAdapter / 7.7 TTS / 7.8 CameraAdapter / **7.9 多模态记忆(观察项,不启动)**)
> **不包含**:Phase 6b ProactiveLoop 真实化(LLMProactiveReasoning)/ Phase 8 领域 Sub-Agent / 后续多节点 Redis Streams
> **前置依赖**:Phase 6.1+6.2 P0 Memory ✅ / Phase 4b ProactiveLoop 框架 ✅ / Phase 5 Skill ✅
> **关联 ADR**:`smartbutler/events/ADR-008-event-driven.md`(事件驱动决策)+ `smartbutler/thinking/ADR-009-proactive-reactive-dual-loop.md`(双循环)+ `README §3` 架构约束

> **🆕 重大修正(2026-10-14)**:原 ADR-008 的"Redis Streams / 8 周拆分"对单进程 dev_chat **过度设计**。
> **现状**:协议 + Server + 内存总线 + Stub Adapter **均已落地**(见 §0.1),EventBus 链路**已经能跑通**。
> **真正缺的是中间环节**:Normalizer / EventTrigger / AnswerRouter **抽象已定义,0 行实现**——直接由 `ButlerServer._on_bus_event` 旁路了。
> **修正后的节奏**:先补齐 Normalizer + Trigger(7.4)+ 真实 OutputAdapter(7.5/7.7),再做真硬件(7.6 Voice / 7.8 Camera)。Redis 升级留 Phase 8+ 多节点。

> **🆕 多模态记忆决策(2026-10-14,本轮敲定)**:**感知层(7.6/7.8)采集音视频,但记忆层(7.6/7.8/7.9)统一先转文本进 MemoryFacade**——`text + QwenEmbedder` 是默认,**家用 95% 场景召回精度足够**。
> - **理由 1**:Omni VLM(7.8 接入)产出 `caption + matched_rules + speeches + suggestions` 本就是结构化文本,直接 `MemoryFacade.remember(text=caption)` 走现有 Qwen text embedding。
> - **理由 2**:"这条裙子 vs 昨天那条蓝裙子"这种场景,只要 Omni caption 抽到"蓝"就能召回到;caption 漏掉才需要多模态 embedding,**95% 情况不会漏**。
> - **理由 3**:"以图搜图 / 跨模态检索"家用 1 年触发 1-5 次,投入 CLIP/ImageBind/EmbeddingGemma 2(1GB+ 依赖)不划算。
> - **结论**:**7.6/7.8 跑通感知流,记忆层不引入多模态 embedding**。`ReactiveSubmission.metadata: dict[str, Any]` 和 `ButlerReply.metadata: dict[str, Any]` **已经预留扩展位**。
> - **7.9 EmbeddingGemma 2** 标为 **"未来观察项,不启动"**——等 3-6 个月后看真实用户行为,触发条件见 §12.3。
> - **本设计文档同步补充 §12 "多模态记忆决策"**——详细记录这条决策的依据和未来切换路径。

---

## 0. 当前状态盘点(2026-10-14)

### 0.1 已交付(7.0 + 7.1 + 7.2 + 7.3)

| 模块 | 文件 | 状态 |
|------|------|------|
| **BaseEvent 协议** | `smartbutler/events/core/event.py` | ✅(`EventSource`/`EventPriority` 4×3 枚举)|
| **EventBus 抽象接口** | `smartbutler/events/core/event_bus.py` | ✅(publish/subscribe/start/stop/ack/replay) |
| **EventNormalizer 抽象** | `smartbutler/events/core/normalizer.py` | ✅(纯接口) |
| **EventTrigger 抽象** | `smartbutler/events/core/trigger.py` | ✅(纯接口) |
| **AnswerRouter 抽象** | `smartbutler/events/routing/answer_router.py` | ✅(纯接口) |
| **PresenceService 抽象** | `smartbutler/events/routing/presence.py` | ✅(纯接口) |
| **DeviceEvent 协议** | `smartbutler/events/protocol/device_event.py` | ✅(DeviceType 14 枚举) |
| **VoiceEvent 协议** | `smartbutler/events/protocol/voice_event.py` | ✅(VoiceAction 4 枚举) |
| **TimerEvent 协议** | `smartbutler/events/protocol/timer_event.py` | ✅ |
| **InMemoryEventBus** | `smartbutler/server/bus.py` | ✅(支持通配符 + history + replay) |
| **ButlerServer** | `smartbutler/server/runtime.py` | ✅(submit_reactive / submit_proactive / start / stop) |
| **ServerConfig** | 同上 | ✅(event_bus 注入 + enable_event_bus 开关) |
| **ReactiveSubmission / ProactiveSubmission / ButlerReply** | `smartbutler/server/adapters/__init__.py` | ✅(ButlerReply 含 `source="reactive"/"proactive"` 区分) |
| **InputAdapter 抽象** | 同上 | ✅(set_sink + start + stop) |
| **OutputAdapter 抽象** | 同上 | ✅(push + 内部 try/except 兜底) |
| **InMemoryInputAdapter** | `smartbutler/server/adapters/stub.py` | ✅(enqueue_reactive / publish_proactive / send_now) |
| **InMemoryOutputAdapter** | 同上 | ✅(replies 列表 + clear + last) |
| **PrintOutputAdapter** | 同上 | ✅(stdout 打印,dev_chat 用) |
| **build_default_server** | `smartbutler/server/runtime.py` | ✅(默认 InMemoryEventBus) |

**关键判断**:EventBus 链路**已经**端到端可跑——`dev_chat → enqueue_reactive → ButlerServer.submit_reactive → orchestrator.ainvoke → PrintOutputAdapter`。`submit_proactive → publish 到 EventBus → _on_bus_event → orchestrator.proactive_tick → OutputAdapter` 这条**也通**,只是**绕过了 EventNormalizer + EventTrigger**(见 §0.2)。

### 0.2 真实缺口(本方案要补)

| 缺口 | 现状 | 风险 |
|------|------|------|
| **EventNormalizer 0 实现** | `normalizer.py` 是纯 `@abstractmethod` | 烟雾报警器 1 秒推 60 事件 → 管家说 60 次"着火了" |
| **EventTrigger 0 实现** | `trigger.py` 是纯 `@abstractmethod` | 缺限速 + 缺 source→loop 路由决策 |
| **`_on_bus_event` 旁路** | `runtime.py:298-322` 直接 `proactive_tick`,**没走 Normalizer/Trigger** | EventBus producer 端(VoiceAdapter/CameraAdapter)接入后必然爆管 |
| **EventBus 历史无去重** | `InMemoryEventBus.replay` 按 topic 返历史,但不 dedup | 单测 debug 重放时同一事件出现 N 次 |
| **AnswerRouter 0 实现** | `routing/answer_router.py` 是纯接口 | ProactiveResult → 选哪个 OutputAdapter 推送没定 |
| **PresenceService 0 实现** | `routing/presence.py` 是纯接口 | 多设备时答完推谁不可知(Phase 7.5 简化:走 default + 广播) |
| **VoiceAdapter 0 实现** | `events/protocol/voice_event.py` 协议有,没 producer | USB 麦 / 智能音响无法接入 |
| **CameraAdapter 0 实现** | `events/protocol/device_event.py` 协议有,没 producer | 摄像头无法接入 |
| **TTS OutputAdapter 0 实现** | 只有 `PrintOutputAdapter` / `InMemoryOutputAdapter` | 管家不会"说话",只能"打印" |

**已确认:Phase 7 协议层 + 抽象层 = 100% 落地,实现层 = ~10%(只 Server + Stub)。**

---

## 1. 整体架构

### 1.1 模块依赖图

```
┌──────────────────────────────────────────────────────────────────────┐
│                       Phase 7 模块分层                                 │
├──────────────────────────────────────────────────────────────────────┤
│                                                                      │
│  ┌────────────────┐  ┌────────────────┐  ┌────────────────┐          │
│  │ VoiceAdapter   │  │ CameraAdapter  │  │ DeviceAdapter  │          │
│  │ (7.6 新)        │  │ (7.8 新)        │  │ (8+ 新)        │          │
│  │ USB 麦/智能音响  │  │ 摄像头/感知流水线│  │ 门锁/灯/空调  │          │
│  └────────┬───────┘  └────────┬───────┘  └────────┬───────┘          │
│           │ publish          │ publish          │ publish           │
│           ▼                  ▼                  ▼                   │
│  ┌──────────────────────────────────────────────────────────┐        │
│  │              EventBus (7.3 已实现)                        │        │
│  │         InMemoryEventBus / (Phase 8+: Redis Streams)     │        │
│  └────────────────────────┬─────────────────────────────────┘        │
│                           │ consumer                                │
│                           ▼                                          │
│  ┌──────────────────────────────────────────────────────────┐        │
│  │         EventNormalizer (7.4 新 — 抽象已有)              │        │
│  │   DedupNormalizer / DebounceNormalizer / FilterNormalizer│        │
│  └────────────────────────┬─────────────────────────────────┘        │
│                           │ 过滤后的事件                              │
│                           ▼                                          │
│  ┌──────────────────────────────────────────────────────────┐        │
│  │         EventTrigger (7.4 新 — 抽象已有)                  │        │
│  │         DefaultEventTrigger(route + 限速 + 兜底)          │        │
│  │     ├─ source=user/interface → Reactive ainvoke          │        │
│  │     └─ source=device/voice/timer/webhook → Proactive tick│        │
│  └────────────────────────┬─────────────────────────────────┘        │
│                           │ answer / ProactiveResult.message        │
│                           ▼                                          │
│  ┌──────────────────────────────────────────────────────────┐        │
│  │  ┌─────────────────────┐  ┌─────────────────────────┐     │        │
│  │  │ AnswerRouter (7.5)   │  │ PresenceService (7.5)    │     │        │
│  │  │ ConsoleAnswerRouter │  │ SimplePresenceService   │     │        │
│  │  └──────────┬──────────┘  └────────────┬────────────┘     │        │
│  │             └──────────┬───────────────┘                  │        │
│  │                        ▼                                  │        │
│  │              选目标 OutputAdapter                          │        │
│  └────────────────────────┬─────────────────────────────────┘        │
│                           │ ButlerReply                              │
│                           ▼                                          │
│  ┌──────────────────────────────────────────────────────────┐        │
│  │              OutputAdapter (7.5 / 7.7 新)                │        │
│  │   ┌─────────────────┐  ┌─────────────────┐  ┌──────────┐ │        │
│  │   │ PrintOutput      │  │ TTSOutput       │  │ PhonePush│ │        │
│  │   │ (已实现)         │  │ (7.7 新)        │  │ (8+ 新)  │ │        │
│  │   └─────────────────┘  └─────────────────┘  └──────────┘ │        │
│  └──────────────────────────────────────────────────────────┘        │
│                                                                      │
│  ┌──────────────────────────────────────────────────────────┐        │
│  │  voice/ 子模块 (7.6 + 7.7 共享)                          │        │
│  │   capture/ (mic_capture) + vad/ (silero)                │        │
│  │   asr/ (funasr) + wakeword/ (porcupine)                 │        │
│  │   tts/ (edge / offline piper)                           │        │
│  └──────────────────────────────────────────────────────────┘        │
│                                                                      │
│  ┌──────────────────────────────────────────────────────────┐        │
│  │  perception/ 子模块 (7.8 新)                            │        │
│  │   gate/ (帧差分) + identity/ (DeepSORT + ReID)          │        │
│  │   omni/ (VLM caption 抽 facts) + buffer/ (音视频对齐)    │        │
│  └──────────────────────────────────────────────────────────┘        │
│                                                                      │
│  ┌──────────────────────────────────────────────────────────┐        │
│  │  emotion/memory/ (Phase 6 已落地)                       │        │
│  │   MemoryFacade.remember(text=caption)                    │        │
│  │   ← 7.6/7.8 感知层输出统一转文本进记忆                   │        │
│  │   ← QwenEmbedder(text-only) 足够 95% 家用场景            │        │
│  │   ← 7.9 观察项:多模态 embedding(EmbeddingGemma 2)按需启动│        │
│  └──────────────────────────────────────────────────────────┘        │
└──────────────────────────────────────────────────────────────────────┘
```

### 1.2 数据流(完整链路)

```
完整 Reactive 链路(7.5+):
  用户说话 → 智能音响/USB 麦
  → voice/capture/mic_capture (sounddevice 采集)
  → voice/vad/silero (VAD 门控)
  → voice/asr/funasr (ASR 转写 → asr_text="杰贝兹,今天周几")
  → events/adapters/voice_adapter.py 构造 VoiceEvent(asr_text=...)
  → EventBus.publish(voice.command)
  → InMemoryEventBus consumer
  → DedupNormalizer.normalize(过滤重复)
  → DefaultEventTrigger.should_handle(限速)
  → DefaultEventTrigger.handle → route(source=voice → Reactive)
  → orchestrator.ainvoke("杰贝兹,今天周几？", parent_agent="trigger:voice.command")
  → answer text
  → ConsoleAnswerRouter.route → PresenceService 选目标设备
  → TTSOutputAdapter.push(answer)
  → voice/tts/edge_impl.synthesize → mp3
  → 本机扬声器 / 智能音响播放

完整 Proactive 链路(7.6+):
  烟雾报警器触发
  → DeviceAdapter 构造 DeviceEvent(device_type=SMOKE_DETECTOR, priority=URGENT)
  → EventBus.publish(device.smoke_detector.alarm)
  → InMemoryEventBus consumer
  → DedupNormalizer (短窗口内去重)
  → DefaultEventTrigger.should_handle(限速)
  → DefaultEventTrigger.handle → route(source=device → Proactive)
  → orchestrator.proactive_tick(event)
  → ProactiveResult.acted=True, message="检测到厨房烟雾,正在通知家人"
  → ConsoleAnswerRouter.route(priority=URGENT → 推全屋)
  → TTSOutputAdapter.push(message) 多次(每个 OutputAdapter 各一次)
  → 全屋智能音响同时播放

音视频感知 → 记忆(7.6 + 7.8 跑通,记忆层文本化):
  ┌─ VoiceAdapter(7.6)──────────────────────────────────────────┐
  │  ASR 转写后:asr_text="主人说蓝裙子比昨天那条好看"           │
  │  → VoiceEvent(asr_text, priority=NORMAL)                    │
  │  → EventBus.publish                                        │
  │  → Trigger.handle → Reactive ainvoke → 管家答              │
  │  → 【同时】MemoryFacade.remember(                            │
  │      text=asr_text,  ← 直接文本进记忆(Qwen text embedding)  │
  │      source="voice",                                        │
  │      metadata={device_id, timestamp}                        │
  │    )                                                        │
  │  → 下次召回"女主人最近说过什么"→ 命中这条                    │
  └─────────────────────────────────────────────────────────────┘

  ┌─ CameraAdapter(7.8)────────────────────────────────────────┐
  │  摄像头帧 → MultimodalCollector(3-5s window)               │
  │  → Gate(帧差分)→ Identity(DeepSORT + ReID)                 │
  │  → Omni(VLM, 抽 caption + matched_rules + speeches)        │
  │  → caption="女主人穿蓝色长款雪纺裙在玄关换鞋"                │
  │  → DeviceEvent(perception, payload={caption, person_id,    │
  │                       matched_rules, ...})                  │
  │  → EventBus.publish                                        │
  │  → 【同时】MemoryFacade.remember(                            │
  │      text=caption,    ← Omni 输出是结构化文本,直接进记忆    │
  │      source="perception",                                  │
  │      metadata={                                          │
  │        person_id, location, scene_tags=["蓝","长款","雪纺"],│
  │        image_ref="/data/butler/images/2026/10/14/xxx.jpg"  │
  │      }                                                     │
  │    )                                                        │
  │  → 下次召回"最近穿的蓝裙子" → 命中(因为 caption 含"蓝")     │
  │                                                            │
  │  【未来 7.9 观察项】若 caption 漏"蓝"导致召不回,             │
  │    才考虑 EmbeddingGemma 2 直接以图召图(见 §12)             │
  └─────────────────────────────────────────────────────────────┘
```

### 1.3 关键不变量(本方案全程必须遵守)

1. **EventBus 抽象稳定**:`EventBus` 接口一旦敲定**不删不改**(沿用 ADR-008 §5 不变量 #1)。
2. **Normalizer 不修改原事件**:`BaseEvent` 是 frozen,Normalizer 必须返回新事件或 None(沿用 `normalizer.py:32-34` 注释)。
3. **Trigger 内部 try/except**:`EventTrigger.handle` **不抛异常**——否则 EventBus consumer 会反复重试同一条事件(沿用 `trigger.py:65-67` 注释)。
4. **写操作工具不暴露给 trigger 模式**:`parent_agent="trigger:*"` 模式下管家 LLM 只能用只读工具,不能直接调设备写(沿用 ADR-008 §6 决策 6)。
5. **proactive 沉默优先**:`ProactiveResult.acted=False` 不下发到 OutputAdapter(沿用 `runtime.py:317-318` 实现)。
6. **OutputAdapter 内部 try/except**:`OutputAdapter.push` **不抛异常**——吞掉 + log(沿用 `adapters/__init__.py:148-150` 注释)。
7. **PresenceService fallback 必做**:`get_active_device` 返 None → AnswerRouter 必须能 fallback 到默认 OutputAdapter,不能挂。
8. **ButlerReply 形态不变**:`source` 字段(`"reactive"/"proactive"`)一旦敲定**不删改**(Phase 7.5+ 新增字段 OK,删改需 ADR)。
9. **多 OutputAdapter 广播**:`ButlerServer._dispatch_reply` 用 `asyncio.gather(..., return_exceptions=True)` 并联广播,任一失败不影响其他(沿用 `runtime.py:331-337` 实现)。
10. **ProactiveLoop tick 必须有结果**:`proactive_tick(event)` 返 `ProactiveResult`(不可变,`acted`/`message`/`urgency`/`silence_reason` 4 字段),不能返 None(沿用 `orchestrator.py:580-609` 契约)。
11. **感知层多模态,记忆层单模态(文本)**:VoiceAdapter(7.6)/CameraAdapter(7.8)可以采集音频/视频原始流,**但进 MemoryFacade 时必须先转文本**——ASR 转写后的 `asr_text`、Omni VLM 输出的 `caption` 直接 `remember(text=...)` 走 Qwen text embedding。**当前不引入多模态 embedding**(7.9 观察项,见 §12)。
12. **感知层原始数据不持久化**:`voice/capture/` 和 `perception/` 子模块只做流式处理,音频帧/视频帧**不写盘**(仅 `image_ref` 存盘,在 7.8 CameraAdapter 落地时评估是否需要短期存档)。**原始数据流式丢弃**,Miloco 思路沿用。
13. **多模态扩展位预留**:`ReactiveSubmission.metadata: dict[str, Any]` 和 `ButlerReply.metadata: dict[str, Any]` 已经预留——7.9 若启动 EmbeddingGemma 2,新增字段直接走 metadata,不破坏现有协议。

---

## 2. 子阶段拆分(7.0 → 7.8)

> 严格按依赖顺序——每阶段必须前一阶段跑通单测 + 端到端测试后才启动。
> **理由**:链路一断,后面所有 adapter 都跑不通。

### 2.1 Phase 7 状态总表

| 子阶段 | 模块 | 自研量 | 状态 | 启动前置 |
|--------|------|--------|------|----------|
| **7.0** 协议 + 抽象 | `events/{core,protocol,routing}/` | ~600 行 | ✅ **已落地** | — |
| **7.1** Server + InMemoryEventBus | `server/{runtime,bus,adapters/}` | ~700 行 | ✅ **已落地** | 7.0 |
| **7.2** 结构化 reply | `ButlerReply` 数据类 | (7.1 内) | ✅ **已落地** | 7.1 |
| **7.3** 内存总线 | `InMemoryEventBus` | (7.1 内) | ✅ **已落地**(Redis 升级 8+) | 7.1 |
| **7.4** Normalizer + Trigger | `events/core/normalizer_dedup.py` + `trigger_default.py` + `server/runtime.py` 改 `_on_bus_event` | ~150 行 + ~150 测试 | 🆕 **下一站** | 7.1 |
| **7.5** AnswerRouter + OutputAdapter | `events/routing/answer_router_console.py` + `presence_simple.py` + `server/adapters/console_output.py`(或扩 InMemoryOutputAdapter) | ~200 行 + ~80 测试 | 🆕 | 7.4 |
| **7.6** VoiceAdapter(第一个真硬件,音频输入)| `events/adapters/voice_adapter.py` + `voice/{capture,vad,asr,wakeword}/` | ~500 行 + ~120 测试 | 🆕 | 7.5 |
| **7.7** TTSOutputAdapter(第一个真输出)| `voice/tts/{edge,offline}.py` + `events/adapters/tts_output_adapter.py` | ~250 行 + ~80 测试 | 🆕 | 7.5 |
| **7.8** CameraAdapter(视频输入,音视频流水线收口)| `events/adapters/camera_adapter.py` + `perception/{gate,identity,omni}.py` + `voice/capture/buffer.py` | ~800 行 + ~150 测试 | 🆕 | 7.7 |
| **7.9** 多模态记忆 EmbeddingGemma 2(观察项,当前不启动)| `capabilities/embedding/gemma2.py` + `factory.py` 扩展 + `LongTermStore` 多模态索引 | ~380 行 + ~120 测试(不写) | ⏳ **观察项** | 7.8 + 3-6 个月真实使用反馈 |

**Phase 7 实际自研量**(7.4-7.8):~1900 行 + ~580 测试,约 5-7 周(单人节奏)。
**7.9 不计入**——只有触发条件满足(见 §12.3)才启动。

### 2.2 节奏表(按"端到端可见"切片)

| 里程碑 | 包含子阶段 | 端到端验证 | 预期周期 |
|--------|------------|-----------|----------|
| **M0** 当前已交付 | 7.0+7.1+7.2+7.3 | dev_chat 文本链路 ✅ | 0 |
| **M1 事件链路补全** | +7.4 | 推 1 个 DeviceEvent → 1 次管家回答(不爆管) | 1 周 |
| **M2 真硬件 PoC** | +7.5+7.6+7.7 | 说话 → 管家答 → 本机播放 | 4-5 周 |
| **M3 视频通路** | +7.8 | 摄像头感知 → 命中规则 → 主动播报 | 2-3 周 |

---

## 3. Phase 7.4 详细设计 — Normalizer + Trigger 真实化

> **当前最优先**——EventBus producer 端(VoiceAdapter / CameraAdapter)在它之前**不能接入**。

### 3.1 DedupNormalizer

**职责**:基于 `event_id` + `topic` 的去重——同 `event_id`(或同 topic + device_id + N 秒内)在窗口期内只保留第一个。

**为什么必须**:
- 烟雾报警器 1 秒内会推 60 个相同 `event_id` 事件
- 门锁开启 5 秒内反复触发 5 次
- 智能音响误唤醒推 3 个 `voice.wake_word`
- 不归一化 = 事件风暴把管家打挂(沿用 `normalizer.py:14` 注释)

**实现要点**:
- `dict[event_id, first_seen_ts]` 内存表 + `window_sec` 窗口
- 容量上限 `max_size=10000` 触发 LRU 淘汰(防内存泄漏)
- `asyncio.Lock` 保护(单进程多 consumer 协程并发)
- `normalize` 返 `None` = 丢弃,返原事件 = 放行(后续可返新事件支持 debounce/filter)

**接口不变**:`class DedupNormalizer(EventNormalizer)` — 完全沿用 `normalizer.py:31-41` 抽象。

**不做的事**(留给 Phase 7.4+ 后续):
- ❌ Debounce(多次按门铃合成 1 次)—— 需要按 `(topic, device_id, payload 关键字段)` 算 fingerprint,Phase 7.4.1
- ❌ Filter(用户已知不感兴趣 → 丢弃)—— 需要 PresenceService + 用户偏好,Phase 7.5
- ❌ Augment(补全上下文)—— 需要历史事件关联,Phase 7.5+

### 3.2 DefaultEventTrigger

**职责**:拿到归一化后的事件,**决定走哪条循环** + **限速** + **调用** + **兜底**。

**实现要点**:
- 限速:`dict[user_id, list[float]]` 时间戳表,默认 `max_per_minute=20`,超出 → ack + WARN,不让管家被淹没
- 路由(沿用 ADR-009 §5.9.3):
  - `source in (DEVICE, TIMER, VOICE, WEBHOOK)` → `orchestrator.proactive_tick(event)` → 沉默优先
  - `source in (USER, INTERFACE)` → `orchestrator.ainvoke(event.to_user_input(), parent_agent=event.parent_agent_tag)`
- 兜底:`handle` 内部 try/except,失败 → 返 fallback 文本 `"[error] 管家暂时无法处理该事件"` + 记 ERROR 日志
- ack:成功后 EventBus 调 `bus.ack(event)`(内存版 no-op,但接口调用保留)

**接口不变**:`class DefaultEventTrigger(EventTrigger)` — 完全沿用 `trigger.py:42-83` 抽象。

### 3.3 ButlerServer 改造

**现状**(runtime.py:298-322):
```python
async def _on_bus_event(self, event: BaseEvent) -> None:
    """EventBus consumer 默认 handler:转给 ProactiveLoop。"""
    result = await self._orchestrator.proactive_tick(event, ...)
    # 沉默 / 异常 / 空 message 都不下发
    ...
```

**改造后**:
```python
async def _on_bus_event(self, event: BaseEvent) -> None:
    """EventBus consumer 默认 handler:走 Normalizer + Trigger。"""
    # 1. 归一化(dedup / debounce / filter / augment)
    normalized = await self._normalizer.normalize(event)
    if normalized is None:
        return  # 丢弃
    # 2. Trigger 限速判断
    if not await self._trigger.should_handle(normalized):
        return
    # 3. Trigger 处理(route + 调循环 + 兜底)
    answer = await self._trigger.handle(normalized)
    if not answer or not answer.strip():
        return
    # 4. 包装成 reply,广播给 OutputAdapter
    reply = ButlerReply(
        content=answer, user_id=normalized.user_id,
        session_id=f"proactive-{normalized.event_id}",
        source="proactive", related_event_id=normalized.event_id,
    )
    await self._dispatch_reply(reply)
```

**ServerConfig 新增 2 字段**:
- `normalizer: EventNormalizer | None`(默认 None 时 7.4 实现 `DedupNormalizer()` 自动注入)
- `trigger: EventTrigger | None`(默认 None 时 7.4 实现 `DefaultEventTrigger(orchestrator=...)` 自动注入)

**向后兼容**:`normalizer=None` / `trigger=None` 时 `_on_bus_event` 走老逻辑(直接 `proactive_tick`),保证 7.4 之前的 dev_chat 不破。

### 3.4 端到端测试(7.4 验收)

`tests/server/test_event_bus_e2e.py`:
- **case 1**:publish 1 个 DeviceEvent → 1 次 OutputAdapter 收到
- **case 2**:publish 60 个同 `event_id` DeviceEvent(window=5s) → 只 1 次 OutputAdapter 收到(剩余 59 次 dedup 丢弃)
- **case 3**:publish 30 个不同 `event_id` DeviceEvent(同 user) → 限速 20/min,只 20 次 OutputAdapter 收到
- **case 4**:Trigger 调 `proactive_tick` 抛异常 → OutputAdapter 收到 fallback 文本,EventBus 不死循环
- **case 5**:VoiceEvent(source=VOICE) → Trigger 路由到 Reactive `ainvoke` 路径(走通 parent_agent="trigger:voice.command")

### 3.5 7.4 工作量

| 任务 | 行数 | 1 周节奏 |
|------|------|----------|
| `normalizer_dedup.py` | ~30 行 | Day 1 |
| `trigger_default.py` | ~80 行 | Day 2-3 |
| `runtime.py` 改 `_on_bus_event` + `ServerConfig` 加 2 字段 | ~25 行 | Day 3 |
| `tests/server/test_event_bus_e2e.py` | ~200 行 5 case | Day 4-5 |
| 调通 + 文档(本文件 §3 段同步) | — | Day 6-7 |

**最坏情况 1 周**内跑通:**从 publish 烟雾事件 → 60 个事件去重为 1 个 → Trigger 路由到 ProactiveLoop → 推回 1 条告警文本 → OutputAdapter 收到。**

---

## 4. Phase 7.5 详细设计 — AnswerRouter + PresenceService + OutputAdapter 扩展

> **承上启下**——7.4 让事件链路**不爆管**,7.5 让 ProactiveResult **正确选择 OutputAdapter**。

### 4.1 ConsoleAnswerRouter(最小实现)

**职责**:拿到 `BaseEvent + answer + priority`,选目标 OutputAdapter 列表。

**实现要点**:
- `priority=URGENT` → 推所有 OutputAdapter
- `priority=NORMAL` → 推 PresenceService 选定的 1 个 OutputAdapter
- `priority=SILENT` → 不推(打 DEBUG 日志)
- PresenceService 返 None → fallback 到"全部 OutputAdapter 列表的第 1 个"(默认)

**接口不变**:`class ConsoleAnswerRouter(AnswerRouter)` — 完全沿用 `answer_router.py:32-54` 抽象。

### 4.2 SimplePresenceService(最小实现)

**职责**:回答"用户当前最合适的输出设备是谁"。

**实现要点**(沿用 `presence.py:11-15` 的 5 阶段简化到 7.5a):
- **数据源**:`dict[user_id, PresenceInfo]`(用户显式调 `update()` 写入)
- 7.5 不接蓝牙 / GPS / 摄像头(留给 Phase 8+)
- `get_active_device(user_id)` 返最近写入的 PresenceInfo
- `should_notify(priority)` 沿用 `presence.py:46-54` 实现(已在抽象里)

**接口不变**:`class SimplePresenceService(PresenceService)` — 完全沿用 `presence.py:60-71` 抽象。

### 4.3 多 OutputAdapter 选路

**ButlerServer 改造**:
- 现有 `_dispatch_reply`(runtime.py:331-337)是无脑广播到所有 OutputAdapter
- 7.5 改造:从 `_dispatch_reply` 改成"先问 AnswerRouter 选目标,再 broadcast"
- **向后兼容**:AnswerRouter 未注入时,走老逻辑(广播全部)— 保证 dev_chat 文本链路不破

### 4.4 端到端测试(7.5 验收)

`tests/server/test_answer_router_e2e.py`:
- **case 1**:Reactive 链路(`submit_reactive`)→ 走默认 OutputAdapter(无 AnswerRouter)
- **case 2**:Proactive 链路 + `priority=NORMAL` → 走 PresenceService 选定的 OutputAdapter
- **case 3**:Proactive 链路 + `priority=URGENT` → 推所有 OutputAdapter
- **case 4**:Proactive 链路 + `priority=SILENT` → 不推
- **case 5**:PresenceService 返 None → fallback 到第 1 个 OutputAdapter(不挂)
- **case 6**:Reactive 链路 + AnswerRouter 注入 → 仍然走 Reactive 默认 OutputAdapter(Reactive 不走 AnswerRouter,见 §1.2 注)

> **注**:Reactive 链路**不**走 AnswerRouter——`submit_reactive` 同步等答复,输出通道由调用方决定(REPL 走 stdout,GUI 走屏显)。AnswerRouter 只服务 Proactive 链路。

### 4.5 7.5 工作量

| 任务 | 行数 | 1-2 周节奏 |
|------|------|-----------|
| `answer_router_console.py` | ~70 行 | Day 1-2 |
| `presence_simple.py` | ~50 行 | Day 2-3 |
| `runtime.py` 改 `_dispatch_reply` | ~15 行 | Day 3 |
| `tests/server/test_answer_router_e2e.py` | ~150 行 6 case | Day 4-5 |
| 调通 + 文档 | — | Day 6-7 |

**最坏情况 1-2 周**——PoC 不需要 PresenceService 7.5b/c/d/e,可以先**只**实现 PresenceService 的 in-memory dict 简化版。

---

## 5. Phase 7.6 详细设计 — VoiceAdapter(第一个真硬件)

> **本节是"音频输入"端到端实现**——USB 麦 / 笔记本内置麦 / 智能音响麦克风阵列 → VoiceEvent → EventBus。

### 5.1 模块结构

```
smartbutler/
├── voice/                                # 🆕 独立子模块
│   ├── __init__.py
│   ├── config.py                          # 采样率 / 通道数 / VAD 阈值配置
│   ├── capture/
│   │   ├── __init__.py
│   │   ├── base.py                        # BaseAudioCapture 接口
│   │   └── mic_capture.py                 # sounddevice InputStream
│   ├── vad/
│   │   ├── __init__.py
│   │   └── silero.py                      # Silero VAD(单测可注入 mock)
│   ├── asr/
│   │   ├── __init__.py
│   │   ├── base.py                        # BaseASR 接口
│   │   ├── funasr_impl.py                 # FunASR Paraformer(中文 SOTA)
│   │   └── streaming.py                   # 流式 ASR 封装 + 句子边界
│   ├── wakeword/
│   │   ├── __init__.py
│   │   ├── base.py                        # BaseWakeWordDetector 接口
│   │   └── porcupine_impl.py              # Picovoice Porcupine(可选,Phase 7.6.1)
│   └── tts/                               # Phase 7.7 详
│       ├── __init__.py
│       ├── base.py
│       ├── edge_impl.py
│       └── offline_impl.py
└── events/
    └── adapters/
        ├── __init__.py
        ├── voice_adapter.py               # 🆕 mic → VoiceEvent
        ├── camera_adapter.py              # Phase 7.8
        ├── device_adapter.py              # Phase 8+
        ├── timer_adapter.py               # Phase 8+
        └── tts_output_adapter.py          # Phase 7.7
```

### 5.2 BaseAudioCapture 接口

```python
# smartbutler/voice/capture/base.py
class AudioChunk(BaseChunk):
    audio: np.ndarray      # 16kHz mono float32
    timestamp: float        # unix time
    duration_ms: int

class BaseAudioCapture(ABC):
    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def stream(self) -> AsyncIterator[AudioChunk]: ...

    @abstractmethod
    async def stop(self) -> None: ...
```

### 5.3 MicAudioCapture 实现要点

- `sounddevice.InputStream` 异步回调(callback 推到 `asyncio.Queue`)
- 16kHz mono float32(与 FunASR Porcupine 一致)
- `frame_duration_ms=30`(与 Silero VAD 输入要求一致)
- 跨平台:Windows(WASAPI)/ macOS(CoreAudio)/ Linux(ALSA)— sounddevice 已封装

### 5.4 SileroVAD 门控

- `torch.hub.load("snakers4/silero-vad", "silero_vad")` 4MB 模型
- `is_speech(audio: np.ndarray) -> bool` 单次调用 < 1ms
- 阈值默认 0.5(可在 `voice/config.py` 调)

### 5.5 FunASR ASR 集成

- `funasr.AutoModel(model="paraformer-zh", vad_model="fa-zh-vad", punc_model="ct-punc", device="cpu")`
- 喂入完整一句话音频(VAD + 静音超时判定句子结束)
- 返 `{"text": "杰贝兹,今天周几"}`(含标点)

### 5.6 VoiceAdapter 主流程

```python
# smartbutler/events/adapters/voice_adapter.py
class VoiceAdapter:
    """USB 麦 / 笔记本麦 → EventBus。"""
    def __init__(
        self,
        *,
        bus: EventBus,
        user_id: str,
        device_id: str = "mic_laptop_builtin",
        capture: BaseAudioCapture | None = None,    # 测试可注入 mock
        vad: SileroVAD | None = None,
        asr: FunASR | None = None,
        wake_word_enabled: bool = False,
    ):
        self._bus = bus
        self._user_id = user_id
        self._device_id = device_id
        self._capture = capture or MicAudioCapture(device_id=device_id)
        self._vad = vad or SileroVAD()
        self._asr = asr or FunASR()
        ...

    async def run(self) -> None:
        """主循环:监听 → VAD → ASR → 发事件。"""
        await self._capture.start()
        try:
            async for chunk in self._capture.stream():
                if not self._vad.is_speech(chunk.audio):
                    continue
                self._asr.feed(chunk.audio)
                if self._asr.is_sentence_end():
                    text = self._asr.flush()
                    if text:
                        await self._emit_command_event(text)
        finally:
            await self._capture.stop()

    async def _emit_command_event(self, asr_text: str) -> None:
        event = VoiceEvent.make(
            action=VoiceAction.COMMAND,
            device_id=self._device_id,
            user_id=self._user_id,
            asr_text=asr_text,
            language="zh-CN",
            timestamp=datetime.now().astimezone(),
            priority=EventPriority.NORMAL,
        )
        await self._bus.publish(event)
```

### 5.7 依赖项

新增 `pyproject.toml` 依赖:
- `sounddevice`(跨平台音频 I/O)
- `funasr`(阿里 Paraformer 中文 SOTA)
- `torch`(Silero VAD 依赖)
- 可选: `pvporcupine`(Porcupine 唤醒词,7.6.1)

### 5.8 端到端测试(7.6 验收)

`tests/voice/test_voice_adapter_e2e.py`:
- **case 1**:`BaseAudioCapture` mock → `VoiceAdapter.run()` 收 3 帧 → 1 个 `VoiceEvent.COMMAND` 发布
- **case 2**:VAD 全 False → 0 个 VoiceEvent 发布
- **case 3**:ASR mock 返空字符串 → 0 个 VoiceEvent 发布
- **case 4**:`asr_text` 字段正确填充(从 ASR mock 输入验证)
- **case 5**:`user_id` / `device_id` / `timestamp` 字段正确

### 5.9 7.6 工作量

| 任务 | 行数 | 2-3 周节奏 |
|------|------|-----------|
| `voice/capture/{base,mic_capture}.py` | ~120 行 | Day 1-2 |
| `voice/vad/silero.py` | ~50 行 | Day 2 |
| `voice/asr/{base,funasr_impl,streaming}.py` | ~180 行 | Day 3-5 |
| `voice/wakeword/{base,porcupine_impl}.py` | ~100 行 | Day 5-6(可选 7.6.1) |
| `voice/config.py` | ~30 行 | Day 1 |
| `events/adapters/voice_adapter.py` | ~120 行 | Day 7-9 |
| `tests/voice/test_voice_adapter_e2e.py` | ~250 行 5 case | Day 10-12 |
| 调通 + 文档 + 实际录音 demo 验证 | — | Day 13-15 |

**最坏情况 2-3 周**——`BaseAudioCapture` mock 注入让单测不依赖物理麦克风,大幅降测试成本。

---

## 6. Phase 7.7 详细设计 — TTSOutputAdapter(第一个真输出)

> **本节是"音频输出"端到端实现**——ButlerReply → TTS → 智能音响 / 笔记本扬声器播放。

### 6.1 BaseTTS 接口

```python
# smartbutler/voice/tts/base.py
class BaseTTS(ABC):
    @abstractmethod
    async def synthesize(self, text: str) -> str:
        """合成语音,返 mp3 文件路径。"""
        ...

    @abstractmethod
    async def speak(self, text: str) -> None:
        """合成 + 播放一体化(用于本机扬声器场景)。"""
        ...
```

### 6.2 EdgeTTS 实现(默认)

- `edge-tts` Python 包,微软 Azure 神经 TTS,**免费云端**
- 中文音色 `zh-CN-XiaoxiaoNeural`(温柔女声)
- `Communicate(text, voice).save(path)` 异步保存 mp3

### 6.3 PiperTTS 实现(7.7.1 离线)

- `sherpa-onnx` 本地 TTS,完全离线
- 中文模型 ~60MB,无网络依赖
- 7.7.1 实现,**主推 EdgeTTS(7.7)**

### 6.4 TTSOutputAdapter

```python
# smartbutler/events/adapters/tts_output_adapter.py
class TTSOutputAdapter(OutputAdapter):
    """ButlerReply → TTS → 播放。"""
    def __init__(
        self,
        *,
        tts: BaseTTS | None = None,
        device: str = "laptop_local",     # "laptop_local" / "miot_speaker_01" / "phone_xxx"
    ):
        self._tts = tts or EdgeTTS()
        self._device = device

    async def push(self, reply: ButlerReply) -> None:
        try:
            if self._device == "laptop_local":
                await self._tts.speak(reply.content)
            elif self._device.startswith("miot_"):
                # TODO: Phase 8+ 接 MIoT.text_to_speech
                raise NotImplementedError("MIoT TTS 待 Phase 8+")
            else:
                raise ValueError(f"未知 device: {self._device}")
        except Exception as exc:
            # 沿用不变量 #6:OutputAdapter 内部 try/except,只 log 不抛
            _logger.warning("tts_output_adapter.push_failed", error=str(exc))
```

### 6.5 端到端测试(7.7 验收)

`tests/voice/test_tts_output_e2e.py`:
- **case 1**:`BaseTTS` mock → `TTSOutputAdapter.push()` 调 1 次 `speak()`
- **case 2**:`TTSOutputAdapter.push()` 抛异常 → 内部吞掉,server 不挂
- **case 3**:`device="laptop_local"` → 调 `_tts.speak()`
- **case 4**:`device="miot_xxx"` → 抛 NotImplementedError,被内部 try/except 吞掉
- **case 5**:ButlerReply.content 含多行文本 → 完整传给 TTS

### 6.6 7.7 工作量

| 任务 | 行数 | 1 周节奏 |
|------|------|----------|
| `voice/tts/base.py` | ~30 行 | Day 1 |
| `voice/tts/edge_impl.py` | ~70 行 | Day 1-2 |
| `voice/tts/offline_impl.py` | ~80 行 | Day 3-4(可选 7.7.1) |
| `events/adapters/tts_output_adapter.py` | ~70 行 | Day 3 |
| `tests/voice/test_tts_output_e2e.py` | ~150 行 5 case | Day 4-5 |
| 调通 + 文档 | — | Day 6-7 |

**最坏情况 1 周**——`BaseTTS` mock 注入让单测不依赖网络 / 扬声器。

---

## 7. Phase 7.8 详细设计 — CameraAdapter(视频输入,音视频流水线收口)

> **本节是"视频输入"端到端实现**——摄像头 → 感知流水线(Gate/Identity/Omni)→ DeviceEvent(perception) → EventBus → **Omni caption 转文本进记忆**。
> **核心决策**:**不引入多模态 embedding**——Omni VLM 输出的 `caption + matched_rules + speeches + suggestions` 本就是结构化文本,直接 `MemoryFacade.remember(text=caption)` 走 Qwen text embedding 即可。**家用 95% 场景召回精度足够**(详见 §12 多模态记忆决策)。

### 7.1 模块结构

```
smartbutler/
├── voice/capture/buffer.py               # 🆕 MultiTrackSyncBuffer(音视频帧对齐)
├── perception/                           # 🆕 fork Miloco perception 子目录
│   ├── __init__.py
│   ├── gate/                             # 帧差分 + VAD 门控
│   │   ├── __init__.py
│   │   └── frame_diff.py                 # 帧差分(过滤静止窗口)
│   ├── identity/                         # 人物识别(DeepSORT + ReID)
│   │   ├── __init__.py
│   │   └── deepsort.py                   # 跟踪 + tier_a/c/u 样本库
│   ├── omni/                             # VLM 抽 caption
│   │   ├── __init__.py
│   │   └── vlm_caption.py                # 调用 Qwen2-VL / InternVL(本地或云)
│   └── buffer/                           # 音视频同步缓冲
│       ├── __init__.py
│       └── sync_buffer.py
└── events/
    └── adapters/
        └── camera_adapter.py             # 🆕 摄像头 → 感知 → DeviceEvent → 记忆
```

### 7.2 CameraAdapter 主流程(参考 Miloco 感知流水线 + 文本化进记忆)

```
摄像头(RTSP / USB / 米家 SDK)
  → MultimodalCollector(帧缓冲,3-5s window)
  → Gate(帧差分,过滤静止窗口)
  → Identity(DeepSORT 跟踪 + ReID → tier_a/c/u 样本库,熟人 / 半熟 / 陌生)
  → Omni(VLM,接收 3-5s 视频片段 → 输出结构化文本)
  → caption = "女主人穿蓝色长款雪纺裙在玄关换鞋出门"
  → matched_rules = ["家规-晚10点后回家小声"]
  → speeches = "主人这么晚还要出门吗?"
  → suggestions = ["锁好门窗", "把灯调暗"]
  → 构造 DeviceEvent(
      device_type=CAMERA, action="perception",
      payload={caption, person_id, matched_rules, speeches, suggestions,
               scene_tags=["蓝","长款","雪纺"],      ← Omni 抽的结构化标签
               image_ref="/data/butler/images/2026/10/14/xxx.jpg"  ← 7.8 评估是否存档
              }
    )
  → publish 到 EventBus

进 EventBus 后(双轨):
  轨道 1(管家回答):
    Trigger.handle → Proactive tick → 管家 LLM 看到 payload → 可能主动播报

  轨道 2(进记忆,本节核心):  🆕 必须
    MemoryFacade.remember(
        text=caption,    ← Omni 输出是结构化文本,直接走 Qwen text embedding
        source="perception",
        metadata={
          person_id, location, scene_tags, image_ref,
          event_id, timestamp
        }
    )
    → 下次 recall("女主人最近穿的蓝裙子") → 命中
    → 下次 recall("领带扔哪了") → Identity 识别"领带" + spatial_tag → 命中
```

**实现策略**:
- **直接 fork Miloco `perception/` 子目录**到 `smartbutler/perception/` 命名空间
- 复用 Miloco 的 Gate / Identity / Omni 实现
- 适配器层(原 Miloco 的 `CameraDeviceAdapter`)改成 `smartbutler.events.adapters.camera_adapter.CameraAdapter`
- 重点:VoiceEvent 与 DeviceEvent 协议已对齐 Miloco → **几乎纯移植**

### 7.3 端到端测试(7.8 验收)

`tests/perception/test_camera_adapter_e2e.py`:
- **case 1**:摄像头 mock → 感知流水线 mock → 1 个 `DeviceEvent(perception)` 发布
- **case 2**:Gate 判定"静止" → 0 个 DeviceEvent 发布
- **case 3**:Identity 匹配 tier_a 样本 → payload 含 `person_id`
- **case 4**:Omni 命中规则 → payload 含 `matched_rules`
- **case 5**:Omni 返空 suggestions → 管家沉默(沿用不变量 #5)
- **case 6(🆕 感知层文本化进记忆)**:`MemoryFacade.remember` 被调用,参数 `text=caption`、`source="perception"`、`metadata={person_id, scene_tags, image_ref}` 正确——**这是 7.8 验收的硬条件**
- **case 7(🆕 召回验证)**:模拟 1 周后"女主人问最近穿的蓝裙子" → `MemoryFacade.recall("蓝裙子")` 命中 7.8 写入的 perception fact

### 7.4 7.8 工作量

| 任务 | 行数 | 4-6 周节奏 |
|------|------|-----------|
| `voice/capture/buffer.py` | ~150 行 | Day 1-3 |
| Miloco `perception/` 移植 + 命名空间适配 | ~500 行 | Day 4-15 |
| `events/adapters/camera_adapter.py`(含 `MemoryFacade.remember` 接入)| ~180 行 | Day 16-18 |
| `tests/perception/test_camera_adapter_e2e.py`(7 case,含文本化进记忆)| ~280 行 | Day 19-22 |
| 调通 + 文档 | — | Day 23-25 |

**最坏情况 4-6 周**——纯移植工作量 + 集成测试,**优先级低于 7.6+7.7**,在 PoC 跑通后择期启动。

**注意**:7.8 **不上**多模态 embedding——若 3-6 个月后 case 7 类召回< 70%,启动 7.9 EmbeddingGemma 2 增强(见 §12)。

---

## 8. 配置矩阵(Phase 7 全周期)

### 8.1 `smartbutler/config/server.py`(7.4 新增)

```python
class ServerConfigSettings(BaseSettings):
    """Server 子模块配置(沿用 Pydantic Settings 子模块化)。"""
    model_config = SettingsConfigDict(
        env_prefix="SMARTBUTLER_SERVER_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    enable_event_bus: bool = True
    enable_normalizer: bool = True       # 7.4 新增
    enable_trigger: bool = True          # 7.4 新增
    enable_answer_router: bool = True    # 7.5 新增
    trigger_max_per_minute: int = 20     # 7.4 新增
    normalizer_window_sec: float = 5.0   # 7.4 新增
    normalizer_max_size: int = 10000     # 7.4 新增
```

### 8.2 `smartbutler/config/voice.py`(7.6 新增)

```python
class VoiceConfigSettings(BaseSettings):
    """voice 子模块配置(7.6 新增)。"""
    model_config = SettingsConfigDict(
        env_prefix="SMARTBUTLER_VOICE_",
        env_file=".env",
        extra="ignore",
    )

    sample_rate: int = 16000
    channels: int = 1
    frame_duration_ms: int = 30
    vad_threshold: float = 0.5
    asr_model: str = "paraformer-zh"
    asr_device: str = "cpu"
    wake_word_enabled: bool = False
    wake_word: str = "杰贝兹"
    default_mic_device: str = "laptop_builtin"
    asr_silence_timeout_ms: int = 1500  # 1.5s 静音 → 句子结束
```

### 8.3 `smartbutler/config/tts.py`(7.7 新增)

```python
class TTSConfigSettings(BaseSettings):
    """TTS 子模块配置(7.7 新增)。"""
    model_config = SettingsConfigDict(
        env_prefix="SMARTBUTLER_TTS_",
        env_file=".env",
        extra="ignore",
    )

    default_engine: str = "edge"          # "edge" / "piper"
    edge_voice: str = "zh-CN-XiaoxiaoNeural"
    piper_model_path: str = ""            # 7.7.1
    piper_config_path: str = ""           # 7.7.1
```

---

## 9. 依赖矩阵(Phase 7 全周期)

| 子阶段 | 新增依赖 | pip 大小 | 测试依赖 |
|--------|----------|----------|----------|
| **7.4** | — | 0 | pytest-asyncio(已有) |
| **7.5** | — | 0 | pytest-asyncio(已有) |
| **7.6** | `sounddevice` + `funasr` + `torch` | ~1.2GB(含 torch) | pytest-asyncio + numpy mock |
| **7.6.1** | `pvporcupine`(可选唤醒词) | ~30MB | — |
| **7.7** | `edge-tts` + `pygame`(本机播放) | ~10MB | pytest-asyncio + BaseTTS mock |
| **7.7.1** | `sherpa-onnx` + Piper 模型 | ~80MB | — |
| **7.8** | `opencv-python` + `torch` + `transformers`(VLM) | ~3GB | pytest-asyncio + 各组件 mock |

**注意**:7.6 引入 `torch` 是大依赖(~1GB),但**只在 `smartbutler.voice` 子模块导入**——`smartbutler.thinking` / `smartbutler.emotion` 仍保持轻量。**开发依赖**写在 `pyproject.toml [project.optional-dependencies.voice]`,**默认不装**。

---

## 10. 关键变更记录(本方案)

| 日期 | 变更 | 关联 |
|------|------|------|
| 2026-10-14 | 🆕 Phase 7 技术方案落地(本文档):盘点真实状态(7.0+7.1+7.2+7.3 已落库) + 拆 7.4-7.8 子阶段 + 7.4 详细设计(Normalizer+Trigger) + 7.5 详细设计(AnswerRouter+Presence) + 7.6 详细设计(VoiceAdapter) + 7.7 详细设计(TTS) + 7.8 详细设计(Camera 简版) | `docs/PHASE_7_TECHNICAL_DESIGN.md` |
| 2026-10-14 | 🆕 **多模态记忆决策敲定**:**7.6/7.8 感知层采集音视频,记忆层统一转文本(Qwen text embedding)**,**不上**多模态 embedding;7.9 EmbeddingGemma 2 标为"未来观察项,不启动",触发条件见 §12.3;§1.1 架构图 + §1.2 数据流 + §1.3 不变量 #11-#13 + §7 7.8 详细设计同步加"感知层文本化进记忆"硬条件(case 6/7) + 7.8 验收门槛 + 新增 §12 多模态记忆决策全文 | `docs/PHASE_7_TECHNICAL_DESIGN.md §12` |

---

## 11. 与其他文档的关系

| 文档 | 关系 |
|------|------|
| `smartbutler/events/ADR-008-event-driven.md` | Phase 7 顶层决策(事件驱动 / Redis Streams / parent_agent 协议)— 本方案**部分沿用**(协议稳定)+ **部分修正**(Redis 升级留 Phase 8+ 多节点) |
| `smartbutler/thinking/ADR-009-proactive-reactive-dual-loop.md` | 双循环架构 — 本方案 Trigger 的路由规则完全沿用 ADR-009 §5.9.3 |
| `README.md §2` 阶段状态表 | 7.0+7.1+7.2+7.3 状态从 🟡 改 ✅,7.4-7.8 状态填入,**新增 7.9 观察项** |
| `README.md §2.7` 待办列表 | 7.4 替代原"Phase 7 真实实现"待办,7.5-7.8 追加,**7.9 不进待办**(观察项) |
| `README.md §10` 关键变更记录 | 本方案落地后追加 v0.4.0 记录 + 多模态决策记录 |
| `smartbutler/events/README.md` | 本方案落地后更新已实现模块清单 |

---

## 12. 多模态记忆决策(本轮敲定,2026-10-14)

> **本节是 7.6/7.8 记忆层"为什么不上多模态 embedding"的决策记录**——给未来 3-6 个月后的自己一份"当初为什么这么做"的依据。

### 12.1 决策结论

**7.6(VoiceAdapter)+ 7.8(CameraAdapter)感知层采集音视频,但记忆层统一先转文本进 MemoryFacade**——`text + QwenEmbedder`(1024 维)是默认。

- **VoiceAdapter(7.6)**:`asr_text` 直接进 `MemoryFacade.remember(text=asr_text)`
- **CameraAdapter(7.8)**:`Omni VLM 输出的 caption` 直接进 `MemoryFacade.remember(text=caption, metadata={scene_tags, image_ref, person_id, location})`

**当前不引入**多模态 embedding(CLIP / ImageBind / **EmbeddingGemma 2**)。**`ReactiveSubmission.metadata: dict[str, Any]` 和 `ButlerReply.metadata: dict[str, Any]` 预留扩展位**。

### 12.2 决策依据

#### 12.2.1 家用 95% 召回需求文本 embedding 足够

| 场景 | 文本 embedding 能召回? | 多模态 embedding 必要? | 触发频率 |
|------|---------------------|---------------------|---------|
| "今天周几"/"今天天气"| ✅ 完全够 | ❌ | 每日多次 |
| "昨天那条蓝裙子比今天这条好看吗"| ✅ `caption` 含"蓝色"→ 召回 | ❌ | 月 1-3 次 |
| "领带扔哪了"| ✅ `caption` 含"领带"+"玄关"→ 召回 | ❌ | 月 1-2 次 |
| "上周来过的人"| ✅ `caption` 含"主人"+Identity 标签 → 召回 | ❌ | 周 1-2 次 |
| **以图搜图**("找到我和闺蜜去年夏天的合影")| ❌ 需要图本身 | ✅ **必要** | **年 1-5 次** |
| **图 vs 图**("这条裙子比那条好看吗")| ⚠️ 可用 caption 比对 | ✅ 更准 | 月 1-2 次 |

**结论**:**"以图搜图 / 图 vs 图"家用年触发 1-5 次**——不值得为此引入 1GB+ 依赖。

#### 12.2.2 Omni VLM 抽事实是高质量的语义压缩

**Miloco 思路沿用**:Omni VLM 接收 3-5s 视频片段,输出**结构化文本**:
```
caption = "女主人穿蓝色长款雪纺裙在玄关换鞋出门"
matched_rules = ["家规-晚10点后回家小声"]
speeches = "主人这么晚还要出门吗?"
suggestions = ["锁好门窗", "把灯调暗"]
```

**这套文本已经包含**:
- **空间**("玄关")→ recall "玄关" 命中
- **时间**("出门")→ recall "出门" 命中
- **属性**("蓝色长款雪纺")→ recall "蓝色" / "长款" 命中
- **人物**("女主人",Identity 标签)→ recall "女主人" 命中
- **行为**("换鞋出门")→ recall "换鞋" 命中

**只要 Omni caption 抽到位(text embedding 走 Qwen,1024 维,精度高于 80%)**,**家用 95% 召回场景都能命中**。

#### 12.2.3 多模态 embedding 的成本

| 模型 | 内存(bf16)| 内存(int8)| MTEB 精度 | 适合家用? |
|------|---------|---------|----------|---------|
| **Qwen text-only**(默认)| 540MB | — | 61.36 | ✅ |
| **EmbeddingGemma 2**(740M 全模态)| 1.1GB | 567MB | 61.36 text / 64.64 image | ⚠️ 占用高 |
| **CLIP-ViT-L/14**(428M)| 856MB | 400MB | 66 image | ⚠️ 占用高 |
| **SigLIP-SO400M**(400M)| 800MB | 400MB | 70 image | ⚠️ 占用高 |

**家用 16GB RAM 笔记本**:
- 已有 torch(~1GB,Phase 7.6 VAD 引入)
- 加 1.1GB EmbeddingGemma 2 = 2.1GB → 占总 RAM 13%
- **可接受,但召回延迟 +50-200ms**(CPU 推理)

**家用 8GB RAM 笔记本**:
- 已有 torch 1GB
- 加 1.1GB EmbeddingGemma 2 = 2.1GB → 占总 RAM **26%**
- **勉强,但 CPU 推理慢**(50ms → 200ms 召回延迟,管家"想"完要等 1s)

**家用 4GB RAM**(老 Mac mini):
- torch + EmbeddingGemma 2 = **2.1GB > 50% 内存**
- **不可用**

#### 12.2.4 "以图搜图"扩展位已经预留

- `ReactiveSubmission.metadata: dict[str, Any]` 已有(沿用 `server/adapters/__init__.py:42-55`)
- `ButlerReply.metadata: dict[str, Any]` 已有
- **若未来 7.9 启动**,新增 `image_embedding: list[float] | None` 字段直接进 metadata,**不破坏现有协议**
- `LongTermStore` 加 `image_embedding` 列(独立 collection `image_vectors`),**只加列不加表,零迁移成本**

### 12.3 7.9 启动条件(任一满足才考虑)

| 触发条件 | 阈值 | 监控方式 |
|---------|------|---------|
| **以图搜图场景频次** | 用户 3 个月内**主动要求"以图搜图"** ≥ 5 次 | `MemoryFacade.recall` 加 `query_type=image` 字段,统计 |
| **Omni caption 召回命中率** | 7.8 跑通后 3 个月,`recall("蓝裙子")` 命中率 < 70% | `tests/perception/test_recall_hit_rate.py`(7.8.1 补) |
| **本地多模态 LLM 成熟** | Qwen2-VL-7B / InternVL-2B **能稳定本地跑**(< 5s 推理) | LLM 资源占用监控 |
| **Phase 8+ 多节点部署** | 中心化机器(32GB+ RAM)+ 4 张 GPU 卡 | Phase 8 启动信号 |

**任一**满足 → 启动 7.9 EmbeddingGemma 2 评估,做小流量 A/B(默认走 Qwen text-only / 1% 流量走 EmbeddingGemma 2 召回),**确认召回率提升 ≥ 10% 才全量**。

### 12.4 7.9 工作量(预备,等启动再做)

| 任务 | 行数 | 备注 |
|------|------|------|
| `capabilities/embedding/gemma2.py` | ~150 行 | 封装 `SentenceTransformer("google/embeddinggemma-2")` + MRL 截断 + 懒加载 |
| `factory.py` 加 gemma2 选项 | ~30 行 | 渐进加载(text-only 540MB / +vision 880MB / +audio 1.1GB) |
| `LongTermStore` 多模态索引 | ~50 行 | 新增 `image_embedding: list[float] | None` 字段 |
| `MemoryFacade.remember` 多模态参数 | ~30 行 | `remember(text=..., image=..., audio=...)` 同时存 text + image embedding |
| `pyproject.toml` 依赖 | — | `sentence-transformers`(torch 已存在) |
| `tests/unit/test_gemma2_embedder.py` | ~120 行 | MRL 截断 / 跨模态相似度 / 懒加载 |
| **总计** | **~380 行 + ~120 测试** | 1 周节奏,3 个月内未必启动 |

### 12.5 决策可逆性

**本决策完全可逆**——7.9 启动时:
1. `LongTermStore` 加 `image_embedding` 列 → `ALTER TABLE` 一条 SQL,零数据迁移
2. `MemoryFacade.remember()` 加 `image=` 参数 → **新参数不影响老调用**(默认 None 走原逻辑)
3. `ReactiveSubmission.metadata` 加字段 → **现有调用零修改**
4. 切换 provider:`SMARTBUTLER_EMBEDDING_PROVIDER=qwen → gemma2` 一行 env
5. **业务层零修改**——`thinking/loop/orchestrator.py` 只看 `MemoryFacade` Protocol,不知道底下是哪个 embedder

**总结**:**今天不上**多模态 embedding 是"延迟决策",**不是"不做决策"**——3-6 个月后看真实数据再决定。
