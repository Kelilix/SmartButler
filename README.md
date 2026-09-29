# SmartButler

> 有性格、有记忆、懂上下文的通用智能体。
> 设计文档见 [`TECHNICAL_DESIGN.md`](./TECHNICAL_DESIGN.md)。

## 当前进度

| 层级 | 状态 | 备注 |
|------|------|------|
| `smartbutler/config/` | ✅ 基础设施层 | Pydantic Settings 子模块化配置（LLM / Storage / Logging / Agent） |
| `smartbutler/utils/logging.py` | ✅ 基础设施层 | structlog 结构化日志 |
| `smartbutler/storage/` | ✅ 基础设施层（接口） | BaseStorage Protocol；具体后端按需实现 |
| `smartbutler/capabilities/llm/` | ✅ 能力层（LLM 已就绪） | BaseLLM 抽象、OpenAI 兼容实现（DeepSeek/Qwen/OpenRouter）、工厂路由；**已实测端到端连通**（chat / stream / tool_calls 三个集成用例通过） |
| `smartbutler/capabilities/tools/` | ⏳ 待开发 | 下一阶段：BaseTool 抽象 + ToolRegistry |
| `smartbutler/capabilities/memory/` | ⏳ 待开发 | 远期：长期记忆存储与检索 |
| `smartbutler/agents/` | ⏳ 待开发 | 再下一阶段：Base / Manager / 领域 Agent |
| `smartbutler/thinking/` | ⏳ 待开发 | LangGraph Loop + Reasoning / Decision |
| `smartbutler/emotion/` | ⏳ 待开发 | Personality / Memory |
| `smartbutler/interface/` | ⏳ 待开发 | HTTP / WebSocket / CLI / MCP |

**测试统计**：60 单元测试 + 3 集成测试（默认 skip，需 `pytest -m integration` 启用）

## 架构约束（实现时必须遵守）

1. **能力层自建抽象**：定义我们自己的 `BaseLLM` / `BaseTool`，不直接 import LangChain 的 `BaseChatModel`。✅ 已落地：`BaseLLM` + `OpenAICompatibleLLM`（httpx 直调，无 OpenAI SDK 依赖）。
2. **Manager 是 Agent 唯一入口**：thinking 层**只**通过 `agents/manager/` 找 Agent，不直接 import 具体 Agent 类。
3. **Agent 接口契约固定**：`name / description / tools / handle()` 是必实现方法。
4. **LangGraph State 字段固定**：`user_input / messages / current_decision / pending_tasks / tool_results / iteration / context / final_response / is_complete`。
5. **依赖方向**：Interface → Thinking → Emotion → Agents；capabilities 被 Thinking/Agents 消费。✅ LLM 已通过 `create_llm(settings) -> BaseLLM` 暴露接口，便于后续节点消费。

## 快速开始

```bash
# 1. 安装依赖（含开发依赖）
python -m pip install -e ".[dev]"

# 2. 跑单元测试（60 用例,默认全跑）
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
