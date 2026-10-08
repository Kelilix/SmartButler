# SmartButler

> 有性格、有记忆、懂上下文的通用智能体。
> 设计文档见 [`TECHNICAL_DESIGN.md`](./TECHNICAL_DESIGN.md)。

## 当前进度

| 层级 | 状态 | 备注 |
|------|------|------|
| `smartbutler/config/` | ✅ 基础设施层 | Pydantic Settings 子模块化配置（LLM / Storage / Logging / Agent） |
| `smartbutler/utils/logging.py` | ✅ 基础设施层 | structlog 结构化日志 |
| `smartbutler/storage/` | ✅ 基础设施层（接口） | BaseStorage Protocol；具体后端按需实现 |
| `smartbutler/capabilities/llm/` | ✅ 能力层（LLM 双后端） | BaseLLM 抽象 + **两个可切换实现**：① `OpenAICompatibleLLM`（httpx 直调，**默认 backend**）；② `LangChainLLMAdapter`（包装 `langchain-openai.ChatOpenAI`，env 切 `backend=langchain` 启用）。覆盖 DeepSeek/Qwen/OpenRouter/Azure 兼容模式。**已实测端到端连通**（chat / stream / tool_calls 三个 e2e 用例通过，且 60→84 个单测全绿） |
| `smartbutler/capabilities/tools/` | ✅ 已完成 | BaseTool + ToolRegistry + Decorator + LangChain Adapter + 2 个 common tool（`get_current_time` / `web_fetch`）；92 单测全绿 |
| `smartbutler/capabilities/memory/` | ⏳ 待开发 | 远期：长期记忆存储与检索 |
| `smartbutler/agents/` | ✅ 已完成 | **Phase 3**：BaseAgent + AgentManager + `TestTimeAgent`（注册 `get_current_time` 全局 tool，验证 Sub-Agent→BaseTool 路径）；`to_langchain_tool()` 暴露 `delegate_to_test_time_agent`；最小 LLM 循环（decide → tool → 收集，最多 5 轮）+ 失败回流到 LLM + 重试/超时。**40 agent 单测全绿 + 1 集成测试**（默认 skip，`-m integration` 启用） | ADR-005 |
| `smartbutler/skills/` | ⏳ 待开发 | **Phase 5**：Anthropic Skills loader，SKILL.md → 能力包（详见 §3.2.6 + ADR-006/007） |
| `smartbutler/thinking/` | ✅ 已完成 | **Phase 4**：ButlerOrchestrator（LangGraph `StateGraph` + 原生 `ToolNode` + `InMemorySaver` checkpointer）+ `decide_node`（唯一业务节点，LLM 推理 + 迭代上限防御）+ `ButlerPromptBuilder`（系统 prompt + Skill snippets 注入点，Phase 5 占位）+ `ButlerChatModelAdapter`（`BaseLLM` → `langchain_core.BaseChatModel`，复用 Phase 1 双后端）。**24 thinking 单测全绿 + 3 E2E 用例**（默认 skip，`-m e2e` 启用）走通"用户 → 管家 LLM → delegate_to_test_time_agent → TestTimeAgent → get_current_time → 反馈用户"完整链路 | ADR-005 |
| `smartbutler/emotion/` | ⏳ 待开发 | **Phase 6**：Personality + Memory |
| `smartbutler/events/` | ✅ 骨架已就位 | **Phase 7**：事件驱动（被动触发）—— EventBus + 设备/语音/定时器事件协议 + EventNormalizer + EventTrigger + AnswerRouter。**Phase 7 当前只放协议 + 抽象接口 + ADR，0 行实现代码**（待 Phase 7 真正启动时填充）。详见 [§5.8](./TECHNICAL_DESIGN.md) + [`smartbutler/events/ADR-008-event-driven.md`](./smartbutler/events/ADR-008-event-driven.md) | ADR-008 |
| `smartbutler/core_sub_agents/` | ⏳ 待开发 | **Phase 8**：核心业务 Sub-Agent（HomeAgent + HomeAssistant 接入 / ScheduleAgent / SearchAgent 等）。仅 Phase 7 + 真实 HomeAssistant 环境就绪后启动；具体技术方案到时再定 | - |
| `smartbutler/interface/` | ⏳ 待开发 | HTTP / WebSocket / CLI / MCP |

**测试统计**：201 单元测试（其中 40 个 Phase 3 agent 测试）+ 2 集成测试 + 5 e2e 流式测试（默认 skip，需 `pytest -m integration` / `-m e2e` 启用）

### Phase 4 之后 — 部署后优化（待办回填）

> 部署场景：管家在家中部署，全家共享唯一一个实例，无并发问题。
> 基础功能（Phase 1 ~ Phase 4）已能跑通，下面列出**尚未实现**的优化项，下次回到项目时一目了然。

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