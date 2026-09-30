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
| `smartbutler/capabilities/tools/` | ⏳ 待开发 | 下一阶段：BaseTool 抽象 + ToolRegistry |
| `smartbutler/capabilities/memory/` | ⏳ 待开发 | 远期：长期记忆存储与检索 |
| `smartbutler/agents/` | ⏳ 待开发 | 再下一阶段：Base / Manager / 领域 Agent |
| `smartbutler/thinking/` | ⏳ 待开发 | LangGraph Loop + Reasoning / Decision |
| `smartbutler/emotion/` | ⏳ 待开发 | Personality / Memory |
| `smartbutler/interface/` | ⏳ 待开发 | HTTP / WebSocket / CLI / MCP |

**测试统计**：84 单元测试 + 3 集成测试 + 5 e2e 流式测试（默认 skip，需 `pytest -m integration` / `-m e2e` 启用）

## 架构约束（实现时必须遵守）

1. **能力层自建抽象**：定义我们自己的 `BaseLLM` / `BaseTool`，不直接 import LangChain 的 `BaseChatModel` / `BaseTool`。✅ 已落地：`BaseLLM` + 两个实现——`OpenAICompatibleLLM`（httpx 直调）和 `LangChainLLMAdapter`（包装 `ChatOpenAI`，但所有 LangChain 类型仅在 adapter 内部出现，业务层零感知）。
2. **LLM 实现后端可切换**：`SMARTBUTLER_LLM_BACKEND=http|langchain`。默认 `http`（保留原有行为，已有的 e2e 测试无需任何修改）；切到 `langchain` 时由 LangChain 负责消息转换 / 工具绑定 / 流式 chunk 处理 / reasoning_content 透传 / structured output。两条路线对外都是 `BaseLLM` 接口，业务层零差别。
3. **Manager 是 Agent 唯一入口**：thinking 层**只**通过 `agents/manager/` 找 Agent，不直接 import 具体 Agent 类。
4. **Agent 接口契约固定**：`name / description / tools / handle()` 是必实现方法。
5. **LangGraph State 字段固定**：`user_input / messages / current_decision / pending_tasks / tool_results / iteration / context / final_response / is_complete`。
6. **依赖方向**：Interface → Thinking → Emotion → Agents；capabilities 被 Thinking/Agents 消费。✅ LLM 已通过 `create_llm(settings) -> BaseLLM` 暴露接口，便于后续节点消费。

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