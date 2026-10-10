# SmartButler

> 有性格、有记忆、懂上下文的家用智能管家。
>
> **当前版本**:`v0.2.12`(`7` 个 commit;`545 unit + 5 integration + 24 e2e` 全部 ✅)。
> **下一站**:Phase 6.3(4 层分片 + 固化 + 遗忘,详细设计见 `docs/PHASE_6.2-6.5_TECHNICAL_DESIGN.md` §2.5.1)。
> **详细方案**:`docs/PHASE_6.2-6.5_TECHNICAL_DESIGN.md`(Memory 4 个子阶段设计)。
> **架构总图**:`docs/TECHNICAL_DESIGN.md`(若不存在,见 git 历史 commit `828337e`)。

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
| **Phase 3** | Sub-Agent 层(框架) | 🟡 **仅脚手架** | `BaseAgent` + `AgentManager` + `TestTimeAgent`(脚手架,集成测试用)。**领域 Sub-Agent(`HomeAgent` / `ScheduleAgent` / `SearchAgent`)属 Phase 8** |
| **Phase 4** | LangGraph Loop + Supervisor | ✅ | `StateGraph` + `ToolNode` + Checkpointer + `ButlerPromptBuilder` |
| **Phase 4b** | ProactiveLoop 框架 | ✅ | 双循环架构 + `RuleBasedProactiveReasoning` 降级后备 + 沉默默认 |
| **Phase 5** | Skill loader | ✅ | `SKILL.md` → 扫描解析 → 注入 system prompt + 6 个文件工具 + 3 类权限 |
| **Phase 6.1** | Memory:长短期落地 | ✅ | `SQLiteStorage` + `QdrantStorage`(嵌入式默认) + `ShortTermMemory`(`SqliteSaver`) + `LongTermStore` |
| **Phase 6.2 P0** | Memory:thinking 接入 + 高级 API | ✅ | `capabilities/embedding/`(Qwen + Stub)+ `capabilities/langmem/`(v0.0.30 + 正则 fallback)+ `MemoryFacade` + `MemoryHooks` + 注入 `memory_block` 到 system_prompt + **e2e 10 用例真打通** |
| **Phase 6.3** | Memory:4 层分片 + 固化 + 遗忘 + vacuum | ⏳ **下一站** | `consolidation.py`(L1/L2/L3 升格管道 + LLM 评分)+ `forgetting.py`(5 类遗忘 + 软删除)+ `levels.py`(4 层 tag)+ `summarizer.py`(5W1H 提炼)+ `task_boundary.py` + `vacuum.py`(双时间窗 7+30 天) + `archive_store.py` + `scripts/memory_cli.py`(手动 CLI)。详见 `docs/PHASE_6.2-6.5_TECHNICAL_DESIGN.md` §2.5.1(含 §2.5.1.16) |
| **Phase 6.4** | Memory:episodic + 跨层 rollup | ⏳ | `summarizer.py`(5W1H 提炼,已在 6.3)+ `episodic.py`(情景记忆,SQLite JSON) |
| **Phase 6.5** | Memory:habit + cross-thread | ⏳ | `habit.py` + `cross_thread.py`(为 Phase 6b 留 hook) |
| **Phase 6.6** | Personality | ⏳ | 静态 traits + injector + 接受用户显式指令 |
| **Phase 6.7** | Learn | ⏳ | 反馈事件流 + trait 映射 + 触发 `personality.evolve()` |
| **Phase 6b** | ProactiveLoop 真实化 | ⏳(必须等 6 完成) | `LLMProactiveReasoning` 接 Memory + Persona + LLM 推理;`RuleBasedProactiveReasoning` **永不被删**(安全网) |
| **Phase 7** | 事件驱动 + 路由 | 🟡 骨架(接口已就位,0 行实现) | `EventNormalizer` / `EventTrigger.route()` / 设备适配器 |
| **Phase 8** | 核心 Sub-Agent | ⏳ | `HomeAgent`(HomeAssistant)/ `ScheduleAgent` / `SearchAgent` |
| **Phase 9** | 多模态感知 | ⏳ | ASR + TTS + Vision |
| **Phase 10** | 主动服务 | ⏳ | 摄像头/麦克风监听/计划任务(与 Phase 7 联动) |
| **Phase 11** | 性格演化 + 反馈学习 | ⏳ | 与 Phase 6.7 重叠,落地到管家级别 |
| **Phase 12+** | self-evolving(候选) | ⏳ | skill 自动生成;当前**严格不做**——Learn 只管"反馈通道" |

### 2.2 Phase 6 子阶段拆分(Memory → Personality → Learn 严格串行)

> 严格串行——每阶段必须前一阶段跑通单测 + 集成测试后才启动。
> **理由**:Memory 是 Personality 的输入数据源,Learn 是 Personality 的演化器。

| 子阶段 | 模块 | 自研量 | 关键依赖 | 启动前置 |
|--------|------|--------|----------|----------|
| **6.1** Memory:长短期落地 | `storage/{sqlite,qdrant}.py` + `emotion/memory/{short_term,long_term}.py` | ~200 行 | `qdrant-client` + `langmem` | Phase 5 完工 ✅ |
| **6.2 P0** Memory:thinking 接入 | `capabilities/embedding/` + `capabilities/langmem/` + `emotion/memory/{embedder,importance,retrieval,hooks,facade}.py` + `thinking/loop/` 注入 `memory_block` | ~630 行 + ~340 测试 | `qwen3.7-text-embedding-flash` + `langmem 0.0.30` | 6.1 ✅ |
| **6.3 P1** Memory:4 层分片 + 固化 + 遗忘 + vacuum | `emotion/memory/{consolidation,forgetting,levels,summarizer,task_boundary,vacuum,archive_store}.py` + `scripts/memory_cli.py` | **~1060 行 + ~40 测试 + 2 e2e** | LLM 评分 + 5W1H 提炼 + 4 层 tag + 双时间窗 vacuum(7+30 天) | 6.2 ✅ + 用户产生"记太多/记错"反馈。**详细设计**:`docs/PHASE_6.2-6.5_TECHNICAL_DESIGN.md` §2.5.1(含 §2.5.1.16 vacuum 治理) |
| **6.4 P1** Memory:episodic + 跨层 rollup | `emotion/memory/{episodic,hierarchical_rollup}.py` | ~150 行 + ~10 测试 | 6.3 已有 4 层分片,6.4 只补对话轨迹 | 6.3 ✅ + 用户问"最近 3 个月偏好" |
| **6.5 P1** Memory:能力补全 | `emotion/memory/{habit,cross_thread}.py` | ~150 行 | 6.4 的 summarizer | 6.4 ✅ + Phase 6b 启动 |
| **6.6** Personality | `emotion/personality/{traits,state,injector}.py` | ~200 行 | **不依赖 6.5**——空壳 personality 独立可测 | 6.5 ✅(实际 6.1 即可并行) |
| **6.7** Learn | `emotion/learn/{feedback_log,signal,evolution_hook}.py` | ~200 行 | 6.6 `personality.evolve()` | 6.6 ✅ |

**Phase 6 总自研量**:~1250 行 + ~50 测试,约 3-4 周。

### 2.3 关键不变量(Phase 6 全程必须遵守)

1. **3 层存储严格分离**——短期 = `SqliteSaver`(checkpointer)/ 长期事实 = Qdrant / 长期情景 = SQLite JSON。**不允许把"短期"也存 Qdrant**。
2. **Qdrant 默认嵌入式**(`path=data/qdrant`),**用户不感知**。Phase 8+ 切服务模式时只改 `StorageSettings.qdrant_url` 即可。
3. **Personality 永远不直接调 LLM**——只接受显式 user 指令 + learn 触发的 trait 调整。**Personality 是数据,不是 Agent**。
4. **Learn 严格不"自动生成 skill"**——skill 生成是 self-evolving agent 范畴,属 Phase 12+ 候选,Phase 6 只管"反馈通道"。

### 2.4 已知风险(本阶段要盯)

- **R1 langmem 装不上**:`pip install` 失败 → `capabilities/langmem/_fallback.py` 正则抽事实接管,业务层无感知。
- **R2 Qdrant 嵌入式性能**:Windows fd 泄漏报告(issue #1234) → 改 `SMARTBUTLER_STORAGE_QDRANT_URL=http://localhost:6333` + docker 跑。
- **R3 QwenEmbedder 限流/不可用**:Qwen 429/401/超时 → `StubEmbedder`(hash → 1024 维)接管,recall 精度降级但管家不崩。
- **R4 consolidation 升格过快/过慢**(待 6.3):重要性分级([0.0, 0.2)/[0.2, 0.5)/[0.5, 0.8)/[0.8, 1.0] 4 档)容易把琐事升 L1 / 关键约定漏升 L4——6.3 单测必须 4 档各 ≥ 5 case + importance 重评分用例 ≥ 3 case。

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
| Sub-Agent 框架 | `smartbutler/agents/` | 🟡 **仅脚手架** | `BaseAgent` + `AgentManager` + `to_langchain_tool()` + 最小 LLM 循环(决定 → 调 tool → 收集,最多 5 轮) + 失败回流 + 重试/超时。**`TestTimeAgent` 是脚手架 agent**(名字里的 "Test" 表示测试脚手架,见 `pyproject.toml` 注释),仅供集成测试验证 agent 协议,**不解决任何领域问题**。**真正的领域 Sub-Agent(`HomeAgent` / `ScheduleAgent` / `SearchAgent`)一个都还没做**——属 Phase 8 任务 |
| Thinking(Reactive) | `smartbutler/thinking/loop/` | ✅ | `ButlerOrchestrator`(LangGraph `StateGraph` + 原生 `ToolNode` + 异步 `AsyncSqliteSaver` checkpointer)+ `decide_node`(唯一业务节点)+ `ButlerPromptBuilder`(系统 prompt + Skill 注入 + **memory_block 注入**)+ `ButlerChatModelAdapter` |
| Thinking(Proactive) | `smartbutler/thinking/proactive/` + `thinking/loop/proactive_loop.py` | ✅ 框架 | `RuleBasedProactiveReasoning`(降级后备,URGENT 主动 / 5min dedup / 其他沉默)+ `ProactiveDecision/Result` + 双循环架构。**真实化见 Phase 6b** |
| 事件总线骨架 | `smartbutler/events/` | 🟡 骨架 | `EventBus` + 设备/语音/定时器事件协议 + `EventNormalizer`(接口)+ `EventTrigger.route()`(接口)+ `AnswerRouter`。**只放协议 + 抽象接口,0 行实现**——真实路由等 Phase 7 |
| 情感层 - Memory | `smartbutler/emotion/memory/` | ✅ 6.1+6.2 P0 | `ShortTermMemory`(`AsyncSqliteSaver` 持久化 + 降级 `InMemorySaver`)+ `LongTermStore`(接 `QdrantStorage` 向量检索 + range query 服务端过滤)+ **`MemoryFacade`**(thinking 唯一入口 `remember`/`recall`/`format_for_prompt`)+ `MemoryHooks`(4 事件 + 异常隔离)+ `MemoryRetriever`(Embedder + Qdrant + 时间衰减重排)+ `ImportanceScorer`(启发式,P0 不调 LLM) |
| 情感层 - Personality | `smartbutler/emotion/personality/` | ⏳ Phase 6.6 | 当前空目录,只占位 |
| 情感层 - Learn | `smartbutler/emotion/learn/` | ⏳ Phase 6.7 | 当前空目录,只占位 |
| Skill 运行时 | `smartbutler/thinking/skills/` | ✅ Phase 5 | `SkillRuntime` + `Scanner` + 6 个文件工具 + 3 类路径 × 3 类模式权限(`allow`/`deny`/`interrupt`)+ `prompt_renderer` + `skill_admin` |
| Skills 资源 | `smartbutler/skills/` | ✅ | 内置 skill 目录(`builtin/`)+ 用户级目录(`~/.smartbutler/skills/`,不存在静默跳过) |

### 2.6 测试统计(实测,2026-10-10)

| 类别 | 数量 | 启用方式 | 覆盖 |
|------|------|----------|------|
| **单元测试** | **545** ✅ 全过 | `pytest tests/unit`(默认全跑) | capabilities(llm/tools/embedding/langmem)+ agents + thinking(loop/proactive/skills)+ emotion(memory)+ storage + config + logging |
| **集成测试** | **5** | `pytest -m integration`(默认 skip) | TestTimeAgent 端到端(2)+ LLM 实时连通(3) |
| **E2E 测试** | **24** | `pytest -m e2e`(默认 skip) | LLM 流式连通(14)+ **memory + thinking 端到端 10**(Phase 6.2 P0 新增) |

> 数字实测命令:
> ```bash
> pytest tests/unit --collect-only        # → 545 tests collected
> pytest tests/integration --collect-only # → 5
> pytest tests/e2e --collect-only        # → 24
> ```
> 每次合入新 PR 后**务必重新跑一遍**更新本节。

### 2.7 下次开工的待办(按 phase 排序)

- [ ] **Phase 6.3:Memory 4 层分片 + 固化 + 遗忘 + vacuum**(~1060 行,~2-3 周)—— `emotion/memory/levels.py`(4 层 tag 规则)+ `consolidation.py`(L1/L2/L3 升格管道 + LLM 评分)+ `forgetting.py`(5 类遗忘 + 软删除 + 恢复)+ `summarizer.py`(5W1H 提炼 + LLM 摘要)+ `task_boundary.py`(短期 task 切分)+ `vacuum.py`(双时间窗 7+30 天 vacuum + VacuumReport)+ `archive_store.py`(archived SQLite 备份表)+ `scripts/memory_cli.py`(手动 CLI 入口)+ facade 加 6 个新 API(`consolidate` / `forget` / `restore` / `list_deleted` / `purge_now` / `vacuum_stats`)。**核心交付**:① 管家说"上周你订了去北京的票"(L2 weekly 召回)② 30 天前的开灯琐事自动软删(TTL)③ `python -m smartbutler.scripts.memory_cli vacuum --dry-run` 手动清软删,容量稳态只多 400 条(3MB)。**详细设计**: `docs/PHASE_6.2-6.5_TECHNICAL_DESIGN.md` §2.5.1(2026-10-10 敲定,含 §2.5.1.16 vacuum 治理)。**触发条件**:6.2 P0 跑通 ✅ + 用户产生"记太多/记错"反馈。
- [ ] **Phase 6.6:Personality**(~200 行,~1 周)—— 静态默认 traits + injector 拼 system_prompt + 接受用户显式指令。**不依赖 6.3+**——空壳 personality 独立可测。
- [ ] **Phase 6.7:Learn**(~200 行,~1 周)—— 显式/隐式反馈事件流 + 反馈→trait 映射规则 + 触发 `personality.evolve()`。**严格不实现"自动生成 skill"**。
- [ ] **Phase 6b:ProactiveLoop 真实化**(~2-3 周,**必须等 Phase 6 完成后启动**)—— `LLMProactiveReasoning` 接 Memory + Persona + LLM 推理;`RuleBasedProactiveReasoning` **永不被删**(安全网,URGENT 事件 LLM 不可用时也要能主动开口)。
- [ ] **Phase 7 真实实现**(~3-4 周)—— `EventNormalizer` 真实实现 + `EventTrigger.route()` 真实实现 + 至少 1 个设备适配器(建议先做 `timer.remind`)+ 架构不变量 #7/#8 单测钉死。
- [ ] **Phase 8 预研**(不启动)—— `HomeAgent` 接入 HomeAssistant 的可行性,**仅在 Phase 7 + 真实 HA 环境就绪后**启动。

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
              ├─ delegate_to_home_agent       → HomeAgent (Phase 8, Haiku)
              ├─ delegate_to_schedule_agent   → ScheduleAgent (Phase 8, Haiku)
              ├─ delegate_to_search_agent     → SearchAgent (Phase 8, Haiku)
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
# 设了则切服务模式(Phase 8+ 部署到中心化机器时用)

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
