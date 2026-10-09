# SmartButler

> 有性格、有记忆、懂上下文的通用智能体。
> 设计文档见 [`TECHNICAL_DESIGN.md`](./TECHNICAL_DESIGN.md)。

## 当前进度

> 实施阶段对应 [`TECHNICAL_DESIGN.md` §1.4](./TECHNICAL_DESIGN.md)，按 phase 落地。
> 每完成一阶段回来更新本节（commit 一起提交，不要拖）。

### 实施阶段

| 阶段 | 模块 | 状态 | 主要交付 | 关联 ADR |
|------|------|------|----------|----------|
| **Phase 1** | 基础设施 + LLM | ✅ | Pydantic Settings / structlog / BaseStorage / BaseLLM（双后端 http+langchain） | ADR-003 |
| **Phase 2** | Tool 能力层 | ✅ | BaseTool + ToolRegistry + Decorator + LangChain Adapter + 2 个 common tool | ADR-002 |
| **Phase 3** | Sub-Agent 层 | ✅ | BaseAgent + AgentManager + `TestTimeAgent` 示例 | ADR-005 |
| **Phase 4** | LangGraph Loop + Supervisor | ✅ | StateGraph + ToolNode + Checkpointer + ButlerPromptBuilder（Skill 注入点占位） | ADR-005 |
| **Phase 4b** | ProactiveLoop 框架 | ✅ | 框架 + `ProactiveReasoning` 降级后备 + 沉默默认 + 最小可用场景 | 硬编码实现 | ADR-009 |
| **Phase 5** | Skill loader | ✅ | SKILL.md → 扫描解析 → 注入 Butler system prompt + 6 个文件工具 + 3 类权限 | ADR-006 / ADR-007 |
| **Phase 6** | 情感层 | 🟡 **6.1 已交付** | 6.1: Memory 长短期落地（SQLiteStorage + QdrantStorage 嵌入式 + ShortTermMemory/SqliteSaver + LongTermStore）。6.2-6.7 待办见下表 | - |
| **Phase 6b** | ProactiveLoop 真实化 | ⏳ **待 Phase 6 完成** | 降级后备 → 接 Memory + Persona + LLM 推理 | 从硬编码判断是否主动建议改成通过Memory/Personality + LLM判断 | ADR-009 |
| **Phase 7** | 事件驱动 + 路由 | 🟡 骨架 | 协议 + 抽象接口已就位（`smartbutler/events/`）；**0 行实现**——EventNormalizer / EventTrigger.route() 真实路由 / 设备适配器全部待补 | ADR-008 / ADR-009 |
| **Phase 8** | 核心 Sub-Agent | ⏳ | HomeAgent（接 HomeAssistant）/ ScheduleAgent / SearchAgent 等 | - |
| **Phase 9** | 多模态感知 | ⏳ | ASR + TTS + Vision | - |
| **Phase 10** | 主动服务 | ⏳ | 摄像头 / 麦克风监听 / 计划任务（与 Phase 7 事件总线联动） | - |
| **Phase 11** | 性格演化 + 反馈学习 | ⏳ | - | - |

### Phase 6 子阶段拆分（Memory → Personality → Learn 顺序）

> **修订说明（2026-10-09）**：原 Phase 6 一句话覆盖 Personality + Memory。
> 经讨论拆为 7 个子阶段，**先 Memory 后 Personality 最后 Learn**——
> Memory 是 Personality 的输入数据源（"管家记了用户偏好"才能调性格），
> Learn 是 Personality 的演化器（"管家看了反馈才能改性格"）。
> **严格串行**——每阶段必须前一阶段跑通单测 + 集成测试后才启动。

| 子阶段 | 模块 | 主要交付 | 自研量 | 关键依赖 | 启动前置 |
|--------|------|----------|--------|----------|----------|
| **6.1** | Memory:长短期落地 | ✅ | `storage/sqlite.py`（SQLiteStorage,BaseStorage 实现）+ `storage/qdrant.py`（QdrantStorage,BaseStorage 实现 + 嵌入式默认,服务模式 url 切）+ `emotion/memory/short_term.py`（ShortTermMemory,SqliteSaver 持久化 + 降级 InMemorySaver）+ `emotion/memory/long_term.py`（LongTermStore,接 QdrantStorage + 留 langmem Phase 6.2 接口）+ 31 个新单测全过 | **~200 行**(sqlite 80 / qdrant 80 / memory 40) | `qdrant-client` + `langmem`（**pip 装**） | Phase 5 完工 ✅ |
| **6.2** | Memory:重要性评分 + 检索 | `emotion/memory/importance.py`(LLM 评 0-1 重要性) + `emotion/memory/retrieval.py`(语义检索 + 元数据过滤 + 置信度衰减) | **~100 行** | langmem `store.search` | 6.1 ✅ 完工 |
| **6.3** | Memory:固化 + 遗忘 | `emotion/memory/consolidation.py`(短期→长期 promote,3 触发器:频率/时间/重要性) + `emotion/memory/forgetting.py`(TTL + 主动遗忘 API) | **~150 行** | LangGraph node 拼 consolidation | 6.2 跑通 |
| **6.4** | Memory:层次化总结 | `emotion/memory/summarizer.py`(日/周/月 3 层 hierarchy,贴近人脑"主观意识固化") + `emotion/memory/episodic.py`(情景记忆,SQLite JSON 存对话轨迹) | **~250 行** | LangGraph checkpointer history | 6.3 跑通 |
| **6.5** | Memory:能力补全 | 习惯(habit)雏形 + 跨 thread 状态 + 固化策略调优;**为 Phase 6b/Phase 11 留 hook** | **~150 行** | 6.4 的 summarizer | 6.4 跑通 |
| **6.6** | Personality | `emotion/personality/traits.py`(静态默认 traits) + `state.py`(当前状态) + `injector.py`(trait→system_prompt 片段) + **接受用户显式指令调整**(用户说"你更轻松点"立即改 trait) | **~200 行** | **不依赖 6.5 之前的内容**——空壳 personality 独立可测,只是不会"动" | 6.5 跑通(实际上 6.1 即可并行,但保持串行) |
| **6.7** | Learn | `emotion/learn/feedback_log.py`(显式 👍/👎/隐式沉默/用户指令事件流) + `signal.py`(反馈→personality trait 映射规则) + `evolution_hook.py`(触发 `personality.evolve()`) | **~200 行** | 6.6 personality.evolve() 接口 | 6.6 跑通 |

**Phase 6 总自研量**:~1250 行,约 3-4 周。

**关键不变量（Phase 6 全程必须遵守）**：

1. **memory 严格区分 3 层存储**：短期(线程级)= langgraph-checkpointer + SQLite / 长期事实 = Qdrant(嵌入式起步) / 长期情景 = SQLite JSON。**不允许把"短期"也存 Qdrant**。
2. **Qdrant 默认嵌入式**(`path=data/qdrant`)，**用户不感知**。Phase 8+ 切服务模式时再改 `StorageSettings` 即可。
3. **personality 永远不直接调 LLM**——只接受显式 user 指令 + learn 触发的 trait 调整。**Personality 是数据,不是 Agent**。
4. **learn 严格不"自动生成 skill"**——skill 生成是 self-evolving agent 范畴,属 Phase 12+ 候选,Phase 6 只管"反馈通道"。

### 已知风险（Phase 6 上半段要盯）

- **R1（langmem 装不上）**：langmem 当前还在 beta,API 可能变。**降级方案**:langmem 装不上时,`long_term.py` 退化为**直接调 qdrant-client + 自己写抽事实 prompt**——多 100 行代码,但能跑。
- **R2（Qdrant 嵌入式性能）**：嵌入式 Qdrant 在 Windows 上有 fd 泄漏报告(issue #1234)。**降级方案**:若出现,改用 `QdrantClient(url="http://localhost:6333")` + `docker run`。
- **R3（consolidation 触发频率）**：频率/时间/重要性 3 触发器容易"过度固化"（什么都被记住）或"欠固化"（该记的没记）。**Phase 6.3 单测必须覆盖 3 触发器各 5 个 case**。

### 当前已交付的代码模块

| 模块 | 路径 | 状态 | 说明 |
|------|------|------|------|
| 配置 | `smartbutler/config/` | ✅ | Pydantic Settings 子模块化（LLM / Storage / Logging / Agent） |
| 日志 | `smartbutler/utils/logging.py` | ✅ | structlog 结构化日志 |
| 存储接口 | `smartbutler/storage/` | ✅ | BaseStorage Protocol + Phase 6.1 落地的 SQLiteStorage(aiosqlite 异步) + QdrantStorage(嵌入式 path= 默认,服务模式 url= 切换) |
| LLM 能力 | `smartbutler/capabilities/llm/` | ✅ | BaseLLM 抽象 + `OpenAICompatibleLLM`（httpx 直调，**默认**） + `LangChainLLMAdapter`（env 切 `backend=langchain`）。覆盖 DeepSeek/Qwen/OpenRouter/Azure 兼容模式。已实测端到端连通 |
| Tool 能力 | `smartbutler/capabilities/tools/` | ✅ | BaseTool + ToolRegistry + Decorator + LangChain Adapter + 2 个 common tool（`get_current_time` / `web_fetch`） |
| Sub-Agent | `smartbutler/agents/` | ✅ | BaseAgent + AgentManager + `TestTimeAgent`；`to_langchain_tool()` 暴露 `delegate_to_test_time_agent`；最小 LLM 循环（decide → tool → 收集，最多 5 轮）+ 失败回流 + 重试/超时 |
| Thinking（Reactive） | `smartbutler/thinking/loop/` | ✅ | ButlerOrchestrator（LangGraph `StateGraph` + 原生 `ToolNode` + `InMemorySaver` checkpointer）+ `decide_node`（唯一业务节点，LLM 推理 + 迭代上限防御）+ `ButlerPromptBuilder`（系统 prompt + Skill snippets 注入点）+ `ButlerChatModelAdapter`（`BaseLLM` → LangChain 适配） |
| Thinking（Proactive） | `smartbutler/thinking/proactive/` + `thinking/loop/proactive_loop.py` | ✅ | ProactiveReasoning（**降级后备**——硬编码 4 条规则：URGENT 主动、5 分钟 dedup、其他沉默；用于 LLM 故障时降级）+ ProactiveDecision/Result + EventTrigger（明确区分 MessageIngress vs EventSource） + 双循环架构 EventTrigger.route() 显式路由。**真实化（接 Memory + Persona + LLM 推理）见 Phase 6b**。详见 [§5.9](./TECHNICAL_DESIGN.md) + [`smartbutler/thinking/ADR-009-proactive-reactive-dual-loop.md`](./smartbutler/thinking/ADR-009-proactive-reactive-dual-loop.md) |
| 事件总线骨架 | `smartbutler/events/` | 🟡 骨架 | EventBus + 设备/语音/定时器事件协议 + EventNormalizer（**接口**）+ EventTrigger.route()（**接口**）+ AnswerRouter。**只放协议 + 抽象接口，0 行实现**——真实路由逻辑等 Phase 7 启动时填充。详见 [§5.8](./TECHNICAL_DESIGN.md) + [`smartbutler/events/ADR-008-event-driven.md`](./smartbutler/events/ADR-008-event-driven.md) |
| 情感层 - Memory | `smartbutler/emotion/memory/` | 🟡 6.1 已交付 | ShortTermMemory（SqliteSaver 持久化 langgraph checkpointer + 降级 InMemorySaver）+ LongTermStore（接 QdrantStorage 向量检索 + 留 langmem Phase 6.2 接口）。详见 README Phase 6 子阶段拆分 |

### 测试统计（实测）

| 类别 | 数量 | 启用方式 |
|------|------|----------|
| 单元测试 | **414**（Phase 6.1 起 +31）| `pytest tests/unit`（默认全跑） |
| 集成测试 | **5** | `pytest -m integration`（默认 skip） |
| E2E 测试 | **5** | `pytest -m e2e`（默认 skip） |

> 上述数字基于 `tests/unit/**/*.py` 函数体数 + `pytest -m integration` / `pytest -m e2e` 收集结果实测。
> 每次合入新 PR 后**务必重新跑一遍**更新本节，不要等季度复盘。

### 下次开工的待办（按 phase 排序）

> 这里只列**下一站**开始的工作，**不要**塞进所有远期任务。

- [x] **Phase 5a：Skill loader**（1-2 周）—— `smartbutler/skills/builtin/` 目录 + `SkillRuntime.from_settings()` 一行装配 + 6 个文件工具(`read_file`/`write_file`/`edit_file`/`delete_file`/`ls`/`grep`/`glob`)+ 3 类路径 × 3 类模式权限(`allow`/`deny`/`interrupt`)+ 55 个单测 + 1 端到端集成测试。详见 TECHNICAL_DESIGN.md §5.7.6。
- [x] **Phase 6.1：Memory 长短期落地**（~200 行,详见 6.1 行说明）—— `SQLiteStorage` + `QdrantStorage` 嵌入式 + `ShortTermMemory`(SqliteSaver) + `LongTermStore`(Qdrant 检索) + 31 个新单测全过
- [ ] **Phase 6.2-6.5：Memory**（2-3 周）—— 严格串行：6.2 重要性评分 + 检索 → 6.3 固化 + 遗忘 → 6.4 层次化总结 → 6.5 能力补全（habit 雏形 + hook 预留）。**预计自研 ~650 行**。详见"Phase 6 子阶段拆分"。
- [ ] **Phase 6.6：Personality**（1 周）—— 静态默认 traits + injector 拼 system_prompt + 接受用户显式指令（"你更轻松点"立即改 trait）。**不依赖 6.5 的动态数据**——空壳 personality 独立可测。
- [ ] **Phase 6.7：Learn**（1 周）—— 显式/隐式反馈事件流 + 反馈→trait 映射规则 + 触发 `personality.evolve()`。**严格不实现"自动生成 skill"**——skill 生成是 self-evolving agent 范畴,属 Phase 12+ 候选。
- [ ] **Phase 6b：ProactiveLoop 真实化**（2-3 周，**必须等 Phase 6 完成后启动**）—— `RuleBasedProactiveReasoning` 是降级后备永久保留，**不是** Phase 6b 完成后要删的"占位"。真实化版 = `LLMProactiveReasoning`：调 LLM 综合判断 + 读 Personality + 查 Memory + 5 分钟内同 topic dedup。LLM 故障/timeout/key 失效时降级到 `RuleBasedProactiveReasoning`——URGENT 事件必须能在 LLM 不可用时主动开口。
- [ ] **Phase 7 真实实现**（与 Phase 5a 串行，3-4 周）—— EventNormalizer 真实实现（设备原始消息 → BaseEvent）+ EventTrigger.route() 真实实现（user → Reactive，设备/定时器 → Proactive）+ 至少 1 个设备适配器（建议先做 timer.remind，最小可用）+ 架构不变量 #7/#8 单测钉死。
- [ ] **Phase 8 预研**（不启动）—— HomeAgent 接入 HomeAssistant 的可行性，**仅**在 Phase 7 真实实现 + 真实 HomeAssistant 环境就绪后才启动。

### 部署后优化（Phase 4 收尾，跨 phase 待办）

> 部署场景：管家在家中部署，全家共享唯一一个实例，无并发问题。
> 这些是**横切关注点**——任何 phase 落地时都可以顺手补，不强求在某个 phase 内集中做完。

**待实现**：

- [ ] 8.1 Tool 重试 & 熔断（单 tool 粒度）
- [ ] 8.2 LLM 重试（限流 / 超时，exponential backoff + jitter）
- [ ] 8.3 流式输出（token 级，astream_events）
- [ ] 8.4 错误分类 & 友好回复（ButlerErrorKind 枚举 → 文案映射）
- [ ] 8.5 可观测性（ButlerCallTrace → structlog）

**不做**（家用场景不需要）：

- ~~并发安全~~ / ~~多 LLM 后端切换~~ / ~~Postgres Checkpointer~~ / ~~OpenTelemetry / Prometheus 上报~~

## 架构约束（实现时必须遵守）

1. **能力层自建抽象**：定义我们自己的 `BaseLLM` / `BaseTool`，不直接 import LangChain 的 `BaseChatModel` / `BaseTool`。✅ 已落地：`BaseLLM` + 两个实现——`OpenAICompatibleLLM`（httpx 直调）和 `LangChainLLMAdapter`（包装 `ChatOpenAI`，但所有 LangChain 类型仅在 adapter 内部出现，业务层零感知）。
2. **LLM 实现后端可切换**：`SMARTBUTLER_LLM_BACKEND=http|langchain`。默认 `http`（保留原有行为，已有的 e2e 测试无需任何修改）；切到 `langchain` 时由 LangChain 负责消息转换 / 工具绑定 / 流式 chunk 处理 / reasoning_content 透传 / structured output。两条路线对外都是 `BaseLLM` 接口，业务层零差别。
3. **Manager 是 Agent 唯一入口**：thinking 层**只**通过 `agents/manager/` 找 Agent，不直接 import 具体 Agent 类。
4. **Agent 接口契约固定**：`name / description / tools / ainvoke()` 是必实现方法；外加 `to_langchain_tool()` 用于暴露成 `delegate_to_<name>` LangChain Tool。
5. **LangGraph State 字段固定**：`user_input / messages / current_decision / pending_tasks / tool_results / iteration / context / final_response / is_complete`。
6. **依赖方向**：Interface → Thinking → Emotion → Agents；capabilities 被 Thinking/Agents 消费。✅ LLM 已通过 `create_llm(settings) -> BaseLLM` 暴露接口，便于后续节点消费。
7. **Skill ≠ Sub-Agent（ADR-006）**：Skill 是 **Anthropic Skills 格式的能力包**，由管家（强模型）执行；Sub-Agent 是 **代码实现的领域智能体**，有自己的 LLM（便宜模型）。两者概念独立，禁止混淆。
8. **Multi-Agent 走 LangGraph Supervisor + Tool-Calling（ADR-005）**：管家作为中央调度器，通过 LangChain `StructuredTool` 机制调用 Sub-Agent；不使用 `create_supervisor` 高层封装，保留性格注入 / 记忆检索等定制空间。
9. **ProactiveReasoning 降级契约（ADR-009）**：ProactiveLoop 的 `ProactiveReasoning` 必须**双轨部署**——`LLMProactiveReasoning`（主路，Phase 6b 实现）+ `RuleBasedProactiveReasoning`（降级后备，已落库）。**任何 LLM 故障（服务挂、key 失效、timeout、解析失败）必须降级到后备**，URGENT 事件（漏水/烟雾/门铃等安全相关）不依赖 LLM 也能主动开口。`RuleBasedProactiveReasoning` **永不被删**——它是安全网，不是临时占位。

## LLM 双后端决策（Phase 1.5）

### 决策结论

**保留自建 `OpenAICompatibleLLM`（默认），并新增 `LangChainLLMAdapter` 作为可选 backend**。两者都实现 `BaseLLM` 接口，工厂 `create_llm()` 按 `SMARTBUTLER_LLM_BACKEND` 路由，业务层完全无感。

### 为什么这么做

| 维度 | 自建 HttpLLM（默认） | LangChain Adapter（可选） |
|---|---|---|
| **首版交付速度** | 慢 | 快 |
| **依赖体积** | 极小 | 较大（langchain-core + langchain-openai） |
| **协议可控性** | 全栈可调试 | 栈深 |
| **流式 tool_calls** | ❌（当前实现未做） | ✅ LangChain 内部处理 |
| **reasoning_content 透传** | ⚠️ 已在 StreamChunk 暴露 | ✅ LangChain 通过 additional_kwargs 透传 |
| **structured output** | ❌（需手写 response_format） | ✅ `with_structured_output` 现成 |
| **后续 Tool 层 / Loop 层的复用** | 协议转换要自写 | LangChain 原生 @tool / LangGraph 直接用 |
| **升级成本** | 自维护 | LangChain 版本风险（0.3.x → 1.x） |
| **可调试性** | 栈浅（httpx 直连） | 栈深（多层包装） |

### 取舍逻辑

1. **不替换**：自建实现已经覆盖 80% 场景（chat / stream / tool_calls / reasoning / extra_body 透传），切到 LangChain 等于把已经验证过的代码扔掉重写，迁移成本高且没有显著收益。
2. **不单选 LangChain**：完全抛弃自建实现意味着放弃对协议栈的完全控制，依赖 LangChain 的版本演进节奏。
4. **不推迟到 Loop 阶段再做**：Loop 阶段一旦要做 tool_calls 循环、tool schema 转换、消息聚合，再切 LangChain adapter 会改动 LangGraph 节点以外的更多地方，提前做更划算。
5. **保持 Backend 切换零成本**：`create_llm(settings)` 一行调用既可切到 LangChain，无需改任何业务代码。

### 不变量

- 现有 60 个单测（特别是 `tests/e2e/test_streaming.py` 与 `test_openai_compatible.py`）**零行修改** 全部通过（实测 84/84 ✅）。
- `BaseLLM` 公开接口（`chat / chat_stream / aclose`）保持不变。
- `LLMResponse / StreamChunk / ToolSpec / ToolCall / Message` 数据类型保持不变。
- 业务层只依赖 `BaseLLM` 与上述数据类型，不感知具体实现。

### 何时切到 `backend=langchain`

- 需要 LangChain 原生 `@tool` 装饰器 / LangGraph 工具节点 → 切换
- 需要 `with_structured_output` 强结构化输出 → 切换
- 需要 LangSmith 监控、LangServe 部署 → 切换
- 仅做基础 chat / stream / 工具调用 → 保持默认 `http`

### 配置示例

```dotenv
# 默认配置(httpx 直调,保留所有现有行为)
SMARTBUTLER_LLM_PROVIDER=openai
SMARTBUTLER_LLM_BACKEND=http
SMARTBUTLER_LLM_MODEL=deepseek-flash
SMARTBUTLER_LLM_OPENAI_API_KEY=sk-...
SMARTBUTLER_LLM_OPENAI_BASE_URL=https://api.deepseek.com

# 切到 LangChain 适配器(后续 Tool / Loop 阶段建议)
SMARTBUTLER_LLM_BACKEND=langchain
```

## 多 Agent + Skill 架构（Phase 3-5 落地）

> 完整决策记录见 [`TECHNICAL_DESIGN.md`](./TECHNICAL_DESIGN.md) §5.5 / §5.6 / §5.7。

### Sub-Agent（代码实现的领域智能体）

- 形态：Python 类继承 `BaseAgent`
- 执行方：**Sub-Agent 自己的 LLM**（便宜模型，如 Haiku）
- 触发方式：管家 LLM 通过 `delegate_to_<name>` tool call 调用
- 典型：HomeAgent（设备控制）、ScheduleAgent（日程）、SearchAgent（搜索）
- 适用场景：需要**状态机**、**副作用管理**、**独立推理**的任务

### Skill（Anthropic Skills 格式能力包）

- 形态：文件夹 + `SKILL.md`（YAML frontmatter + markdown body）
- 执行方：**管家 LLM**（强模型，自己执行）
- 触发方式：管家 LLM 看到 skill 注入的 system_prompt 后自主决定是否用
- 典型：`pdf-summary/`、`stock-analysis/`、亲属拖入的 `~/.smartbutler/skills/*/`
- 适用场景：**强模型 + 步骤提示 + 工具**就能完成的任务

### 协作模式：LangGraph Supervisor + Tool-Calling

```
用户消息 → LangGraph Loop (管家 LLM 推理)
              ↓ 工具集
              ├─ delegate_to_test_time_agent → TestTimeAgent (Phase 3)
              ├─ delegate_to_home_agent  → HomeAgent (Phase 8,Haiku)
              ├─ delegate_to_schedule_agent → ScheduleAgent (Phase 8,Haiku)
              ├─ delegate_to_search_agent → SearchAgent (Phase 8,Haiku)
              ├─ pdf_extract (Skill 工具，管家直接调)
              ├─ get_today_digest (管家元工具)
              └─ ... (其他 Skill 工具,管家直接调)
```

**核心**：Sub-Agent 通过 LangChain `StructuredTool` 机制暴露；管家 Loop 用 LangGraph 原生 `StateGraph + ToolNode` 编排。

### Sub-Agent vs Skill 边界

| 维度 | Sub-Agent | Skill |
|------|-----------|-------|
| 触发方 | 管家 LLM tool_call | 管家 LLM 看 system_prompt 自主判断 |
| 执行方 | Sub-Agent 自己的 LLM | 管家 LLM |
| 维护者 | 开发者 | 任何人（含亲属） |
| 修改代码 | 需要 | **不需要** |

**判断依据**：需要状态机 / 副作用 / 独立推理 → Sub-Agent；强模型 + 步骤提示就能搞定 → Skill。

---

## 快速开始

```bash
# 1. 安装依赖（含开发依赖）
python -m pip install -e ".[dev]"

# 2. 跑单元测试（84 用例,默认全跑）
pytest tests/unit

# 3. 跑集成测试（需 .env 已填 API key）
pytest -m integration

# 4. Lint + 类型检查
ruff check .
mypy smartbutler
```

## 配置

所有配置通过环境变量（或项目根目录的 `.env` 文件）注入。子模块前缀：

| 前缀 | 模块 |
|------|------|
| `SMARTBUTLER_LLM_` | LLM 子模块 |
| `SMARTBUTLER_STORAGE_` | 存储子模块 |
| `SMARTBUTLER_LOGGING_` | 日志子模块 |
| `SMARTBUTLER_AGENT_` | Agent 子模块 |

最小 `.env` 示例：

```dotenv
SMARTBUTLER_LLM_PROVIDER=openai
SMARTBUTLER_LLM_MODEL=gpt-4o-mini
SMARTBUTLER_LLM_OPENAI_API_KEY=sk-xxx

SMARTBUTLER_LOGGING_LEVEL=INFO
SMARTBUTLER_LOGGING_JSON_OUTPUT=false
```

## 开发规范

- 类型注解：所有公共接口必须有完整类型注解（mypy strict）。
- 日志：业务模块统一通过 `from smartbutler.utils import get_logger` 获取 logger，禁止 `print`。
- 配置：业务模块禁止读取环境变量，必须通过 `smartbutler.config.load_xxx_settings()` 获取。


## 测试用例

- LLM对话调用：pytest -m e2e tests/e2e/test_streaming.py::test_ask_and_log[model-info] -v -s