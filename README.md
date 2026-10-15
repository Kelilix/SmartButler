# SmartButler

> 有性格、有记忆、懂上下文的家用智能管家。
>
> **当前版本**:`v0.5.2`(`10` 个 commit;`886 unit + 5 integration + 24 e2e` 全部 ✅,原 851 → 886,+35 新测试——29 声纹协议 + 6 orchestrator speakers 透传)。**无新功能破坏性改动**——单 user 设计,向后兼容,7.6 声纹模型留协议占位。
> **下一站**:Phase 7.6(VoiceAdapter 第一个真硬件,silero VAD / funasr ASR / porcupine 唤醒词,见 `docs/PHASE_7_TECHNICAL_DESIGN.md` §5)。
> **详细方案**:
> - `docs/PHASE_6.2-6.5_TECHNICAL_DESIGN.md`(Memory 4 个子阶段设计)
> - `docs/PHASE_7_TECHNICAL_DESIGN.md`(Phase 7 全周期 7.0-7.8 设计 + **§12 多模态记忆决策** + 7.9 观察项)
> - `docs/PHASE_8_EMOTION_GOVERNANCE.md`(🆕 **Phase 8 重定位:情绪治理引擎**,替代原 Sub-Agent 主题,见 §10)
> **架构总图**:`docs/TECHNICAL_DESIGN.md`(若不存在,见 git 历史 commit `828337e`)。
> **关联 ADR**:`smartbutler/events/ADR-008-event-driven.md`(事件驱动)+ `smartbutler/thinking/ADR-009-proactive-reactive-dual-loop.md`(双循环)

---

## 1. 这是什么

SmartButler 是部署在家庭里、和主人长期相处的**单一智能体实例**。它不抢"通用 Agent 框架"的赛道,只做一件事——把"管家"这个角色做得**像真人**。

| 维度 | 我们怎么实现 |
|------|--------------|
| **有性格** | 静态默认 traits(Phase 6.6)+ 用户显式指令("你更轻松点"立即改 trait) + 反馈学习(Phase 6.7 演化) |
| **有记忆** | **4 层分片**(L0 短期 raw / L1 日 / L2 周 / L3 月 / L4 永久)+ **3 类存储**(短期=LangGraph `SqliteSaver` / 长期事实=Qdrant 嵌入式 `path=` / 长期情景=SQLite JSON)+ **5 类遗忘** + **高级 API** = `MemoryFacade`(`remember` / `recall` / `format_for_prompt` / `consolidate` / `forget` / `restore`) |
| **懂上下文** | thinking 链(`ButlerOrchestrator`)每次 ainvoke 调 `format_for_prompt` 召回 5 条相关历史 → 注入 system prompt → LLM 看到"用户之前说过 X" |
| **能主动开口** | 双循环架构(Reactive + Proactive,ADR-009):URGENT 事件必主动;5 分钟 dedup;沉默默认;LLM 故障降级到 `RuleBasedProactiveReasoning` |
| **能扩展** | 2 路并行——**Sub-Agent**(代码,廉价模型,状态机) vs **Skill**(Anthropic `SKILL.md` 格式,管家强模型自执行,亲属可拖入) |

---

## 2. 当前进度

> 实施阶段对应设计文档 §1.4。每完成一阶段回来更新本节(commit 一起提交)。

### 2.1 阶段状态

| 阶段 | 模块 | 状态 | 主要交付 |
|------|------|------|----------|
| **Phase 1** | 基础设施 + LLM | ✅ | Pydantic Settings / structlog / `BaseStorage` / `BaseLLM`(**双后端** `http`+`langchain`) |
| **Phase 2** | Tool 能力层 | ✅ | `BaseTool` + `ToolRegistry` + Decorator + LangChain Adapter + 2 个 common tool |
| **Phase 3** | Sub-Agent 层(框架) | 🟡 **仅脚手架** | `BaseAgent` + `AgentManager` + `TestTimeAgent`(脚手架,集成测试用)。**领域 Sub-Agent(`HomeAgent` / `ScheduleAgent` / `SearchAgent`)推 Phase 10+**(用户 2026-10-15 决策 F:整体往后) |
| **Phase 4** | LangGraph Loop + Supervisor | ✅ | `StateGraph` + `ToolNode` + Checkpointer + `ButlerPromptBuilder` |
| **Phase 4b** | ProactiveLoop 框架 | ✅ | 双循环架构 + `RuleBasedProactiveReasoning` 降级后备 + 沉默默认 |
| **Phase 5** | Skill loader | ✅ | `SKILL.md` → 扫描解析 → 注入 system prompt + 6 个文件工具 + 3 类权限 |
| **Phase 6.1** | Memory:长短期落地 | ✅ | `SQLiteStorage` + `QdrantStorage`(嵌入式默认) + `ShortTermMemory`(`SqliteSaver`) + `LongTermStore` |
| **Phase 6.2 P0** | Memory:thinking 接入 + 高级 API | ✅ | `capabilities/embedding/`(Qwen + Stub)+ `capabilities/langmem/`(v0.0.30 + 正则 fallback)+ `MemoryFacade` + `MemoryHooks` + 注入 `memory_block` 到 system_prompt + **e2e 10 用例真打通** |
| **Phase 6.3** | Memory:4 层分片 + 固化 + 遗忘 + vacuum | ⏭ **往后推**(Phase 9+ 治理优化再做) | `consolidation.py`(L1/L2/L3 升格管道 + LLM 评分)+ `forgetting.py`(5 类遗忘 + 软删除)+ `levels.py`(4 层 tag)+ `summarizer.py`(5W1H 提炼)+ `task_boundary.py` + `vacuum.py`(双时间窗 7+30 天) + `archive_store.py` + `scripts/memory_cli.py`(手动 CLI)。详见 `docs/PHASE_6.2-6.5_TECHNICAL_DESIGN.md` §2.5.1(含 §2.5.1.16)。**为什么往后(A 决策)**:vacuum/遗忘是"工程治理优化",不阻塞护城河;优先做情绪治理(Phase 8)+ 7.6/7.7/7.8。**触发条件**:Phase 8 跑通 + 用户产生"记太多/记错"反馈 |
| **Phase 6.4** | Memory:episodic + 跨层 rollup | ⏭ **往后推**(Phase 9+) | `summarizer.py`(5W1H 提炼,已在 6.3)+ `episodic.py`(情景记忆,SQLite JSON)。**为什么往后(B 决策)**:情景记忆是辅助型,不是核心护城河 |
| **Phase 6.5** | Memory:habit + cross-thread | ⏭ **往后推**(Phase 9+) | `habit.py` + `cross_thread.py`(为 Phase 6b 留 hook)。**为什么往后(E 决策)**:用户决策 2026-10-15——习惯/跨线程也是工程优化,优先做情绪治理。**注意**:Phase 8 情绪治理会复用"成功模式归档"(bp-emotion-recovery)——6.5 不阻塞 8.x |
| **Phase 6.6** | Personality | ✅ | `emotion/personality/traits.py` + `loader.py`(YAML frontmatter md)+ `injector.py`(渲染 system prompt 片段);`ButlerPromptBuilder` 加 `personality_block` 参数;`ButlerOrchestrator.reload_personality()` 接 Learn 演化。详见 `README §2.7` 注释 |
| **Phase 6.7** | Learn | ⏭ **往后推**(Phase 9+) | 反馈事件流 + trait 映射 + 触发 `Personality.adjust_trait()`。**为什么往后(C 决策)**:Learn 是"反馈通道",不阻塞情绪治理(Phase 8 不依赖 Learn) |
| **Phase 6b** | ProactiveLoop 真实化 | ⏭ **往后推**(Phase 9+) | `LLMProactiveReasoning` 接 Memory + Persona + LLM 推理;`RuleBasedProactiveReasoning` **永不被删**(安全网)。**为什么往后**:Proactive 真实化是"主动智能"增强,Phase 8 情绪治理本身已包含主动干预(更聚焦,更受控) |
| **Phase 7** | 事件驱动 + 路由 | 🟡 **当前在做(G)** | **已落地**:7.0 协议 + 7.1 InMemoryEventBus/Server/Adapter + 7.2 ButlerReply 结构化 + 7.3 内存总线 + 7.4 Normalizer+Trigger + **7.5 AnswerRouter+PresenceService ✅**。**D 决策 = 等于已做**:总线 + 输入输出适配器 7.4+7.5 已完整落库,7.6 之后才是真硬件对接。**下一站 7.6**:VoiceAdapter 第一个真硬件。**后续**:7.7 TTS / 7.8 CameraAdapter / **7.9 多模态记忆(EmbeddingGemma 2,观察项,不启动)** |
| **Phase 8** | 🆕 **情绪治理引擎(Emotion Governance Engine)** | ⏳ 下一阶段(7.6/7.7/7.8 完工后启动) | 6 模块:`8.1 EmotionMonitor`(实时负面因子计算)/ `8.2 InterventionPlanner`(反向追溯根因 + LLM 设计干预方案)/ `8.3 InterventionExecutor`(调 7.5/7.7 链路执行 TTS/设备调整)/ `8.4 RecoveryTracker`(7 天观察 + 48h 正因子持续 → 判定恢复)/ `8.5 DataPruner`(ACTIVE → RESOLVED 状态机,不真删)/ `8.6 PatternArchive`(成功案例归档为可复用 pattern)。**核心范式**:看到事实 → 持久化为关系(带 sentiment)→ 派生行为模式 → **主动治理情绪** → 持续追踪 → 标记清除 → 学习闭环。**伦理护栏**:数据本地 / voice_tts 默认关 / 管家不是心理医生(toxic 情绪自动升级"建议专业帮助")/ 静默观察默认。**详细设计**:`docs/PHASE_8_EMOTION_GOVERNANCE.md`(6 周,~1350 行 + ~370 测试) |
| **Phase 9** | 治理优化回填 + 多节点 Redis Streams | ⏳ | (1)回填**真空期优化的 5 块**:6.3 vacuum/遗忘(1060 行)+ 6.4 episodic(150 行)+ 6.5 habit(150 行)+ 6.7 Learn(200 行)+ 6b LLMProactiveReasoning(2-3 周)。(2)**多节点 Redis Streams 部署**(原 ADR-008 残留,顺延到 Phase 9+) |
| **Phase 10+** | 原 Sub-Agent 主题 | ⏳ | F 决策整体往后:`HomeAgent`(HomeAssistant)/ `ScheduleAgent` / `SearchAgent` 等。**触发条件**:Phase 8 情绪治理 + 6.5 habit 跑通后,产生"管家需要专业子能力"反馈 |
| **Phase 11+** | self-evolving(候选) | ⏳ | skill 自动生成;当前**严格不做**——Learn 只管"反馈通道" |

### 2.2 Phase 6 子阶段拆分(Memory → Personality → Learn)

> **2026-10-15 更新(用户决策 A-F-G)**:严格串行已**松绑**——6.3/6.4/6.5/6.7/6b 整体推 Phase 9+ 治理优化期;**Memory 6.1+6.2 P0 已落库**(继续夯实)→ **Personality 6.6 已落地** → **Learn 6.7 推 Phase 9+**。**Phase 8 情绪治理是当前核心**,依附于 6.1+6.2 P0 Memory,本身不依赖 6.3-6.7。
> **新节奏**:**Memory(P0 已落库,P1 优化往后)** + **Personality(6.6 已落库)** + **Phase 8 情绪治理(下一阶段)** + **Phase 9 治理优化回填** + **Phase 10+ Sub-Agent 主题**。

| 子阶段 | 模块 | 自研量 | 关键依赖 | 启动前置 | 优先级 |
|--------|------|--------|----------|----------|--------|
| **6.1** Memory:长短期落地 | `storage/{sqlite,qdrant}.py` + `emotion/memory/{short_term,long_term}.py` | ~200 行 | `qdrant-client` + `langmem` | Phase 5 完工 ✅ | ✅ 完成 |
| **6.2 P0** Memory:thinking 接入 | `capabilities/embedding/` + `capabilities/langmem/` + `emotion/memory/{embedder,importance,retrieval,hooks,facade}.py` + `thinking/loop/` 注入 `memory_block` | ~630 行 + ~340 测试 | `qwen3.7-text-embedding-flash` + `langmem 0.0.30` | 6.1 ✅ | ✅ 完成 |
| **6.3 P1** Memory:4 层分片 + 固化 + 遗忘 + vacuum | `emotion/memory/{consolidation,forgetting,levels,summarizer,task_boundary,vacuum,archive_store}.py` + `scripts/memory_cli.py` | **~1060 行 + ~40 测试 + 2 e2e** | LLM 评分 + 5W1H 提炼 + 4 层 tag + 双时间窗 vacuum(7+30 天) | 6.2 ✅ | ⏭ **A 决策:Phase 9+ 治理优化** |
| **6.4 P1** Memory:episodic + 跨层 rollup | `emotion/memory/{episodic,hierarchical_rollup}.py` | ~150 行 + ~10 测试 | 6.3 已有 4 层分片,6.4 只补对话轨迹 | 6.3 ✅ | ⏭ **B 决策:Phase 9+** |
| **6.5 P1** Memory:能力补全 | `emotion/memory/{habit,cross_thread}.py` | ~150 行 | 6.4 的 summarizer | 6.4 ✅ | ⏭ **E 决策:Phase 9+** |
| **6.6** Personality | `emotion/personality/{traits,loader,injector}.py` | **~350 行 + ~77 测试** | **不依赖 6.5**——空壳 personality 独立可测 | 6.5 ✅(实际 6.1 即可并行) | ✅ 完成 |
| **6.7** Learn | `emotion/learn/{feedback_log,signal,evolution_hook}.py` | ~200 行 | 6.6 `personality.adjust_trait()` | 6.6 ✅ | ⏭ **C 决策:Phase 9+** |

### 2.3 关键不变量(Phase 6 全程必须遵守)

1. **3 层存储严格分离**——短期 = `SqliteSaver`(checkpointer)/ 长期事实 = Qdrant / 长期情景 = SQLite JSON。**不允许把"短期"也存 Qdrant**。
2. **Qdrant 默认嵌入式**(`path=data/qdrant`),**用户不感知**。Phase 9+ 切服务模式时只改 `StorageSettings.qdrant_url` 即可(多节点 Redis Streams 部署时统一切)。
3. **Personality 永远不直接调 LLM**——只接受显式 user 指令 + learn 触发的 trait 调整。**Personality 是数据,不是 Agent**。
4. **Learn 严格不"自动生成 skill"**——skill 生成是 self-evolving agent 范畴,属 Phase 12+ 候选,Phase 6 只管"反馈通道"。

### 2.4 已知风险(本阶段要盯)

- **R1 langmem 装不上**:`pip install` 失败 → `capabilities/langmem/_fallback.py` 正则抽事实接管,业务层无感知。
- **R2 Qdrant 嵌入式性能**:Windows fd 泄漏报告(issue #1234) → 改 `SMARTBUTLER_STORAGE_QDRANT_URL=http://localhost:6333` + docker 跑。
- **R3 QwenEmbedder 限流/不可用**:Qwen 429/401/超时 → `StubEmbedder`(hash → 1024 维)接管,recall 精度降级但管家不崩。
- **R4 consolidation 升格过快/过慢**(待 6.3):重要性分级([0.0, 0.2)/[0.2, 0.5)/[0.5, 0.8)/[0.8, 1.0] 4 档)容易把琐事升 L1 / 关键约定漏升 L4——6.3 单测必须 4 档各 ≥ 5 case + importance 重评分用例 ≥ 3 case。

### 2.4.1 Phase 6.6 关键决策(2026-10-13)

- **Personality 纯自研,不用 LangChain**——LangChain 没有"personality"模块,只有 prompt 模板工具,自研 ~350 行更可控。
- **数据/行为分层**:Personality 是**数据**(Pydantic + trait 数值),不是 Agent;Learn(6.7)才是**行为**(触发 trait 调整)。
- **5 个内置 trait**:`formality` / `humor` / `verbosity` / `warmth` / `proactiveness`,值域 [0, 1],Pydantic Field + 防御性 validator 双重约束。
- **配置文件用 md 不用 YAML**:`personality.md` 走 YAML frontmatter(跟 SKILL.md 格式一致)+ body 自由注释;加载失败/缺字段全部降级到默认 Personality,管家不崩。
- **三档翻译**:trait 数值 → low/mid/high → 中文档位描述("严肃认真"/"偶尔轻松"/"幽默风趣"),让 LLM 看到的是自然语言不是裸数字。
- **不变量维持**:Personality 严格不调 LLM(不变量 #3);`Personality.adjust_trait()` 是数据 mutator,Learn 阶段调它+`orchestrator.reload_personality()` 刷缓存。

### 2.5 已交付的代码模块

| 模块 | 路径 | 状态 | 说明 |
|------|------|------|------|
| 配置 | `smartbutler/config/` | ✅ | Pydantic Settings 子模块化(`LLM`/`Storage`/`Logging`/`Agent`/`Skills`/`Embedding`) |
| 日志 | `smartbutler/utils/logging.py` | ✅ | structlog 结构化日志 |
| 存储接口 | `smartbutler/storage/` | ✅ | `BaseStorage` Protocol + `SQLiteStorage`(aiosqlite 异步)+ `QdrantStorage`(嵌入式 `path=` 默认,服务模式 `url=` 切换) + `RedisBackend`(可选) |
| LLM 能力 | `smartbutler/capabilities/llm/` | ✅ | `BaseLLM` 抽象 + `OpenAICompatibleLLM`(httpx 直调,默认)+ `LangChainLLMAdapter`(env 切 `backend=langchain`)+ `ButlerChatModelAdapter`(→ LangGraph) |
| Embedding 能力 | `smartbutler/capabilities/embedding/` | ✅ | `Embedder` Protocol + `QwenEmbedder`(`qwen3.7-text-embedding-flash` 1024 维)+ `StubEmbedder`(降级,无 API) + 工厂自动降级 |
| langmem 抽象 | `smartbutler/capabilities/langmem/` | ✅ | `BaseLangmemAdapter` + `Langmem0030Adapter`(软依赖 try-import)+ `RegexFactExtractor`(装不上时降级)+ 工厂自动选 |
| Tool 能力 | `smartbutler/capabilities/tools/` | ✅ | `BaseTool` + `ToolRegistry` + Decorator + LangChain Adapter + 2 个 common tool(`get_current_time` / `web_fetch`) + 6 个 skill 文件工具 |
| Sub-Agent 框架 | `smartbutler/agents/` | 🟡 **仅脚手架** | `BaseAgent` + `AgentManager` + `to_langchain_tool()` + 最小 LLM 循环(决定 → 调 tool → 收集,最多 5 轮) + 失败回流 + 重试/超时。**`TestTimeAgent` 是脚手架 agent**(名字里的 "Test" 表示测试脚手架,见 `pyproject.toml` 注释),仅供集成测试验证 agent 协议,**不解决任何领域问题**。**真正的领域 Sub-Agent(`HomeAgent` / `ScheduleAgent` / `SearchAgent`)一个都还没做**——F 决策整体推 Phase 10+ |
| Thinking(Reactive) | `smartbutler/thinking/loop/` | ✅ | `ButlerOrchestrator`(LangGraph `StateGraph` + 原生 `ToolNode` + 异步 `AsyncSqliteSaver` checkpointer)+ `decide_node`(唯一业务节点)+ `ButlerPromptBuilder`(系统 prompt + Skill 注入 + **memory_block 注入**)+ `ButlerChatModelAdapter` |
| Thinking(Proactive) | `smartbutler/thinking/proactive/` + `thinking/loop/proactive_loop.py` | ✅ 框架 | `RuleBasedProactiveReasoning`(降级后备,URGENT 主动 / 5min dedup / 其他沉默)+ `ProactiveDecision/Result` + 双循环架构。**真实化见 Phase 6b** |
| 事件总线骨架 | `smartbutler/events/` | 🟡 协议+抽象落库,Server/内存总线落库,中间环节(Normalizer/Trigger/Router/Adapter 实现)待补 | `EventBus` + 设备/语音/定时器事件协议 + `EventNormalizer`(接口)+ `EventTrigger`(接口)+ `AnswerRouter`(接口)+ `PresenceService`(接口)。**7.0 + 7.1 + 7.2 + 7.3 + 7.4 + 7.5 全部 ✅**——`InMemoryEventBus` / `ButlerServer` / `ReactiveSubmission` / `ProactiveSubmission` / `ButlerReply` / `InMemoryInputAdapter` / `InMemoryOutputAdapter` / `PrintOutputAdapter` / `build_default_server` / `DedupNormalizer` / `DefaultEventTrigger` / `ServerConfig(normalizer/trigger/answer_router 字段)` / **`RouteResult`(frozen dataclass)** / **`DeviceRegistry`** / **`ConsoleAnswerRouter`** / **`SimplePresenceService`** 全部实现。**0 行实现的**:VoiceAdapter / CameraAdapter / TTSOutputAdapter(7.5b PresenceService 智能门锁 / 7.5c 手机 GPS / 7.5d 摄像头人脸 后续阶段待 7.5+ 启动)。**详细拆分**: `docs/PHASE_7_TECHNICAL_DESIGN.md` §0.1/§0.2/§4 |
| 情感层 - Memory | `smartbutler/emotion/memory/` | ✅ 6.1+6.2 P0 | `ShortTermMemory`(`AsyncSqliteSaver` 持久化 + 降级 `InMemorySaver`)+ `LongTermStore`(接 `QdrantStorage` 向量检索 + range query 服务端过滤)+ **`MemoryFacade`**(thinking 唯一入口 `remember`/`recall`/`format_for_prompt`)+ `MemoryHooks`(4 事件 + 异常隔离)+ `MemoryRetriever`(Embedder + Qdrant + 时间衰减重排)+ `ImportanceScorer`(启发式,P0 不调 LLM) |
| 情感层 - Personality | `smartbutler/emotion/personality/` | ✅ 6.6 | `Personality`(Pydantic)+ `TraitDimension`([0,1] 约束 + 防御性夹紧)+ 5 个内置 trait(formality/humor/verbosity/warmth/proactiveness)+ `adjust_trait(delta/new_value, source)` + `replace_guidelines()` + `to_dict/from_dict`;`load_personality(path=None)` 找 `<cwd>/personality.md` → `~/.smartbutler/personality.md` → 默认值(降级闭合);`save_personality()` 写 YAML frontmatter;`render_persona_block()` 三档翻译(low/mid/high)→ system prompt 片段;**Personality 严格不调 LLM**(不变量 §2.3);Learn 阶段调 `Personality.adjust_trait()` + `orchestrator.reload_personality()` |
| 情感层 - Learn | `smartbutler/emotion/learn/` | ⏳ Phase 6.7 | 当前空目录,只占位 |
| Skill 运行时 | `smartbutler/thinking/skills/` | ✅ Phase 5 | `SkillRuntime` + `Scanner` + 6 个文件工具 + 3 类路径 × 3 类模式权限(`allow`/`deny`/`interrupt`)+ `prompt_renderer` + `skill_admin` |
| Skills 资源 | `smartbutler/skills/` | ✅ | 内置 skill 目录(`builtin/`)+ 用户级目录(`~/.smartbutler/skills/`,不存在静默跳过) |

### 2.6 测试统计(实测,2026-10-13)

| 类别 | 数量 | 启用方式 | 覆盖 |
|------|------|----------|------|
| **单元测试** | **851** ✅ 全过 | `pytest tests/unit`(默认全跑) | capabilities(llm/tools/embedding/langmem)+ agents + thinking(loop/proactive/skills)+ emotion(memory/personality)+ storage + config + logging + **server(e2e:EventBus 19 + AnswerRouter 30)** |
| **集成测试** | **5** | `pytest -m integration`(默认 skip) | TestTimeAgent 端到端(2)+ LLM 实时连通(3) |
| **E2E 测试** | **24** | `pytest -m e2e`(默认 skip) | LLM 流式连通(14)+ **memory + thinking 端到端 10**(Phase 6.2 P0 新增) |

> 数字实测命令:
> ```bash
> pytest tests/unit --collect-only        # → 773 tests collected
> pytest tests/integration --collect-only # → 5
> pytest tests/e2e --collect-only        # → 24
> ```
> 每次合入新 PR 后**务必重新跑一遍**更新本节。

### 2.7 下次开工的待办(按 phase 排序)

> **2026-10-15 优先级重排(用户决策 A-F-G)**:A=6.3 vacuum / B=6.4 episodic / C=6.7 Learn / D=总线+适配器(= 已做,7.4+7.5 完整落库)/ E=6.5 habit / F=子 Agent 全部往后。**G=Phase 7 当前在做**(7.6/7.7/7.8 仍按 7.6 → 7.7 → 7.8 顺序推)。**重点 = 记忆**(情绪治理引擎依附于此)。具体见 §2.1 阶段表 + §10 变更记录。

- [ ] **Phase 6.3 vacuum/遗忘(往后推,A 决策)**(~1060 行,~2-3 周)——`emotion/memory/levels.py`(4 层 tag 规则)+ `consolidation.py`(L1/L2/L3 升格管道 + LLM 评分)+ `forgetting.py`(5 类遗忘 + 软删除 + 恢复)+ `summarizer.py`(5W1H 提炼 + LLM 摘要)+ `task_boundary.py`(短期 task 切分)+ `vacuum.py`(双时间窗 7+30 天 vacuum + VacuumReport)+ `archive_store.py`(archived SQLite 备份表)+ `scripts/memory_cli.py`(手动 CLI 入口)+ facade 加 6 个新 API(`consolidate` / `forget` / `restore` / `list_deleted` / `purge_now` / `vacuum_stats`)。**核心交付**:① 管家说"上周你订了去北京的票"(L2 weekly 召回)② 30 天前的开灯琐事自动软删(TTL)③ `python -m smartbutler.scripts.memory_cli vacuum --dry-run` 手动清软删,容量稳态只多 400 条(3MB)。**详细设计**:`docs/PHASE_6.2-6.5_TECHNICAL_DESIGN.md` §2.5.1。**为什么往后**:vacuum 是"工程治理优化",不阻塞护城河;优先做情绪治理(Phase 8)+ 7.6/7.7/7.8。**触发条件**:Phase 8 跑通 + 真实家庭使用 1-3 个月 + 用户产生"记太多/记错"反馈。**Phase 9+ 再启**
- [ ] **Phase 6.7 Learn(往后推,C 决策)**(~200 行,~1 周)——显式/隐式反馈事件流 + 反馈→trait 映射规则 + 触发 `personality.adjust_trait()` + 调 `orchestrator.reload_personality()` 刷缓存。**严格不实现"自动生成 skill"**。**前置**:6.6 Personality ✅。**为什么不阻塞 Phase 8**:Phase 8 情绪治理的"成功案例归档"(8.6 PatternArchive)走的是 BehaviorPattern 持久化,跟 Learn 的 trait 演化是两条独立通路
- [ ] **Phase 6b:ProactiveLoop 真实化(往后推)**(~2-3 周,**必须等 Phase 6 完成后启动**)——`LLMProactiveReasoning` 接 Memory + Persona + LLM 推理;`RuleBasedProactiveReasoning` **永不被删**(安全网,URGENT 事件 LLM 不可用时也要能主动开口)。**为什么不阻塞 Phase 8**:Phase 8.3 InterventionExecutor 自带"主动干预"逻辑,更聚焦(治理情绪)且更受控(voice_tts 默认关 + 过度干预防护)
- [x] **Phase 7.4:Normalizer + Trigger 真实化**(2026-10-14 ✅ 实施完成,v0.4.2)— `events/core/normalizer_dedup.py`(基于 event_id + topic 去重,window 5s,LRU 10000)+ `events/core/trigger_default.py`(route + 限速 20/min + 兜底)+ `server/runtime.py` 改 `_on_bus_event` 走 Normalizer+Trigger + `server/adapters/stub.py` 加 `ServerConfig` 注入字段 + `tests/server/test_event_bus_e2e.py`(5 case:dedup/限速/异常兜底/source 路由)。**🆕 5 个钩子必加(7.9 多模态记忆预留,~40 行)**:① H1 `Embedder` Protocol 加 3 个多模态方法(默认 `NotImplementedError`)② H2 `LongTermStore` SQLite 加 4 列(`image_ref`/`audio_ref`/`image_embedding`/`embedding_model`,**Day 1 必做**)③ H3 `MemoryFacade.remember()` 加 2 个可选 kw-only(`image_ref`/`audio_ref`)④ H4 `BaseEvent.media_refs` 字段 ⑤ H5 `metadata["modality_tags"]` 协议注释。**详见 `docs/PHASE_7_TECHNICAL_DESIGN.md` §3 + §12.6**。**核心交付**:从 publish 烟雾事件 → 60 个事件去重为 1 个 → Trigger 路由到 ProactiveLoop → 推回 1 条告警文本 → OutputAdapter 收到。**不启动前置**:VoiceAdapter / CameraAdapter(它们 producer 端会爆管,必须先有 Normalizer)。
- [x] **Phase 7.5:AnswerRouter + PresenceService 真实化**(2026-10-14 ✅ 实施完成,v0.5.0,851/851 测试全过,原 821 → 851,+30 新测试)— `events/routing/route_result.py`(`RouteResult` frozen dataclass 5 字段 + `success` property + `__str__`,D1)+ `events/routing/device_registry.py`(`DeviceRegistry` register/get/all/first + `_device_id` 约定,D2)+ `events/routing/answer_router_console.py`(`ConsoleAnswerRouter` SILENT 不推 / NORMAL 走 PresenceService / URGENT 推全屋,D1+D2+D4)+ `events/routing/presence_simple.py`(`SimplePresenceService` in-memory dict + 便利方法 set_sync/clear,D3)+ `events/routing/answer_router.py`(`AnswerRouter.route()` 返 `RouteResult` 而非 `bool`)+ `server/runtime.py`(`_answer_router` 字段 + `set_answer_router()` + `set_presence_service()` + `_on_bus_event` 拿 answer 后走 AnswerRouter 选路,Reactive 链路保持老逻辑不动)+ `scripts/dev_chat.py`(`/presence set/get/clear/list` REPL 命令 + `--no-answer-router` 开关)+ `server/adapters/stub.py`(`InMemoryOutputAdapter` 加 `device_id` 可选参数,backward-compat)+ `tests/unit/server/test_answer_router_e2e.py`(30 case:6 e2e + RouteResult 5 + DeviceRegistry 7 + SimplePresenceService 5 + ConsoleAnswerRouter 直测 6 + 1 全场景)。**核心交付**:从 publish 事件 → AnswerRouter 选路 → 推对/全屋/不推,可观测(RouteResult 5 字段)。**总工作量 ~1020 行(超 415 估,因测试 580 行 vs 180 估)+ 1 周**。**前置**:7.4 ✅
- [ ] **Phase 7.6:VoiceAdapter 第一个真硬件**(~700 行 + ~300 测试,~3-4 周,**含声纹识别**)— `voice/` 子模块(capture/silero VAD/funasr ASR/porcupine 唤醒词)+ `events/adapters/voice_adapter.py`(mic → VoiceEvent)+ 新增依赖 `sounddevice` + `funasr` + `torch` + `resemblyzer`(放 `[project.optional-dependencies.voice]`,默认不装)。**BaseAudioCapture / BaseASR / BaseWakeWordDetector 抽象**让单测用 mock,大幅降测试成本。**🆕 声纹识别子模块(`voice/speaker/`,7.6 阶段必做,TOTHINK 1.1/1.2 场景刚需)**:① `SpeakerDiarizer`(VAD 切分 + 说话人聚类,输出多 segment)② `SpeakerEmbedder`(resemblyzer 256 维,中文友好模型)③ `SpeakerRegistry` 抽象 + 7.5 阶段 stub `SimpleSpeakerRegistry`(内存 dict,7.6 接 EmbeddingSpeakerRegistry 余弦相似度)④ `voice.speaker_registry` 集成到 VoiceAdapter(算 embedding → 查 Registry → 写 VoiceEvent.speakers)。**注意单 user 设计**:`ButlerOrchestrator.ainvoke()` 仍只接**单值 user_id**(每轮 ainvoke 一个 user 在说话);多 user 场景(TOTHINK 1.1:男女主人交替)通过**多轮 ainvoke** 切换 user_id,声纹结果落 `HumanMessage.additional_kwargs["speakers"]` 供后续 Sub-Agent / 记忆系统 / Phase 8 情绪治理消费。**前置**:7.5 ✅(7.5 已落 `SpeakerInfo` + `BaseEvent.speakers` + `SimpleSpeakerRegistry` 协议占位)
- [ ] **Phase 7.7:TTSOutputAdapter 第一个真输出**(~250 行 + ~150 测试,~1 周)— `voice/tts/{edge,offline}.py`(`edge-tts` 云端 + `sherpa-onnx` Piper 离线 7.7.1)+ `events/adapters/tts_output_adapter.py`(ButlerReply → TTS → 播放)+ 端到端 PoC(说话→管家答→本机播放)。**前置**:7.5 ✅(可与 7.6 并行)
- [ ] **Phase 7.8:CameraAdapter 视频输入,音视频流水线收口**(~800 行 + ~250 测试,~4-6 周,**优先级低于 7.6+7.7**)— `voice/capture/buffer.py`(MultiTrackSyncBuffer 音视频对齐)+ fork Miloco `perception/` 子目录(Gate / Identity / Omni 三件套)+ `events/adapters/camera_adapter.py`(摄像头 → 感知 → DeviceEvent → **MemoryFacade.remember(text=caption)**)。**核心硬条件(case 6/7 验收)**:① 感知层 Omni 输出的 caption 必须**强制**进 `MemoryFacade.remember`,不依赖 ProactiveLoop 触发;② 1 周后 recall "蓝裙子" 必须能命中(若 < 70% 启动 7.9 EmbeddingGemma 2)。**当前不引入多模态 embedding**——`ReactiveSubmission.metadata` / `ButlerReply.metadata` 留扩展位。**详细设计**:`docs/PHASE_7_TECHNICAL_DESIGN.md` §7 + §12。**前置**:7.7 ✅
- [ ] **Phase 7.9:多模态记忆(EmbeddingGemma 2,观察项,不启动)**(~380 行 + ~120 测试,等触发)— `capabilities/embedding/gemma2.py`(740M 参数,Apache 2.0,768 维 unified space)+ `factory.py` 渐进加载(text-only 540MB / +vision 880MB / +audio 1.1GB)+ `LongTermStore` 多模态索引(`image_embedding: list[float] | None`)+ `MemoryFacade.remember(text=..., image=...)` 双路召回。**触发条件(任一满足才启动)**:① 用户 3 个月内主动要求"以图搜图" ≥ 5 次;② 7.8 case 7 类召回命中率 < 70%;③ Qwen2-VL-7B 本地稳定跑(< 5s 推理);④ Phase 9+ 多节点部署(32GB+ RAM,用户 2026-10-15 决策顺延)。**决策可逆**:LongTermStore 加列 / facade 加可选参数 / 业务层零修改,仅 1 行 env 切换 provider。**详细决策**:`docs/PHASE_7_TECHNICAL_DESIGN.md` §12。**不进本周期待办**——等真实使用数据再决定。
- [ ] **Phase 8:情绪治理引擎(下一阶段,核心护城河)**(~1350 行 + ~370 测试,~6 周)—— 6 模块:**8.1 EmotionMonitor**(实时负面因子计算,纯规则 Σ 公式,P95 < 100ms)/ **8.2 InterventionPlanner**(反向追溯根因 + 历史成功 pattern 检索 + LLM 设计 2-3 方案,选最优,P95 < 2s)/ **8.3 InterventionExecutor**(调 7.5 AnswerRouter + 7.7 TTS 执行干预,带降级链,P95 < 1s)/ **8.4 RecoveryTracker**(每日检查情绪因子,7 天观察期 + 48h 正因子持续 → 判定恢复,失败升级回 8.2)/ **8.5 DataPruner**(恢复成功 → 标记 ACTIVE → RESOLVED,不真删,保留复盘)/ **8.6 PatternArchive**(成功案例归档为 `bp-emotion-recovery-xxx` BehaviorPattern,跨 session 可复用)。**核心范式**:看到事实 → 持久化为关系(带 sentiment)→ 派生行为模式 → **主动治理情绪** → 持续追踪 → 标记清除 → 学习闭环。**伦理护栏**:数据本地化(`data/emotion.db`)/ voice_tts 默认关闭 / 同 1 周最多 3 次干预,超限降级为只观察 / toxic 关键词(自伤/想死/不想活)自动升级"建议联系专业心理咨询"通知 / 管家不是心理医生,只做"识别异常→推专业帮助"桥梁。**PoC 简化**:情绪维度只做 2 维(negative_factor + positive_factor),完整 4 维 PAD 模型留 Phase 10+。**核心 Schema 扩展**:`relation` 加 `sentiment` / `sentiment_score` / `status`(ACTIVE/RESOLVED)字段;新增 `emotion_state` 表(因子缓存) + `intervention` 表(干预日志)。**详细设计**:`docs/PHASE_8_EMOTION_GOVERNANCE.md`(6 个子阶段 + 6 个测试 case + 验收标准 + 风险缓解)。**前置**:7.6 VoiceAdapter ✅(7.3 干预执行器要调 TTS/设备) + 7.7 TTS ✅(TTS 是干预主要出口) + 6.6 Personality ✅(可选,作为个性化基线)。**不依赖**:6.3/6.4/6.5/6.7/6b(已 A-E 决策往后推)

---

## 3. 架构约束(实现时必须遵守)

1. **能力层自建抽象**:定义我们自己的 `BaseLLM` / `BaseTool` / `Embedder` / `BaseLangmemAdapter`,不直接 import LangChain 类型。✅ 已落地。
2. **LLM 实现后端可切换**:`SMARTBUTLER_LLM_BACKEND=http|langchain`。默认 `http`;切到 `langchain` 由 LangChain 负责消息转换 / 工具绑定 / 流式 chunk / reasoning_content 透传。两条路线对外都是 `BaseLLM` 接口,业务层零差别。
3. **Embedder 实现可降级**:QwenEmbedder 不可用 → StubEmbedder(hash → 1024 维)。`memory/` 业务层只 import `Embedder` Protocol,不感知具体实现。
4. **Manager 是 Agent 唯一入口**:thinking 层**只**通过 `agents/manager/` 找 Agent,不直接 import 具体 Agent 类。
5. **Agent 接口契约固定**:`name / description / tools / ainvoke()` 是必实现方法;外加 `to_langchain_tool()` 用于暴露成 `delegate_to_<name>` LangChain Tool。
6. **LangGraph State 字段固定**:`user_input / messages / current_decision / pending_tasks / tool_results / iteration / context / final_response / is_complete`。
7. **依赖方向**:Interface → Thinking → Emotion → Agents;capabilities 被 Thinking/Agents 消费。✅ LLM 已通过 `create_llm(settings) -> BaseLLM` 暴露接口。
8. **Skill ≠ Sub-Agent(ADR-006)**:Skill 是 Anthropic Skills 格式的能力包,由管家(强模型)执行;Sub-Agent 是代码实现的领域智能体,有自己的 LLM(便宜模型)。两者概念独立,禁止混淆。
9. **Multi-Agent 走 LangGraph Supervisor + Tool-Calling(ADR-005)**:管家作为中央调度器,通过 LangChain `StructuredTool` 机制调用 Sub-Agent;不使用 `create_supervisor` 高层封装,保留性格注入 / 记忆检索等定制空间。
10. **ProactiveReasoning 降级契约(ADR-009)**:`ProactiveReasoning` 必须**双轨部署**——`LLMProactiveReasoning`(主路,Phase 6b)+ `RuleBasedProactiveReasoning`(降级后备,已落库)。**任何 LLM 故障必须降级到后备**,URGENT 事件不依赖 LLM 也能主动开口。`RuleBasedProactiveReasoning` **永不被删**——它是安全网,不是临时占位。
11. **Memory 三层存储严格分离**(不变量 §2.3 详述)。
12. **thinking 只 import `MemoryFacade`**:不感知底层 `Embedder` / `Retriever` / `Hooks`;`MemoryFacade` 也不直接 import `QwenEmbedder` / `Langmem0030Adapter` —— 只 import Protocol。
13. **降级链必须闭合**:`format_for_prompt` 失败 → 返回空字符串 → thinking 看到"无相关历史"继续工作。Qdrant 挂 → `SQLiteStorage` LIKE 检索。langmem 装不上 → 正则 fallback。**任何环节失败,管家不崩**。

---

## 4. LLM 双后端决策(Phase 1.5)

### 4.1 决策结论

**保留自建 `OpenAICompatibleLLM`(默认),并新增 `LangChainLLMAdapter` 作为可选 backend**。两者都实现 `BaseLLM` 接口,工厂 `create_llm()` 按 `SMARTBUTLER_LLM_BACKEND` 路由,业务层完全无感。

### 4.2 为什么这么做

| 维度 | 自建 HttpLLM(默认) | LangChain Adapter(可选) |
|---|---|---|
| **首版交付速度** | 慢 | 快 |
| **依赖体积** | 极小 | 较大 |
| **协议可控性** | 全栈可调试 | 栈深 |
| **流式 tool_calls** | ❌(当前实现未做) | ✅ LangChain 内部处理 |
| **reasoning_content 透传** | ⚠️ 已在 `StreamChunk` 暴露 | ✅ LangChain 通过 `additional_kwargs` 透传 |
| **structured output** | ❌ | ✅ `with_structured_output` 现成 |
| **Tool / Loop 复用** | 协议转换要自写 | LangChain 原生 `@tool` / LangGraph 直接用 |
| **升级成本** | 自维护 | LangChain 版本风险(0.3.x → 1.x) |
| **可调试性** | 栈浅(httpx 直连) | 栈深(多层包装) |

### 4.3 取舍逻辑

1. **不替换**:自建实现已覆盖 80% 场景,切到 LangChain 等于把验证过的代码扔掉重写,迁移成本高。
2. **不单选 LangChain**:完全抛弃自建意味着放弃协议栈控制权,被 LangChain 版本节奏绑死。
3. **不推迟到 Loop 阶段再做**:Loop 阶段一旦要做 tool_calls 循环 / schema 转换 / 消息聚合,再切 adapter 改动面更大,提前做更划算。
4. **保持 Backend 切换零成本**:`create_llm(settings)` 一行调用切到 LangChain,业务代码零修改。

### 4.4 不变量

- 现有单测(包括 `tests/e2e/test_streaming.py` 与 `test_openai_compatible.py`)**零行修改**全部通过。
- `BaseLLM` 公开接口(`chat / chat_stream / aclose`)保持不变。
- `LLMResponse / StreamChunk / ToolSpec / ToolCall / Message` 数据类型保持不变。
- 业务层只依赖 `BaseLLM` 与上述数据类型,不感知具体实现。

### 4.5 何时切到 `backend=langchain`

- 需要 LangChain 原生 `@tool` 装饰器 / LangGraph 工具节点 → 切换
- 需要 `with_structured_output` 强结构化输出 → 切换
- 需要 LangSmith 监控 / LangServe 部署 → 切换
- 仅做基础 chat / stream / 工具调用 → 保持默认 `http`

### 4.6 配置示例

```dotenv
# 默认配置(httpx 直调)
SMARTBUTLER_LLM_PROVIDER=openai
SMARTBUTLER_LLM_BACKEND=http
SMARTBUTLER_LLM_MODEL=deepseek-flash
SMARTBUTLER_LLM_OPENAI_API_KEY=sk-...
SMARTBUTLER_LLM_OPENAI_BASE_URL=https://api.deepseek.com

# 切到 LangChain 适配器
SMARTBUTLER_LLM_BACKEND=langchain
```

---

## 5. 多 Agent + Skill 架构(Phase 3-5 落地)

> 完整决策记录见设计文档 §5.5 / §5.6 / §5.7。

### 5.1 Sub-Agent(代码实现的领域智能体)

- 形态:Python 类继承 `BaseAgent`
- 执行方:**Sub-Agent 自己的 LLM**(便宜模型,如 Haiku)
- 触发方式:管家 LLM 通过 `delegate_to_<name>` tool call 调用
- 典型:`HomeAgent`(设备控制)/ `ScheduleAgent`(日程)/ `SearchAgent`(搜索)
- 适用场景:需要**状态机**、**副作用管理**、**独立推理**的任务

### 5.2 Skill(Anthropic Skills 格式能力包)

- 形态:文件夹 + `SKILL.md`(YAML frontmatter + markdown body)
- 执行方:**管家 LLM**(强模型,自己执行)
- 触发方式:管家 LLM 看到 skill 注入的 system_prompt 后自主决定是否用
- 典型:`pdf-summary/` / `stock-analysis/` / 亲属拖入的 `~/.smartbutler/skills/*/`
- 适用场景:**强模型 + 步骤提示 + 工具**就能完成的任务

### 5.3 协作模式:LangGraph Supervisor + Tool-Calling

```
用户消息 → LangGraph Loop (管家 LLM 推理)
              ↓ 工具集
              ├─ delegate_to_test_time_agent → TestTimeAgent (Phase 3)
              ├─ delegate_to_home_agent       → HomeAgent (Phase 10+, Haiku)
              ├─ delegate_to_schedule_agent   → ScheduleAgent (Phase 10+, Haiku)
              ├─ delegate_to_search_agent     → SearchAgent (Phase 10+, Haiku)
              ├─ pdf_extract                  (Skill 工具,管家直接调)
              ├─ get_today_digest             (管家元工具)
              └─ ... (其他 Skill 工具,管家直接调)
```

**核心**:Sub-Agent 通过 LangChain `StructuredTool` 机制暴露;管家 Loop 用 LangGraph 原生 `StateGraph + ToolNode` 编排。

### 5.4 Sub-Agent vs Skill 边界

| 维度 | Sub-Agent | Skill |
|------|-----------|-------|
| 触发方 | 管家 LLM tool_call | 管家 LLM 看 system_prompt 自主判断 |
| 执行方 | Sub-Agent 自己的 LLM | 管家 LLM |
| 维护者 | 开发者 | 任何人(含亲属) |
| 修改代码 | 需要 | **不需要** |

**判断依据**:需要状态机 / 副作用 / 独立推理 → Sub-Agent;强模型 + 步骤提示就能搞定 → Skill。

---

## 6. 快速开始

```bash
# 1. 安装依赖(含开发依赖)
python -m pip install -e ".[dev]"

# 2. 复制环境变量模板并填入真实值
cp .env.example .env
# 至少填: SMARTBUTLER_LLM_OPENAI_API_KEY
# 可选填: SMARTBUTLER_EMBEDDING_API_KEY(不填则降级 StubEmbedder,recall 精度降级但可跑)

# 3. 跑单元测试(545 用例,默认全跑,实测 ~24s 全过)
pytest tests/unit

# 4. 跑集成测试(需 .env 已填 API key)
pytest -m integration

# 5. 跑 E2E 测试(需 .env 已填 API key + Embedding key)
pytest -m e2e

# 6. Lint + 类型检查
ruff check .
mypy smartbutler
```

### 6.1 一行验证 thinking + memory 端到端

```bash
pytest -m e2e tests/e2e/test_memory_thinking.py -v -s
```

跑通即代表:**用户消息 → QwenEmbedder 1024 维 → Qdrant 嵌入式 → MemoryFacade 召回 5 条 → 注入 system_prompt → DeepSeek-Flash 看到"用户之前说 X" → 生成回答**整条链路 OK。

---

## 7. 配置

所有配置通过环境变量(或项目根目录的 `.env` 文件)注入。子模块前缀:

| 前缀 | 模块 |
|------|------|
| `SMARTBUTLER_LLM_` | LLM 子模块 |
| `SMARTBUTLER_STORAGE_` | 存储子模块(SQLite / Qdrant / Redis) |
| `SMARTBUTLER_LOGGING_` | 日志子模块 |
| `SMARTBUTLER_AGENT_` | Agent 子模块 |
| `SMARTBUTLER_SKILL_` | Skill 子模块(workspace / builtin / user 目录) |
| `SMARTBUTLER_EMBEDDING_` | Embedding 子模块(Phase 6.2 P0) |

### 7.1 最小 `.env`

```dotenv
SMARTBUTLER_LLM_PROVIDER=openai
SMARTBUTLER_LLM_MODEL=deepseek-flash
SMARTBUTLER_LLM_OPENAI_API_KEY=sk-xxx
SMARTBUTLER_LLM_OPENAI_BASE_URL=https://api.deepseek.com
SMARTBUTLER_LLM_BACKEND=langchain    # 可选;默认 http

SMARTBUTLER_STORAGE_SQLITE_PATH=./data/smartbutler.db
# 不设 SMARTBUTLER_STORAGE_QDRANT_URL → 嵌入式 path=./data/qdrant
# 设了则切服务模式(Phase 9+ 多节点部署到中心化机器时用)

SMARTBUTLER_LOGGING_LEVEL=INFO
SMARTBUTLER_LOGGING_JSON_OUTPUT=false

# Embedding(可选;不配则降级 StubEmbedder,管家仍可跑但 recall 精度降级)
SMARTBUTLER_EMBEDDING_PROVIDER=qwen
SMARTBUTLER_EMBEDDING_API_KEY=sk-xxx
SMARTBUTLER_EMBEDDING_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
SMARTBUTLER_EMBEDDING_MODEL=qwen3.7-text-embedding-flash
SMARTBUTLER_EMBEDDING_DIMENSION=1024   # 必须与 SMARTBUTLER_STORAGE_QDRANT_VECTOR_SIZE 一致
```

完整模板见 `.env.example`。

---

## 8. 开发规范

- **类型注解**:所有公共接口必须有完整类型注解(`mypy strict`)。
- **日志**:业务模块统一通过 `from smartbutler.utils import get_logger` 获取 logger,禁止 `print`。
- **配置**:业务模块禁止读取环境变量,必须通过 `smartbutler.config.load_xxx_settings()` 获取。
- **降级**:任何外部依赖(LLM / Embedder / Qdrant / langmem)失败时必须有降级路径,管家不能崩。
- **测试**:每个新模块必须带单测(默认 `pytest tests/unit` 全跑);跨模块链路补 e2e(默认 skip,显式 `-m e2e` 启用)。

---

## 9. 部署后优化(横切关注点,任何 phase 顺手补)

> 部署场景:管家在家中部署,全家共享唯一一个实例,无并发问题。
> 这些是**横切关注点**——不强求在某个 phase 内集中做完。

**待实现**:
- [ ] 9.1 Tool 重试 & 熔断(单 tool 粒度)
- [ ] 9.2 LLM 重试(限流 / 超时,exponential backoff + jitter)
- [ ] 9.3 流式输出(token 级,`astream_events`)
- [ ] 9.4 错误分类 & 友好回复(`ButlerErrorKind` 枚举 → 文案映射)
- [ ] 9.5 可观测性(`ButlerCallTrace` → structlog)

**不做**(家用场景不需要):
- ~~并发安全~~ / ~~多 LLM 后端切换~~ / ~~Postgres Checkpointer~~ / ~~OpenTelemetry / Prometheus 上报~~

---

## 10. 关键变更记录(README 同步用)

| 日期 | 版本 | 变更 | 关联 |
|------|------|------|------|
| 2026-10-15 | v0.5.1 | **🆕 阶段表 + 优先级重排(用户决策 A-F-G) + Phase 8 重定位为情绪治理引擎**:A=`Phase 6.3` vacuum/遗忘 1060 行推 Phase 9+ / B=`Phase 6.4` episodic 推 Phase 9+ / C=`Phase 6.7` Learn 推 Phase 9+ / D=总线+输入输出适配器**等于已做**(7.4 Normalizer+Trigger ✅ + 7.5 AnswerRouter+PresenceService ✅) / E=`Phase 6.5` habit/cross_thread 推 Phase 9+ / F=各子 agent(Home/Schedule/Search)整体推 Phase 10+ / G=`Phase 7` 当前在做(7.6/7.7/7.8 维持 7.6→7.7→7.8 顺序)。**Phase 8 重定位**:从"核心 Sub-Agent"改为"**情绪治理引擎(Emotion Governance Engine)**"——6 模块(8.1-8.6,1350 行 + 370 测试,6 周),核心范式:看到事实→持久化为关系(带 sentiment)→派生行为模式→**主动治理情绪**→持续追踪→标记清除→学习闭环。**伦理护栏**:数据本地 / voice_tts 默认关 / toxic 关键词自动升级"建议联系专业心理咨询" / 管家不是心理医生,只做桥梁。**新设计文档**:`docs/PHASE_8_EMOTION_GOVERNANCE.md`(2026-10-15 落地,1234 行,10 章 + 3 附录)。**新节奏**:**Phase 7.6/7.7/7.8 真硬件对接** → **Phase 8 情绪治理(核心护城河)** → **Phase 9 治理优化回填 + 多节点 Redis Streams** → **Phase 10+ 原 Sub-Agent 主题**。**重点强调**:记忆(Memory)是 SmartButler 的核心,情绪治理引擎依附于此 | `docs/PHASE_8_EMOTION_GOVERNANCE.md`(本轮新建) |
| 2026-10-15 | v0.5.2 | **🆕 7.6 声纹识别协议占位**(TOTHINK 1.1/1.2 场景):`SpeakerInfo` frozen BaseModel(`user_id`/`confidence`/`is_known`/`embedding`/`voice_segment_id`/`language`)+ `BaseEvent.speakers: list[SpeakerInfo]` 默认空 list(向后兼容,frozen 透传)+ `VoiceEvent.make(speakers=...)` + `to_user_input` 拼 "[会话参与者:dad(95%)+mom(72%)]"(LLM 看到在跟谁对话)+ `SpeakerRegistry` Protocol 抽象 + `SpeakerProfile`/`SpeakerMatch` dataclass + `SimpleSpeakerRegistry` 内存版(7.5 stub,7.6 接 resemblyzer 余弦相似度)+ `ButlerOrchestrator.ainvoke/astream(speakers=...)` 接本轮声纹结果,落 `HumanMessage.additional_kwargs["speakers"]` 供 SqliteSaver 落盘透传(Sub-Agent/记忆/Phase 8 消费)+ 6 个 SpeakerInfo 字段 + 9 个 VoiceEvent 行为 + 5 个 SimpleSpeakerRegistry 操作 + 4 个 TOTHINK 1.1/1.2 场景 fixture = **29 个新单测**。**关键设计原则**:每轮 ainvoke 一个 user 在说话,单值 `user_id` 足够(不引入 `user_ids` 多值);多 user 场景通过**多轮 ainvoke 切换 user_id** + 声纹结果透传到 message metadata 表达。**工作量**:~470 行代码 + ~400 行测试。**前置**:7.5 ✅ | `smartbutler/events/core/event.py` + `events/protocol/voice_event.py` + `events/routing/speaker_registry*.py` + `thinking/loop/orchestrator.py`(speakers kw-only)+ `tests/unit/events/test_speaker_registry.py` + `tests/unit/thinking/test_orchestrator_speakers.py` |
| 2026-10-14 | v0.5.0 | **🆕 Phase 7.5 实施 — AnswerRouter + PresenceService 完整落地**:D1 `RouteResult` frozen dataclass(5 字段 + `success` property + `__str__`)+ D2 `DeviceRegistry`(register/get/all/first + `_device_id` 约定)+ D1+D2+D4 `ConsoleAnswerRouter`(SILENT 不推 / NORMAL 走 PresenceService / URGENT 推全屋,内部 `asyncio.gather` 并联)+ D3 `SimplePresenceService`(in-memory dict + 便利方法 set_sync/clear)+ `AnswerRouter.route()` 返 `RouteResult` 替代 `bool` + `ButlerServer._answer_router` 字段 + `set_answer_router()` + `set_presence_service()` + `_on_bus_event` 6 步流程(归一化→限速→处理→老逻辑→答案空校验→AnswerRouter 选路,Reactive 链路保持老逻辑不动)+ `dev_chat.py` `/presence set/get/clear/list` REPL 命令 + `--no-answer-router` 开关 + `InMemoryOutputAdapter` 加 `device_id` 可选参数(backward-compat)+ `tests/unit/server/test_answer_router_e2e.py` 30 case(6 e2e + RouteResult 5 + DeviceRegistry 7 + SimplePresenceService 5 + ConsoleAnswerRouter 直测 6 + 1 全场景);**851/851 测试全过(原 821 → 851,+30 新测试)**;**总工作量 ~1020 行(超 415 估 1.5x,因测试 580 行 vs 180 估)**,1 周节奏不变;`events/routing/__init__.py` 导出 6 个新类型 | `docs/PHASE_7_TECHNICAL_DESIGN.md §4` |
| 2026-10-14 | v0.4.3 | **🆕 7.5 详细设计敲定 D1-D4**:AnswerRouter 返 `RouteResult` 而非 `bool`(D1);新增 `DeviceRegistry` 抽象集中管 OutputAdapter 池(D2);`SimplePresenceService` 7.5 阶段只接 dev_chat + 测试(D3);`route()` 内部 `await` 所有 push(D4);§4 重写(从 30 行 → 200 行);README §2.7 下一站从 7.4 → 7.5;**7.5 实施总工作量 415 行 + 180 测试,1-1.5 周** | `docs/PHASE_7_TECHNICAL_DESIGN.md §4` |
| 2026-10-14 | v0.4.2 | **🆕 Phase 7.4 实施 — 5 钩子 + Normalizer+Trigger + ServerConfig**:5 钩子落地(H1/H2/H3/H4/H5,7.9 多模态记忆预留);`DedupNormalizer`(event_id+window 去重,LRU 10000);`DefaultEventTrigger`(限速 20/min,route → Proactive,内部 try/except);`ServerConfig` 加 normalizer/trigger 字段 + start() 自动构造;`_on_bus_event` 走 Normalizer → Trigger → ButlerReply;5 case 端到端测试(test_event_bus_e2e.py);**821/821 测试全过(原 773 → 821,+48 新测试)** | `docs/PHASE_7_TECHNICAL_DESIGN.md §3 + §12.6` |
| 2026-10-14 | v0.4.1 | **🆕 7.4 实施时为 7.9 多模态记忆预留 5 个钩子落地**:`docs/PHASE_7_TECHNICAL_DESIGN.md` §12.6 新增(总 10 个子小节),5 个钩子总成本 7.4 内 0.5 天 + ~40 行代码,**7.9 启动从 1-2 周改造变 0 改造**;**H2 强制**(Day 1 加 Qdrant payload 字段,错过 = 7.9 启动要迁移数据,几小时锁表);H1/H3/H4 强烈建议;H5 建议;§3 7.4 待办同步加 5 个钩子;**7.4 总工作量从 150 行 → 190 行,1 周节奏不变** | `docs/PHASE_7_TECHNICAL_DESIGN.md §12.6` |
| 2026-10-14 | v0.4.0 | **🆕 多模态记忆决策敲定 + 音视频流水线收口 + 7.9 观察项**:`docs/PHASE_7_TECHNICAL_DESIGN.md` §12 新增,7.6/7.8 感知层采集音视频但记忆层统一转文本(Qwen text embedding,**不上**多模态 embedding);`ReactiveSubmission.metadata` / `ButlerReply.metadata` 留扩展位;**7.9 EmbeddingGemma 2** 标为"未来观察项,不启动",触发条件任一:用户 3 个月主动以图搜图 ≥ 5 次 / 7.8 召回命中率 < 70% / Qwen2-VL-7B 本地稳定跑 / Phase 8+ 多节点;7.8 验收加 case 6/7 硬条件(感知层文本化进记忆 + 召回验证);§1.1 架构图 + §1.2 数据流 + §1.3 不变量 #11-#13 同步加"感知层多模态,记忆层单模态"约束;**7.6/7.8 工作量不变**,新增 7.9 预备工作量 ~380 行(暂不写) | `docs/PHASE_7_TECHNICAL_DESIGN.md §12` |
| 2026-10-14 | v0.3.1 | **Phase 7 技术方案落地**:`docs/PHASE_7_TECHNICAL_DESIGN.md` 新建,盘点真实状态(7.0+7.1+7.2+7.3 全部已落库)+ 拆 7.4 Normalizer+Trigger / 7.5 AnswerRouter+Presence / 7.6 VoiceAdapter / 7.7 TTS / 7.8 Camera 5 个子阶段(总自研量 ~1900 行 + ~580 测试,约 5-7 周);README 阶段表 + 已交付模块表 + 待办列表同步更新 | `docs/PHASE_7_TECHNICAL_DESIGN.md` |
| 2026-10-13 | v0.3.0 | **Phase 6.6 Personality 落地**:5 维 trait 数据模型(Pydantic + [0,1] 约束) + personality.md(YAML frontmatter)加载/保存 + 三档翻译渲染 system prompt 片段 + ButlerOrchestrator 接入(`personality` 参数 + `reload_personality()` 刷缓存);77 新单测(773 total) | `emotion/personality/{traits,loader,injector}.py` |
| 2026-10-10 | v0.2.12 | **Phase 6.2 P0 正式落地**:MemoryFacade + thinking 接入 + e2e 10 用例全过 | `docs/PHASE_6.2-6.5_TECHNICAL_DESIGN.md` |
| 2026-10-09 | v0.2.11 | 优化依赖,修复测试问题 | - |
| 2026-10-09 | v0.2.10 | Skill loader + 6 个文件工具 | - |
| 2026-10-09 | v0.2.9 | Proactive 思考模式框架 | ADR-009 |
| 2026-10-08 | v0.2.8 | Reactive 思考模式 + event 框架 | ADR-005/008 |
| 2026-10-07 | v0.2.7 | Agent 支持 | ADR-005 |
| 2026-10-06 | v0.2.6 | Tool 支持 | ADR-002 |
| 2026-10-05 | v0.2.5 | LLM 集成 langchain 适配器 | Phase 1.5 决策 |
| 2026-10-04 | v0.2 | LLM 链接部分 | - |
| 2026-10-01 | v0.1 | 初始化基建 | - |
