# SmartButler 技术方案

## 1. SmartButler 能力与规划

### 1.1 项目定位

SmartButler（大管家）是一个**通用智能体**，旨在为用户提供拟人化的交互体验。作为一个「有性格、有记忆、懂上下文」的智能助手，它不仅仅是问答机器人，而是一个能够理解用户、记住用户习惯、主动服务的数字伙伴。

### 1.2 核心能力矩阵

#### 1.2.1 感知能力

| 能力 | 描述 | 技术方向 |
|------|------|----------|
| **语音交互** | 支持语音输入和语音输出，实现全双工对话 | ASR（语音识别）+ TTS（语音合成） |
| **视觉理解** | 理解用户发送的图片内容，支持多模态输入 | 多模态 LLM / 专用视觉模型 |
| **自然语言理解** | 深度理解用户意图和语义 | LLM + Intent Classification |

#### 1.2.2 认知能力

| 能力 | 描述 | 技术方向 |
|------|------|----------|
| **推理规划** | 复杂任务的分解与规划 | ReAct / CoT / Tool Learning |
| **工具调用** | 调用各类外部工具完成任务 | Function Calling / Tool Integration |
| **上下文理解** | 维护长程对话上下文 | Context Window Management |
| **反思检查** | 执行结果的自检与修正 | Self-Verification |

#### 1.2.3 情感能力

| 能力 | 描述 | 技术方向 |
|------|------|----------|
| **性格塑造** | 统一的性格人设与表达风格 | Personality System |
| **情感记忆** | 记住用户的偏好、习惯、重要事件 | Long-term Memory |
| **情感表达** | 根据场景生成有温度的回复 | Emotion-aware Generation |
| **性格演化** | 性格随交互逐渐进化 | Reinforcement Learning from Feedback |

#### 1.2.4 执行能力

| 能力 | 描述 | 技术方向 |
|------|------|----------|
| **智能家居控制** | 控制灯光、空调、家电等设备 | IoT Integration |
| **日程管理** | 创建、查询、提醒日程 | Calendar Integration |
| **信息检索** | 搜索互联网或内部知识库 | Search API / RAG |
| **第三方服务** | 调用各类第三方 API | API Gateway |

### 1.3 能力演进规划

#### Phase 1：基础对话能力

- 实现基础的文本对话
- 建立 Agent 循环（思考 → 决策 → 执行 → 回复）
- 接入基础 LLM 能力

#### Phase 2：多模态感知

- 接入语音交互（ASR/TTS）
- 支持图像理解
- 实现多模态融合

#### Phase 3：个性化与记忆

- 建立性格系统
- 实现短期记忆与长期记忆
- 支持用户偏好学习

#### Phase 4：主动服务

- 基于上下文的主动建议
- 智能家居联动
- 日程主动提醒

#### Phase 5：持续进化

- 性格演化机制
- 用户反馈学习
- 能力持续扩展

#### 1.4 实施阶段规划（按模块落地顺序）

> **说明**：1.3 是**产品能力演进**视角（看得到的能力），按用户体验排列。
> 下面 1.4 是**实施阶段**视角（按代码模块落地顺序），与模块代码一一对应。每完成一阶段更新 README "当前进度" 表格。

| 阶段 | 模块 | 状态 | 主要交付 | 关联 ADR |
|------|------|------|----------|----------|
| **Phase 1** | 基础设施 + LLM | ✅ 已完成 | Pydantic Settings / structlog / BaseStorage / BaseLLM（双后端） | ADR-003 |
| **Phase 2** | Tool 能力层 | ⏳ **当前** | BaseTool + ToolRegistry + Decorator + LangChain Adapter | ADR-002 |
| **Phase 3** | Sub-Agent 层 | ✅ 已完成 | BaseAgent + AgentManager + `TestTimeAgent` 示例（注册 capabilities 已有 tool，0 失败） | ADR-005 |
| **Phase 4** | LangGraph Loop + Supervisor | ✅ 已完成 | StateGraph + ToolNode + Checkpointer + Skill Prompt 注入 | ADR-005 |
| **Phase 5** | Anthropic Skills loader + ProactiveLoop 框架 | ⏳ | SKILL.md → 能力包 → 注入 Butler system prompt + 注册 tools；**+ ProactiveLoop 框架**（ProactiveReasoning **降级后备** + 沉默支持 + 最小可用场景）| ADR-006 / ADR-007 / **ADR-009** |
| **Phase 6** | 情感层 | ⏳ | Personality + Memory | - |
| **Phase 6b** | ProactiveLoop 真实化 | ⏳ **待 Phase 6 完成** | 降级后备 → LLM 综合判断 + 读 Memory + 读 Persona + dedup；**降级后备永久保留** | **ADR-009** |
| **Phase 7** | 事件驱动（被动触发）+ Proactive 路由 | 🆕 **当前** | 事件总线 + 设备接入协议 + EventNormalizer + **EventTrigger.route()**（**显式路由**到 Reactive/Proactive） + AnswerRouter（**只放协议 + 抽象接口，Phase 5-6 阶段 0 行实现**）| ADR-008 / **ADR-009** |
| **Phase 8** | 核心业务 Sub-Agent | ⏳ | HomeAgent（接入 HomeAssistant）/ ScheduleAgent / SearchAgent 等。**与事件驱动联动**：HomeAgent 通过 EventBus 订阅 `device.*` 主题。仅 Phase 7 + 真实 HomeAssistant 环境就绪后启动；具体技术方案到时再定 | - |
| **Phase 9** | 多模态感知 | ⏳ | ASR + TTS + Vision（原 Phase 7 顺延）| - |
| **Phase 10** | 主动服务 | ⏳ | 摄像头 / 麦克风监听 / 计划任务（**与 Phase 7 事件总线联动**）| - |
| **Phase 11** | 性格演化、反馈学习 | ⏳ | - | - |

> **修订说明（2026-10-08）**：原"事件驱动"原本排在 Phase 8，按用户决策提前为 Phase 7（独立模块 `smartbutler/events/`，详见 ADR-008）。原"核心 Sub-Agent 落地"原编号 Phase 3.5 取消，并入 Phase 8（HomeAgent 等）；原 Phase 7（多模态）→ Phase 9；原 Phase 8（主动服务）→ Phase 10。Phase 7 骨架已落库（协议 + 抽象接口），实现留到 Phase 7 真正启动时填充。

---

## 2. SmartButler 架构图

### 2.1 整体架构

```
┌─────────────────────────────────────────────────────────────────────────┐
│                              SmartButler                                  │
│                                                                          │
│  ┌─────────────────────────────────────────────────────────────────┐    │
│  │                         接口层 (Interface)                        │    │
│  │  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐       │    │
│  │  │   HTTP   │  │WebSocket │  │   CLI    │  │   MCP    │       │    │
│  │  │   API    │  │  Server  │  │  Tool    │  │ Protocol │       │    │
│  │  └──────────┘  └──────────┘  └──────────┘  └──────────┘       │    │
│  └─────────────────────────────────────────────────────────────────┘    │
│                                    │                                      │
│                                    ▼                                      │
│  ┌─────────────────────────────────────────────────────────────────┐    │
│  │                       能力层 (Capabilities)                       │    │
│  │  ┌─────────┐  ┌─────────┐  ┌─────────┐  ┌─────────┐            │    │
│  │  │   LLM   │  │   ASR   │  │   TTS   │  │  Vision │            │    │
│  │  │  能力   │  │  能力   │  │  能力   │  │  能力   │            │    │
│  │  └────┬────┘  └────┬────┘  └────┬────┘  └────┬────┘            │    │
│  │       └────────────┴────────────┴────────────┘                  │    │
│  │                            │                                       │    │
│  │                      ┌─────┴─────┐                                │    │
│  │                      │   Tools   │                                │    │
│  │                      │  工具层    │                                │    │
│  │                      └───────────┘                                │    │
│  └─────────────────────────────────────────────────────────────────┘    │
│                                    │                                      │
│                                    ▼                                      │
│  ┌─────────────────────────────────────────────────────────────────┐    │
│  │                       思考层 (Thinking)                          │    │
│  │                                                                  │    │
│  │  ┌──────────────────────────────────────────────────────────┐   │    │
│  │  │                      LangGraph Loop                      │   │    │
│  │  │                                                          │   │    │
│  │  │    ┌──────────┐      ┌──────────┐      ┌──────────┐     │   │    │
│  │  │    │ Evaluate │─────▶│  Decide  │─────▶│  Invoke  │     │   │    │
│  │  │    │   评估    │      │   决策   │      │   执行   │     │   │    │
│  │  │    └────┬─────┘      └────┬─────┘      └────┬─────┘     │   │    │
│  │  │         │                 │                  │           │   │    │
│  │  │         │                 │                  │           │   │    │
│  │  │         │◀────────────────┴──────────────────┘           │   │    │
│  │  │         │                    (循环)                        │   │    │
│  │  └─────────┼────────────────────────────────────────────────┘   │    │
│  │            │                                                     │    │
│  │  ┌─────────┴─────────┐                                         │    │
│  │  │     Reasoning     │   推理业务（任务规划、反思）              │    │
│  │  ├───────────────────┤                                         │    │
│  │  │     Decision      │   决策业务（意图识别、路由）              │    │
│  │  ├───────────────────┤                                         │    │
│  │  │     Prompt        │   Prompt 工程（组装、模板）              │    │
│  │  ├───────────────────┤                                         │    │
│  │  │     Context       │   上下文管理（窗口、压缩）                │    │
│  │  └───────────────────┘                                         │    │
│  └─────────────────────────────────────────────────────────────────┘    │
│                                    │                                      │
│                                    ▼                                      │
│  ┌─────────────────────────────────────────────────────────────────┐    │
│  │                       情感层 (Emotion)                           │    │
│  │                                                                  │    │
│  │  ┌────────────────────┐          ┌────────────────────┐        │    │
│  │  │     Personality    │          │      Memory       │        │    │
│  │  │                    │          │                    │        │    │
│  │  │  • 性格特征定义     │          │  • 短期对话记忆    │        │    │
│  │  │  • 当前心情状态     │          │  • 长期事实记忆    │        │    │
│  │  │  • 性格注入器      │          │  • 情感记忆       │        │    │
│  │  │  • 性格演化规则     │          │  • 记忆检索       │        │    │
│  │  └────────────────────┘          └────────────────────┘        │    │
│  └─────────────────────────────────────────────────────────────────┘    │
│                                    │                                      │
│                                    ▼                                      │
│  ┌─────────────────────────────────────────────────────────────────┐    │
│  │                       Agent 层 (Agents)                          │    │
│  │  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐       │    │
│  │  │   Base   │  │   Home   │  │ Schedule │  │  Search  │  ... │    │
│  │  │   基类   │  │  家居    │  │  日程    │  │  搜索    │       │    │
│  │  └──────────┘  └──────────┘  └──────────┘  └──────────┘       │    │
│  └─────────────────────────────────────────────────────────────────┘    │
│                                                                          │
└─────────────────────────────────────────────────────────────────────────┘
```

### 2.2 模块依赖关系

```
┌───────────────────────────────────────────────────────────────────────┐
│                           依赖方向                                     │
│                                ▲                                       │
│                                │                                       │
│    ┌──────────┐               │                                       │
│    │ Interface│               │                                       │
│    └────┬─────┘               │                                       │
│         │                     │                                       │
│         ▼                     │                                       │
│    ┌──────────┐               │                                       │
│    │Capabilities│              │                                       │
│    └────┬─────┘               │                                       │
│         │                     │                                       │
│         ▼                     │                                       │
│    ┌──────────┐               │                                       │
│    │ Thinking │               │                                       │
│    │  ◀────────────────────────┘                                       │
│    └────┬─────┘                                                      │
│         │                                                            │
│         ▼                                                            │
│    ┌──────────┐                                                      │
│    │  Emotion │                                                      │
│    └────┬─────┘                                                      │
│         │                                                            │
│         ▼                                                            │
│    ┌──────────┐                                                      │
│    │  Agents  │                                                      │
│    └──────────┘                                                      │
│                                                                       │
│    依赖方向：上层依赖下层，业务层不依赖框架                              │
└───────────────────────────────────────────────────────────────────────┘
```

### 2.3 LangGraph Loop 内部流程

```
                    ┌─────────────────────────┐
                    │       User Input        │
                    │      (文本/语音/图片)     │
                    └───────────┬─────────────┘
                                │
                                ▼
                    ┌─────────────────────────┐
                    │     Input Processing     │
                    │    输入处理与标准化       │
                    └───────────┬─────────────┘
                                │
                                ▼
                    ┌─────────────────────────┐
                    │        Evaluate          │
                    │   评估：理解意图与上下文   │
                    └───────────┬─────────────┘
                                │
                                ▼
                    ┌─────────────────────────┐
                    │        Decide           │
                    │   决策：选择行动方案      │
                    │                         │
                    │  • 需要调工具？          │
                    │  • 准备回复？            │
                    │  • 继续循环？            │
                    └───────────┬─────────────┘
                                │
                    ┌───────────┴───────────┐
                    │                       │
              需要工具                   直接回复
                    │                       │
                    ▼                       ▼
        ┌───────────────────┐   ┌───────────────────┐
        │      Invoke       │   │      Respond      │
        │   调度 Agent 执行   │   │    生成回复        │
        └─────────┬─────────┘   └─────────┬─────────┘
                  │                       │
                  │                       │
                  ▼                       │
        ┌───────────────────┐             │
        │      Collect      │             │
        │    收集执行结果     │             │
        └─────────┬─────────┘             │
                  │                       │
                  │◀──────────────────────┘
                  │         (继续评估或结束)
                  │
                  │ (迭代 < max_iterations)
                  │
                  └──────────┐
                             │
                    ┌────────▼────────┐
                    │  是否达到退出条件  │
                    │  • 回复已充分     │
                    │  • 迭代次数耗尽   │
                    │  • 出现错误       │
                    └────────┬────────┘
                             │
                    ┌────────┴────────┐
                    │                 │
                   是                否
                    │                 │
                    ▼                 │
        ┌───────────────────┐         │
        │      END          │         │
        │      结束         │         │
        └───────────────────┘         │
                             └─────────┘
                               (回到 Evaluate)
```

---

## 3. SmartButler 代码模块设计

### 3.1 整体目录结构

**说明**：以下只列出目录结构与各目录承担的职责，不列出具体文件名。代码文件（.py）会随实现演进而变化，但目录的职责划分是稳定的。

```
SmartButler/
│
├── smartbutler/                      # 主应用包（含包入口与对外核心 API）
│   │
│   ├── interface/                    # 接口层：对外暴露的入口
│   │                                  #   - HTTP REST API
│   │                                  #   - WebSocket 实时通道
│   │                                  #   - 命令行工具
│   │                                  #   - MCP 协议支持
│   │
│   ├── capabilities/                  # 能力层：基础能力抽象与多种实现
│   │   ├── llm/                      # LLM 能力（抽象基类、多厂商实现、注册机制）
│   │   ├── asr/                      # 语音识别能力
│   │   ├── tts/                      # 语音合成能力
│   │   ├── vision/                   # 视觉理解能力
│   │   └── tools/                    # 工具能力（基类、注册表、Schema）
│   │
│   ├── agents/                        # Sub-Agent 层：垂直领域智能单元（代码实现）
│   │   ├── base/                      # Sub-Agent 基类与公共抽象（BaseAgent）
│   │   ├── manager/                  # AgentManager：注册、发现、调度的统一入口
│   │   ├── home/                      # 智能家居 Sub-Agent（含其专属工具）
│   │   ├── schedule/                  # 日程管理 Sub-Agent（含其专属工具）
│   │   └── search/                    # 搜索 Sub-Agent（含其专属工具）
│   │
│   ├── skills/                        # Skills 层：Anthropic Skills 格式能力包
│   │   ├── loader/                   # SKILL.md 解析（YAML frontmatter + body）
│   │   ├── runtime/                  # Skill 运行时（注入 prompt + 注册 tool）
│   │   └── builtin/                  # 内置 Skills（pdf-summary / stock-analysis ...）
│   │                                   # 亲属/用户可往这里拖文件夹,无需改代码
│   │
│   ├── emotion/                       # 情感层：性格与记忆
│   │   ├── personality/              # 性格系统（特征定义、状态、演化、注入、持久化）
│   │   └── memory/                    # 记忆系统（短期、长期、情景、语义、检索、持久化）
│   │
│   ├── thinking/                      # 思考层：核心认知与决策
│   │   ├── loop/                      # 循环编排（Reactive + Proactive 双循环）
│   │   │   ├── reactive/              # ReactiveLoop（Phase 4 已有）:用户驱动
│   │   │   ├── proactive/             # ProactiveLoop（Phase 5+）:事件驱动
│   │   │   ├── base.py                # BaseLoop 抽象(共享 ReAct 模式)
│   │   │   ├── graph.py               # LangGraph StateGraph(Reactive 专用)
│   │   │   ├── state.py               # ButlerState Schema
│   │   │   ├── nodes/                 # 业务节点(decide / ToolNode)
│   │   │   └── orchestrator.py        # ButlerOrchestrator 双入口
│   │   ├── proactive/                 # 🆕 主动专属逻辑
│   │   │   ├── reasoning.py           # ProactiveReasoning:该不该主动
│   │   │   ├── decision.py            # ProactiveDecision / ProactiveResult 模型
│   │   │   └── triggers.py            # 触发条件配置
│   │   ├── reasoning/                 # 推理业务（任务规划、任务分解、反思）
│   │   ├── decision/                  # 决策业务（意图识别、路由选择）
│   │   ├── prompt/                    # Prompt 工程（构造、组装、模板）
│   │   │   └── templates/             # Prompt 模板资源
│   │   └── context/                   # 上下文管理（窗口、压缩）
│
│   ├── events/                        # 🆕 事件驱动层（Phase 7，骨架阶段）
│   │   ├── core/                      # 核心抽象:Event基类/EventBus/Trigger/Normalizer
│   │   ├── protocol/                  # 设备/语音/定时器事件协议
│   │   ├── adapters/                  # 设备接入适配器(HomeAssistant/米家/Mock)
│   │   └── routing/                   # 反馈路由(AnswerRouter/PresenceService)
│
│   ├── config/                        # 配置管理（主配置 + 各子模块配置）
│   ├── storage/                       # 存储层（多种后端实现）
│   └── utils/                         # 工具函数（日志、异步、序列化等通用工具）
│
├── tests/                             # 测试目录
│   ├── unit/                          # 单元测试
│   ├── integration/                   # 集成测试
│   └── e2e/                           # 端到端测试
│
├── docs/                              # 文档目录
├── config/                            # 配置文件目录（YAML/JSON/TOML）
└── scripts/                           # 脚本工具目录
```

### 3.2 各模块职责定义

#### 3.2.1 接口层 (interface/)

| 模块 | 职责 |
|------|------|
| **http_api** | 提供 HTTP REST API，接收用户请求，返回智能体响应 |
| **websocket_server** | 提供 WebSocket 通道，支持实时双向通信（语音流） |
| **cli** | 命令行工具，用于调试、测试、管理 |
| **mcp_protocol** | MCP (Model Context Protocol) 协议支持，可被 Claude Desktop 等调用 |

#### 3.2.2 能力层 (capabilities/)

| 模块 | 职责 |
|------|------|
| **llm** | LLM 能力抽象，支持多厂商切换（OpenAI/Anthropic/本地模型等） |
| **asr** | 语音识别能力抽象 |
| **tts** | 语音合成能力抽象 |
| **vision** | 视觉理解能力抽象 |
| **tools** | 工具能力抽象，定义 Tool 的标准接口和注册机制 |

#### 3.2.3 Sub-Agent 层 (agents/)

> **关键概念区分**：本节的"Agent"特指**代码实现的 Sub-Agent**，与 §3.2.6 的
> "Skill"（Anthropic Skills 格式能力包）是**完全不同的概念**。详见 ADR-006。

| 子模块/目录 | 职责 |
|------|------|
| **base/** | Sub-Agent 基类与公共抽象：定义 `BaseAgent`（name / description / tools / ainvoke / to_langchain_tool），是 Sub-Agent 体系的根基 |
| **manager/** | `AgentManager`：负责 Sub-Agent 的注册、发现、调度；上层（thinking 层）通过 manager 找到并执行 Sub-Agent，不直接与具体 Sub-Agent 耦合 |
| **home/** | 智能家居 Sub-Agent：封装窗帘 / 灯光 / 空调等设备的控制逻辑，独立 LLM 循环 |
| **schedule/** | 日程管理 Sub-Agent：封装 OAuth / webhook 等副作用，独立 LLM 循环 |
| **search/** | 搜索 Sub-Agent：封装搜索 API / 缓存 / 熔断，独立 LLM 循环 |

**BaseAgent 核心接口**：

```python
class BaseAgent(ABC):
    name: str                                # 唯一 ID: "home_agent"
    description: str                         # 路由用："管理家居设备..."
    tools: list[BaseTool]                    # 该 Sub-Agent 管辖的工具

    @abstractmethod
    async def ainvoke(self, input: AgentInput, ctx: AgentContext) -> AgentResult: ...

    def to_langchain_tool(self) -> StructuredTool:
        """把 Sub-Agent 暴露成 LangChain StructuredTool,
           管家 LLM 通过 delegate_to_<name> 调用"""
        async def call_agent(task: str) -> str:
            result = await self.ainvoke(
                AgentInput(raw=task),
                AgentContext(parent_agent="butler"),
            )
            return result.output
        return StructuredTool.from_function(
            coroutine=call_agent,
            name=f"delegate_to_{self.name}",
            description=self.description,
            args_schema=DelegateInput,
        )
```

**AgentManager 关键 API**：

```python
class AgentManager:
    def register(self, agent: BaseAgent) -> None: ...
    def unregister(self, name: str) -> None: ...
    def get(self, name: str) -> BaseAgent: ...
    def list_all(self) -> list[BaseAgent]: ...
    def get_delegate_tools(self) -> list[StructuredTool]:
        """把所有 Sub-Agent 包装成 delegate_to_<name> 工具集,注入管家 LLM"""
        return [a.to_langchain_tool() for a in self.list_all()]
```

**职责边界**：

| 设计点 | 说明 |
|--------|------|
| **职责边界** | Manager 只负责"找到 Sub-Agent、调度 Sub-Agent"，不关心 Sub-Agent 内部如何执行 |
| **依赖方向** | thinking 层调 Manager；Manager 查找 Sub-Agent；Sub-Agent 内部用 capabilities/tools |
| **可发现性** | 管家 LLM 通过 `delegate_to_<name>` tool description 自动路由到合适的 Sub-Agent |
| **解耦** | thinking 层不直接 import 具体 Sub-Agent 类，只与 Manager 接口交互 |
| **与 Skill 的关系** | Manager **不**管 Skill，Skill 由 §3.2.6 的 SkillLoader 管理 |

#### 3.2.4 情感层 (emotion/)

| 模块 | 职责 |
|------|------|
| **personality/traits** | 定义性格特征（如：外向程度、幽默感、同理心等） |
| **personality/state** | 管理当前性格状态（可能随心情变化） |
| **personality/evolution** | 性格演化规则，根据反馈逐渐调整性格 |
| **personality/injector** | 性格注入器，将性格特征注入到 Prompt/回复中 |
| **memory/short_term** | 短期记忆，管理当前对话上下文 |
| **memory/long_term** | 长期记忆，持久化用户偏好、历史交互 |
| **memory/retrieval** | 记忆检索，根据当前上下文召回相关记忆 |

#### 3.2.5 思考层 (thinking/)

> **核心结构（Phase 5+）**：`thinking/loop/` 下有**两条并列循环**——
> `ReactiveLoop`（用户驱动，Phase 4 已有）和 `ProactiveLoop`（事件驱动，Phase 5+ 新建）。
> 两条循环**共享** `thinking/reasoning/` `thinking/decision/` `thinking/memory_access/` `thinking/prompt/` `thinking/context/` 的能力
> ——**不重复造轮子**。详见 §5.9（ADR-009）。

| 模块 | 职责 |
|------|------|
| **loop/reactive/** | `ReactiveLoop`：用户输入驱动的 ReAct 循环；用户问 → 管家思考 → 调 tool → 答用户（**永远不沉默**）|
| **loop/proactive/** | `ProactiveLoop`（🆕）：事件/状态驱动循环；管家观察 → 触发判断 → 沉默/主动推送（**支持沉默**）|
| **loop/base** | `BaseLoop` 抽象类（🆕）：两条循环共享 ReAct 模式的最小契约（run / should_run / get_tools）|
| **loop/graph** | LangGraph `StateGraph` + `ToolNode` + `InMemorySaver`（**Reactive 专用**）|
| **loop/state** | `ButlerState` Schema：`messages / user_id / session_id / parent_agent / iteration_count / max_iterations / skill_prompt_snippets` |
| **loop/nodes/decide** | 决策节点：LLM 推理 + 决定调 tool 或直接回复（Reactive 唯一业务节点）|
| **loop/nodes/tools** | ToolNode：LangGraph 原生工具执行节点（Sub-Agent `delegate_to_xxx` + 普通 Tool）|
| **loop/skill_injector** | Skill Prompt 注入器：把加载的 Skills 的 `system_prompt` 片段拼接到管家 system prompt |
| **loop/orchestrator** | `ButlerOrchestrator` 双入口：`ainvoke(user_msg)` Reactive + `proactive_tick(event)` Proactive |
| **proactive/reasoning** | `ProactiveReasoning`（🆕）：判断"该不该主动开口"——查 memory / persona / event urgency（**Phase 5 占位，Phase 6 接真实数据**）|
| **proactive/decision** | `ProactiveDecision` / `ProactiveResult` Pydantic 模型（🆕）|
| **proactive/triggers** | 触发条件配置（🆕）：哪些 event.source 走 Proactive |
| **reasoning/reflector** | 反思器，检查执行结果是否正确 |
| **prompt/builder** | Prompt 构造器，组装各种组件生成最终 Prompt |
| **prompt/templates** | Prompt 模板库 |
| **context/window** | 对话窗口管理，控制历史消息数量 |
| **context/compressor** | 上下文压缩，当窗口满时压缩历史 |

> **修订说明**：原 `evaluate / invoke / collect / respond` 节点被 LangGraph 原生
> `StateGraph + ToolNode` 模式替代（详见 ADR-005）。管家循环只保留 `decide` 一个
> 业务节点，其余由 LangGraph 框架处理。

#### 3.2.6 Skills 层 (skills/)

> **重要**：**Skill 不是 Sub-Agent**。Skill 是 Anthropic Skills 格式的**能力包**，
> 由管家（强模型）直接执行，不需要单独的 LLM 实例。详见 ADR-006 / ADR-007。

| 子模块/目录 | 职责 |
|------|------|
| **loader/** | `SkillLoader`：扫描 `skills/builtin/` 与 `~/.smartbutler/skills/`，解析每个文件夹的 `SKILL.md`（YAML frontmatter + markdown body） |
| **runtime/** | `SkillRuntime`：把 Skill 的 `system_prompt` 片段注入管家 system prompt；把 Skill 携带的 `scripts/*.py` 自动生成 `BaseTool` 注册到 ToolRegistry |
| **builtin/** | 内置 Skills 目录（随项目提交，例如 `pdf-summary/`、`stock-analysis/`），作为 Skill 格式的官方示例 |
| **~/.smartbutler/skills/** | 用户级 Skills 目录（亲属可拖入，无需改代码） |

**Skill 数据结构**：

```python
@dataclass
class Skill:
    name: str                                # 来自 SKILL.md frontmatter
    description: str                         # 来自 SKILL.md frontmatter（路由关键）
    system_prompt: str                       # SKILL.md body（注入到管家 prompt）
    tools: list[BaseTool]                    # Skill 自带的工具（自动从 scripts/ 扫描）
    source_dir: Path                         # Skill 文件夹路径（用于热加载定位）
```

**Skill vs Sub-Agent 边界**：

| 维度 | Sub-Agent（代码实现） | Skill（Anthropic Skills 文件） |
|------|----------------------|-------------------------------|
| 形态 | Python 类继承 `BaseAgent` | 文件夹 + `SKILL.md` |
| 执行方 | Sub-Agent 自己的 LLM（便宜模型） | **管家 LLM**（强模型） |
| 触发方式 | `delegate_to_<name>` tool call | 管家 LLM 自主决定是否用（system_prompt 注入） |
| 维护者 | 开发者 | **任何人（含亲属）** |
| 加载时机 | Python import 时 | 启动时扫描 `skills/` 目录 |
| 失败回退 | AgentManager 启动 ERROR | SkillLoader 启动 ERROR + 跳过 |

### 3.3 模块间协作关系

```
用户请求
    │
    ▼
┌─────────────────────────────────────────────────────────────────┐
│                        Interface Layer                          │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │                     HTTP / WebSocket                      │   │
│  └──────────────────────────────────────────────────────────┘   │
└────────────────────────────┬────────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│                        Capabilities Layer                        │
│                                                                 │
│  ┌──────────┐     ┌──────────┐     ┌──────────┐                │
│  │   LLM    │     │   ASR    │     │  Vision   │                │
│  └────┬─────┘     └──────────┘     └──────────┘                │
│       │                                                        │
│  ┌────┴────┐                                                   │
│  │  Tools  │                                                   │
│  └────┬─────┘                                                   │
└───────┼─────────────────────────────────────────────────────────┘
        │
        ▼
┌─────────────────────────────────────────────────────────────────┐
│                         Thinking Layer                           │
│                                                                 │
│  ┌────────────────────────────────────────────────────────┐    │
│  │                    LangGraph Loop                       │    │
│  │                                                         │    │
│  │   Evaluate ──▶ Decide ──▶ Invoke ──▶ Collect ──▶ ...   │    │
│  │                                   │                      │    │
│  │                                   └──────────────────────│    │
│  │                                              (循环)       │    │
│  └─────────────────────────────────────────────────────────┘    │
│                                                                 │
│  各节点内部协作：                                                 │
│                                                                 │
│  ┌─────────────┐     ┌─────────────┐     ┌─────────────┐      │
│  │  Prompt     │────▶│    LLM      │────▶│  Decision   │      │
│  │  Builder    │     │             │     │   Result    │      │
│  └──────┬──────┘     └─────────────┘     └─────────────┘      │
│         │                                                        │
│         │ 调用                                                     │
│         ▼                                                        │
│  ┌─────────────┐     ┌─────────────┐     ┌─────────────┐      │
│  │  Emotion    │     │   Agent     │     │  Memory     │      │
│  │  (性格注入)  │     │  Manager    │     │  (记忆检索)  │      │
│  └─────────────┘     │  (调度)      │     └─────────────┘      │
│                      └──────┬──────┘                             │
│                             │ 调用                               │
│                             ▼                                    │
│                      ┌─────────────┐                            │
│                      │   Agents    │                            │
│                      │ (执行工具)   │                            │
│                      └─────────────┘                            │
└─────────────────────────────────────────────────────────────────┘
                             │
                             ▼
                      ┌─────────────┐
                      │   Response │
                      │   返回给用户 │
                      └─────────────┘
```

### 3.4 数据流与状态管理

#### 3.4.1 请求处理数据流

```
1. 用户输入（文本/语音/图片）
       │
       ▼
2. Interface Layer 标准化输入
   - 文本：直接透传
   - 语音：ASR 转文字
   - 图片：记录图片 URL/路径
       │
       ▼
3. Thinking Loop 接收输入，开始循环
   ┌──────────────────────────────────┐
   │ Loop State Schema:               │
   │ {                                │
   │   user_input: str,              │
   │   messages: list,              │
   │   current_decision: str,        │
   │   pending_tasks: list,         │
   │   tool_results: list,          │
   │   iteration: int,               │
   │   context: dict,               │
   │   final_response: str,         │
   │   is_complete: bool            │
   │ }                               │
   └──────────────────────────────────┘
       │
       ▼
4. 各节点通过 State 共享数据
   - 节点只返回部分更新
   - LangGraph 自动合并到 State
       │
       ▼
5. 循环结束，生成最终回复
       │
       ▼
6. Interface Layer 根据请求类型返回
   - 文本：直接返回文本
   - 语音：TTS 合成音频返回
```

#### 3.4.2 长期数据流

```
┌─────────────┐      ┌─────────────┐      ┌─────────────┐
│   User     │      │   Agent     │      │   Storage   │
│  Interaction│ ───▶│   Loop      │ ────▶│   Layer     │
└─────────────┘      └─────────────┘      └──────┬──────┘
                           │                     │
                           │                     │ 异步写入
                           │                     ▼
                           │              ┌─────────────┐
                           │              │   Emotion   │
                           │              │   Layer     │
                           │              │             │
                           │              │ • 性格状态   │
                           │              │ • 记忆更新  │
                           │              │ • 偏好记录  │
                           │              └─────────────┘
                           │
                           ▼
                    ┌─────────────┐
                    │  下次交互    │
                    │  可读取     │
                    └─────────────┘
```

---

## 4. SmartButler 技术选型

### 4.1 核心技术栈

| 层级 | 技术选型 | 选型理由 |
|------|----------|----------|
| **运行时** | Python 3.11+ | LLM 生态最成熟，async/await 完善 |
| **类型检查** | Pydantic v2 | 强大的数据验证，序列化/反序列化一体化 |
| **异步框架** | asyncio + FastAPI | 现代化、高性能、内置 OpenAPI 文档 |
| **Agent 编排** | LangGraph | 原生支持循环、状态管理、中断恢复 |
| **LLM 调用** | LangChain (可选) | 生态丰富，但不强依赖 |

### 4.2 能力层技术选型

| 能力 | 推荐技术 | 备选技术 |
|------|----------|----------|
| **LLM** | OpenAI GPT-4 / Claude 3.5 | Anthropic API、Ollama（本地） |
| **语音识别 (ASR)** | Whisper API / FunASR | 阿里云 ASR、腾讯 ASR |
| **语音合成 (TTS)** | Azure TTS / CosyVoice | 阿里云 TTS、Edge TTS |
| **视觉理解** | GPT-4V / Claude Vision | 阿里通义千问 VL |
| **向量数据库** | Qdrant / Chroma | Milvus、Pinecone、FAISS |

### 4.3 存储层技术选型

| 数据类型 | 推荐存储 | 说明 |
|----------|----------|------|
| **对话上下文** | SQLite / Redis | 短期，高频读写 |
| **用户记忆** | SQLite + Qdrant | 结构化数据 + 向量检索 |
| **性格状态** | SQLite | 轻量，支持 JSON 字段 |
| **Agent 配置** | YAML / TOML | 静态配置，版本友好 |
| **日志** | 文件 / ELK | 结构化日志 |

### 4.4 接口层技术选型

| 接口类型 | 技术选型 | 说明 |
|----------|----------|------|
| **HTTP API** | FastAPI / Uvicorn | 异步、高性能、自动文档 |
| **WebSocket** | FastAPI WebSocket | 与 HTTP 共享端口 |
| **CLI** | Typer | 基于 FastAPI CLI，类型安全 |
| **MCP** | 自研 + SDK | 支持 Claude Desktop 调用 |

### 4.5 开发与部署技术选型

| 维度 | 技术选型 | 说明 |
|------|----------|------|
| **依赖管理** | Poetry / uv | 现代化 Python 包管理 |
| **配置管理** | Pydantic Settings | 类型安全的环境变量管理 |
| **日志** | Loguru / Structlog | 结构化日志 |
| **测试** | pytest + pytest-asyncio | 异步测试支持 |
| **容器化** | Docker | 环境一致性 |
| **进程管理** | Supervisor / Systemd | 生产环境进程管理 |

### 4.6 技术选型原则

1. **优先生态成熟度**：选社区活跃、文档完善的库
2. **保持可替换性**：核心接口抽象，避免强耦合具体实现
3. **异步优先**：充分利用 asyncio 提升并发能力
4. **类型安全**：Pydantic + 类型注解，提前发现错误
5. **配置外部化**：所有配置通过环境变量或配置文件注入

---

## 5. 关键技术决策记录

### 5.1 为什么用 LangGraph 而不用 LangChain Agent？

| 对比项 | LangChain Agent | LangGraph |
|--------|-----------------|-----------|
| 循环支持 | 有限 | 原生支持 |
| 状态管理 | 需自行实现 | 内置 |
| 可视化 | 困难 | 简单导出 Mermaid |
| 定制粒度 | 粗 | 细 |
| 中断恢复 | 需自行实现 | 开箱即用 |
| 适用场景 | 简单场景 | 复杂 Agent |

**决策**：SmartButler 作为通用智能体，流程复杂，选用 LangGraph。

### 5.2 为什么能力层不直接用 LangChain？

| 对比项 | 直接用 LangChain | 自建能力抽象 |
|--------|------------------|--------------|
| 开发速度 | 快 | 慢 |
| 可控性 | 低 | 高 |
| 可替换性 | 依赖 LangChain 版本 | 完全可控 |
| 复杂度 | 低 | 中等 |

**决策**：能力层自己定义抽象接口（BaseLLM、BaseTool 等），可复用 LangChain 的实现，但不绑定。

### 5.3 记忆系统为什么分开存储？

| 记忆类型 | 存储方式 | 原因 |
|----------|----------|------|
| **短期记忆** | SQLite | 高频读写，不需要向量检索 |
| **长期记忆** | SQLite + Qdrant | 需要向量检索相似记忆 |
| **情景记忆** | SQLite (JSON) | 结构化事件记录 |
| **语义记忆** | Qdrant | 知识图谱式存储 |

**决策**：不同类型的记忆用不同的存储方式，平衡性能和功能。

### 5.4 Thinking Mode：动态三档路由（ADR-004）

#### 5.4.1 背景

DeepSeek-Flash 等推理优化模型默认会输出 `reasoning_content`（思考过程），这对某些高频低单价场景（如闲聊、简单问答）是不必要的延迟和成本。用户需要按任务复杂度动态决定是否开启思考模式。

#### 5.4.2 三档设计

```python
class ThinkingMode(StrEnum):
    OFF  = "off"   # 关闭思考模式，extra_body={"thinking": {"type": "disabled"}}
    AUTO = "auto" # 自动判断（启发式 + 小模型兜底）
    ON   = "on"   # 开启思考模式，extra_body={"thinking": {"type": "enabled", "budget_tokens": ...}}
```

#### 5.4.3 决策链路（分层路由）

```
用户请求
    │
    ├─ 调用方显式传参 thinking="on/off"？
    │   是 → 直接使用，覆盖一切
    │
    ├─ Agent 自己声明 _thinking_default()？
    │   是 → ChatAgent→off / ExecutorAgent→on / 其他→auto
    │
    ├─ 用户消息含关键词？（纯字符串匹配，零成本）
    │   "认真想想" → on
    │   "随便说说" → off
    │
    ├─ 启发式打分（Step 1：< 1ms，不调用模型）
    │   score ≤ 0   → off
    │   score ≥ 1   → on  （中间地带直接走 on，asymmetric loss 原则）
    │
    └─ 小模型兜底（Step 2：仅对启发式返回 auto 时触发，< 5% 请求）
        调用快模型（DeepSeek-V3-lite 等）做二分类
```

启发式打分信号：

| 信号 | 加分 |
|------|------|
| 消息 token 数 > 80 | +1 |
| 包含数字/金额/日期 | +1 |
| 约束数 ≥ 3 | +2 |
| 含"规划/分析/代码/写"等动词 | +2 |
| 历史已调工具 ≥ 4 次 | +2 |
| 含"你好/谢了/再见" | -2 |
| 单字短句 | -1 |

#### 5.4.4 元数据透出

每次 LLM 调用返回时携带调用"病历"：

```python
class LLMCallMeta(BaseModel):
    thinking_mode_used: Literal["off","auto","on"]
    decision_source: Literal[
        "explicit_user",   # 用户消息关键词
        "explicit_agent", # Agent 默认声明
        "explicit_call",  # 业务代码显式传参
        "heuristic",      # 启发式打分
        "meta_llm",       # 小模型二分类
        "fallback_off",   # 超时/预算耗尽强制 off
    ]
    decision_reason: str            # "score=4,constraints=3,keyword=规划"
    total_latency_ms: float
    prompt_tokens: int
    completion_tokens: int
    reasoning_tokens: int | None   # 思考模式专属
```

#### 5.4.5 落地阶段

| 阶段 | 内容 |
|------|------|
| 阶段 3（LangGraph Loop） | 加 `thinking_mode` 字段 + Step 0（Agent 显式）+ Step 1（启发式） |
| 阶段 5（情感/记忆） | 加 Step 2（小模型兜底）+ 监控面板 |

#### 5.4.6 模型侧适配映射

```python
# 适配器层统一抽象
class ThinkingModeParams:
    @staticmethod
    def to_extra_body(mode: ThinkingMode, model: str) -> dict | None:
        if mode == "off":
            # DeepSeek
            if "deepseek" in model.lower():
                return {"thinking": {"type": "disabled"}}
            # 其他厂商可按需扩展
        if mode == "on":
            if "deepseek" in model.lower():
                return {"thinking": {"type": "enabled", "budget_tokens": 1024}}
        return None  # auto 或不支持的模型，走默认行为
```

#### 5.4.7 关键原则

1. **显式优先于隐式**：Agent 和调用方声明 > 关键词 > 启发式 > 小模型
2. **asymmetric loss**：中间地带不确定时，默认走 on（on 有收益，off 有成本）
3. **零成本兜底**：启发式覆盖 90% 请求，小模型仅兜底模糊 case
4. **可观测性先于完美分类**：每次调用留痕，用数据迭代规则而非一次性设计完美规则

### 5.5 多 Agent 协作：LangGraph Supervisor Pattern（ADR-005）

#### 5.5.1 背景

SmartButler 管家需要调度多个领域 Sub-Agent（Home / Schedule / Search 等）。
本节确定 Sub-Agent 协作方式以及 LangGraph 多 Agent 模式选型。

#### 5.5.2 LangGraph 官方多 Agent 模式

LangGraph 官方文档（[Multi-Agent Systems](https://langchain-ai.github.io/langgraph/concepts/multi_agent/)）
定义了四种标准模式：

| 模式 | 描述 | 适用 |
|------|------|------|
| **Network** | Agent 之间对等通信，无中心调度 | 复杂协作 / 辩论 |
| **Supervisor** | 中央调度 Agent 决定下一个调谁 | **本方案** |
| **Hierarchical Teams** | 多层 Supervisor 嵌套 | 企业级（暂不需要） |
| **Custom Multi-Agent** | 自定义 Graph 节点 | 特殊流程 |

**决策**：SmartButler 选用 **Supervisor** 模式 —— 管家作为中央调度器，统一决策。

#### 5.5.3 Supervisor 的两种实现方式

| 维度 | Subgraphs（紧耦合） | **Tool-Calling（松耦合）** ✅ |
|------|---------------------|---------------------------|
| Worker 形态 | 独立 `StateGraph` | LangChain `StructuredTool` |
| 状态共享 | 共享 state schema | 仅靠 tool args/results |
| 动态增删 Agent | 困难（要重启 Supervisor graph） | **简单**（改 tools 列表即可） |
| Anthropic Skills 适配 | 难（Skill 不是 StateGraph） | **天然适配**（Skill.tools → ToolRegistry） |
| 错误处理 | 异常进 state，需手工处理 | LLM 看到 error，可自主决定重试/降级 |
| 调试观测 | LangGraph trace（复杂） | LangChain tool call trace（清晰） |

**决策**：选 **Tool-Calling**，因为 SmartButler 需要支持亲属添加 Skills（动态性 + 第三方格式）。

#### 5.5.4 落地：BaseAgent 暴露成 LangChain StructuredTool

不自己造轮子，用 LangChain 成熟的 `StructuredTool` 机制作为 Sub-Agent 与管家 LLM 的桥梁：

```python
# smartbutler/agents/base.py
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

class DelegateInput(BaseModel):
    task: str = Field(..., description="交给 Sub-Agent 的具体任务描述")

class BaseAgent(ABC):
    name: str
    description: str       # 路由准不准的关键

    @abstractmethod
    async def ainvoke(self, input: AgentInput, ctx: AgentContext) -> AgentResult: ...

    def to_langchain_tool(self) -> StructuredTool:
        async def call_agent(task: str) -> str:
            return (await self.ainvoke(
                AgentInput(raw=task),
                AgentContext(parent_agent="butler"),
            )).output
        return StructuredTool.from_function(
            coroutine=call_agent,
            name=f"delegate_to_{self.name}",
            description=self.description,
            args_schema=DelegateInput,
        )
```

`StructuredTool.from_function` 由 LangChain 负责：JSON Schema 生成、tool_call 处理、结果回灌。

#### 5.5.5 管家 Loop：LangGraph 原生 StateGraph + ToolNode

```python
from langgraph.graph import StateGraph, MessagesState, START, END
from langgraph.prebuilt import ToolNode

graph = StateGraph(MessagesState)
graph.add_node("decide", decide_node)        # llm_with_tools.ainvoke(messages)
graph.add_node("tools", ToolNode(all_tools)) # LangGraph 原生,执行 delegate_to_xxx 和普通 Tool
graph.add_edge(START, "decide")
graph.add_conditional_edges("decide", should_continue, ["tools", END])
graph.add_edge("tools", "decide")            # tool 结果回灌到 decide
return graph.compile(checkpointer=PostgresCheckpointer())
```

**不**使用 `langgraph.prebuilt.create_supervisor`：

| 问题 | 详情 |
|------|------|
| 抽象过度 | 状态流转做成内置，我们想控的（性格注入、记忆检索、并发委派）没法做 |
| 强假设 | worker 必须都是 StateGraph，Skills 接入麻烦 |
| 黑盒 | 出问题难调试 |
| 代码量 | 自己组装并没有多几行 |

#### 5.5.6 不变量

- Sub-Agent 内部**仍可**用便宜模型（Haiku）做工具选择
- 管家本身**仍用**强模型（Opus / Sonnet）做推理 + 路由
- 错误信息**回流**到管家 LLM，让其自主决定后续动作
- 失败时 LangGraph Checkpointer 保证可恢复

### 5.6 Skill = 能力包，不等于 Sub-Agent（ADR-006）

#### 5.6.1 背景

家属用户希望不写代码就能给管家加能力。最初设计把 Skill 包装成 `SkillAgent`（独立 Sub-Agent 用小模型），
发现这有两个问题：

1. **质量问题**：复杂 Skill（PDF 摘要、股票分析）用小模型质量存疑
2. **主流框架都没有**：Anthropic / LangChain / AutoGen / CrewAI 都没有 "Skill = Sub-Agent" 这种设计

#### 5.6.2 业界主流做法

| 框架 | Skill 形态 | 执行方 |
|------|-----------|--------|
| **Anthropic Claude** | SKILL.md + 资源文件 | **主 Agent（最强模型）** |
| **LangChain Agent** | Tool（带 prompt 注入） | 绑定到 Agent LLM |
| **Microsoft AutoGen** | `AssistantAgent` 持有 skill prompt | 同一个 Agent |
| **CrewAI** | Task → Tool | TaskExecutor 绑定的 Agent |
| **OpenAI Swarm** | Instructions + handover | 当前 Agent（可切换） |

**共同规律**：Skill 本质上是"能力的描述/定义"，执行方始终是持有 SKILL 的那个 Agent 本身。

#### 5.6.3 决策

| 决策项 | 内容 |
|--------|------|
| Skill 形态 | **能力包**（含 `system_prompt` + `tools` + 资源文件） |
| 执行方 | **管家 LLM**（强模型，不单独起 Sub-Agent） |
| 加载机制 | `SkillLoader` 扫描 `skills/` → 注入管家 `system_prompt` + 注册 tools |
| 与 Sub-Agent 关系 | Skill **不**继承 `BaseAgent`，独立概念 |
| 复用模式 | 亲属只需写一个 SKILL.md 文件，扔进 `~/.smartbutler/skills/` |

#### 5.6.4 边界澄清（任务划分）

| 任务类型 | 走 Sub-Agent | 走 Skill |
|----------|--------------|----------|
| 控制具体设备（窗帘 / 灯 / 空调） | ✅ HomeAgent | - |
| 日程 OAuth / webhook 等副作用 | ✅ ScheduleAgent | - |
| 搜索流量控制 / 熔断 | ✅ SearchAgent | - |
| 长时任务（盯着股票异动） | ✅ 独立 Sub-Agent | - |
| PDF 摘要 | - | ✅ pdf-summary skill |
| 股票分析 | - | ✅ stock-analysis skill |
| 亲属加的"健康饮食建议" | - | ✅ my-diet skill |

**判断依据**：
- 需要**状态机**、**副作用管理**、**独立 LLM 推理** → Sub-Agent
- 只需要**强模型 + 步骤提示 + 工具** → Skill

### 5.7 Anthropic Skills 格式标准（ADR-007）

#### 5.7.1 背景

亲属加技能不能用项目自创格式，否则亲属要学一套新东西。
直接对标 **Anthropic Skills** 格式，亲属在 Anthropic 生态下写过的 SKILL.md 直接能用。

#### 5.7.2 标准 SKILL.md 格式

```markdown
---
name: my-skill
description: |
技能描述。
When to use: 触发场景（关键，决定管家 LLM 是否调用）。
When NOT to use: 反例（避免误触发）。
---

# My Skill

## Steps
1. ...

## Available Tools
- tool_name(...)

## Output Format
...
```

外加可选子目录：`scripts/`（自动生成 Tool）/ `templates/` / `resources/`。

#### 5.7.3 SmartButler 适配

- `SkillLoader` 扫描两个目录：
  - 项目内：`smartbutler/skills/builtin/`（随仓库提交）
  - 用户级：`~/.smartbutler/skills/`（亲属/用户自定义，无需改代码）
- 解析 YAML frontmatter 提取 `name` / `description`
- body 作为 `system_prompt` 片段追加到管家 system prompt
- `scripts/*.py` 按 `@register_tool(scope=SKILL)` 装饰器规范自动生成 `BaseTool`

#### 5.7.4 失败回退

| 失败类型 | 处理 |
|----------|------|
| SKILL.md 缺 frontmatter | 启动 ERROR 日志，跳过该 skill |
| YAML 解析失败 | 启动 ERROR 日志，跳过该 skill |
| tools/*.py 加载失败 | Skill 内 LLM 看到错误消息，可自主决定是否继续 |
| Skill 整体加载失败 | 不影响其他 Skill 加载，管家继续启动 |

#### 5.7.5 Phase 5 实现，本节先确定接口

具体实现细节在 Phase 5 展开。

### 5.8 事件驱动架构（ADR-008，Phase 7）

> **修订（2026-10-08）**：原 §5.8 "复用 ainvoke + parent_agent 路由"方案**已废弃**。
> 替换为 **Proactive / Reactive 双循环架构**（详见 §5.9 / `smartbutler/thinking/ADR-009-proactive-reactive-dual-loop.md`）。
> 本节保留**事件协议**与**总线选型**——这两部分不受双循环改动影响。
> 路由部分请直接看 §5.9。

#### 5.8.1 背景

SmartButler 当前只支持**主动调用模式**（用户发请求 → 管家回答）。
智能家居场景需要**被动触发模式**：

- 智能门锁被打开 → 管家主动打招呼
- 热水器温度到 → 管家主动通知
- 早晨 7 点 → 管家主动叫人起床
- 烟雾报警 → 管家紧急通知

#### 5.8.2 核心决策（保留部分）

| 决策 | 选择 | 理由 |
|------|------|------|
| 触发模式 | **事件驱动**（不轮询） | 100 个设备时轮询 100 QPS，事件驱动 0 QPS |
| 事件总线 | **Redis Streams** | smartbutler/storage/redis_backend.py 已有基础；轻量；ACK；可回放 |
| 归一化 | **EventNormalizer 必走** | 不归一化直接喂 LLM = 事件风暴把管家打挂 |
| 触发模式工具白名单 | **只读工具** | 禁止写操作（避免"事件→管家→写设备→又发事件"循环）|

#### 5.8.3 ❌ 废弃的"复用 ainvoke"方案

原方案：

```
events/ (Phase 7)
    │
    ├──► thinking/loop/   ButlerOrchestrator.ainvoke()  ← 复用,不修改主体
    │
    ├──► thinking/loop/   parent_agent="trigger:..."    ← 已有字段,直接用
    │
    └──► interface/       HTTP/WebSocket 用户入口        ← 共存,事件走事件口
```

**问题**：

1. **语义错位**：把"事件触发"伪装成"用户消息"——ainvoke 假设有 user_input，事件触发常常没用户
2. **沉默困难**：ainvoke 永远不沉默，但 Proactive 70%+ 应该沉默
3. **路由不灵活**：ainvoke 走 HTTP/WebSocket 入口，但事件应该走 AnswerRouter
4. **parent_agent 字段双重职责**：既是路由依据又是信息字段——混了

#### 5.8.4 ✅ 替代方案

**ButlerOrchestrator 双入口 + EventTrigger 显式路由**：

```
events/ (Phase 7)
    │
    ├──► EventTrigger.route(event)              ← 🆕 显式路由
    │       │
    │       ├── source == user  →  ReactiveLoop.ainvoke(msg)
    │       └── source == *     →  ProactiveLoop.tick(event)
    │
    ├──► ReactiveLoop   ← Phase 4 已有,一行不改
    │
    ├──► ProactiveLoop  ← 🆕 Phase 5 新建,ADR-009
    │
    └──► AnswerRouter   ← 主动推送渠道(音响/手机/邮件)
```

**对现有代码的侵入**：

| 文件 | 改动 |
|------|------|
| `ButlerOrchestrator.ainvoke()` | **不改**（保持 `-> str`）|
| `ButlerOrchestrator.proactive_tick()` | **新增**（`-> ProactiveResult`）|
| `EventTrigger.handle()` | **改**为 `route()` 方法 + 双分支 |

**主体循环逻辑 / decide 节点 / ToolNode / Checkpointer 一行不动**。

#### 5.8.5 Phase 7 子阶段（修订后）

| 子阶段 | 内容 | 工作量 | 状态 |
|--------|------|--------|------|
| 7.0 | 协议 + 抽象接口 | ✅ 已完成 | 落库 |
| 7.1 | EventNormalizer + 简单 dedup | 1 周 | 待启动 |
| 7.2 | EventTrigger.route() 实现 | 1 天 | 待启动 |
| 7.3 | ProactiveLoop 框架（**降级后备** reasoning） | 1 周 | 🆕 与 7.1 并行 |
| 7.4 | ReactiveLoop → ProactiveLoop 内部通道 | 2 天 | 🆕 |
| 7.5 | Redis Streams EventBus 实现 | 1 周 | 待启动 |
| 7.6 | HomeAssistant DeviceAdapter | 2 周 | 待启动 |
| 7.7 | PresenceService + AnswerRouter | 2 周 | 待启动 |
| 7.8 | 端到端测试 + 误触发兜底 | 1 周 | 待启动 |

**总计 ~8 周**。**不是 1 天**。

#### 5.8.6 不变量

1. **`ainvoke(user_msg) -> str` 签名不变**——所有现有 e2e 测试零修改
2. `proactive_tick(event) -> ProactiveResult` 是新方法——不替代 ainvoke
3. 事件触发不绕过 EventNormalizer
4. parent_agent 协议**保留**（`trigger:<source>.<subtype>`），但**不再作为路由依据**
5. 写操作工具不暴露给 Proactive 触发的工具

#### 5.8.7 完整 ADR

详见：
- `smartbutler/events/ADR-008-event-driven.md`（事件协议 + 总线，本节保留）
- `smartbutler/thinking/ADR-009-proactive-reactive-dual-loop.md`（🆕 双循环架构，替代本节原路由方案）

---

### 5.9 Proactive / Reactive 双循环架构（ADR-009，Phase 5+）

#### 5.9.1 背景

§5.8 原方案试图用 `parent_agent="trigger:..."` 字段把"事件触发"伪装成"用户消息"，
**复用 ainvoke 入口**。这导致 4 个根本性冲突（详见 ADR-009 §1.2）：

- **触发者错位**：ainvoke 假设有 user_input，事件触发常常没
- **沉默困难**：ainvoke 永远不沉默，Proactive 70%+ 应该沉默
- **路由不灵活**：ainvoke 走 HTTP/WebSocket，事件应走 AnswerRouter
- **parent_agent 双重职责**：混了"路由依据"和"信息字段"

#### 5.9.2 核心决策

**ProactiveLoop 和 ReactiveLoop 是 `thinking/loop/` 下的两条并列循环**。
**EventTrigger 显式路由**——不再靠 parent_agent 字段。

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

> **本图与下方 §5.9.3 路由规则有 4 处旧版本残留**，统一以本节最新分层图为准。
> 旧版错把"用户消息"也走 EventBus，再靠 `source==user` 路由回 Reactive。
> 正确分层是：**用户消息永远不进 EventBus**——见下方 §5.9.2a "两种触发器分层"。

---

#### 5.9.2a 🆕 两种触发器分层（MessageIngress vs EventSource）

**核心原则**：**用户消息 = Reactive 入口 = 不经过 EventBus**。

EventBus 只承载"非用户来源"的事件（设备状态 / 定时器 / 唤醒 / 外部回调）。
用户消息（文字 / 语音）由 `interface/` 层的 HTTP / WebSocket handler **直接调 `ainvoke()`**。

```
┌──────────────────────────────────────────────────────────────────────────┐
│  Layer 1: 用户消息入口（MessageIngress）= Reactive，不经 EventBus         │
│  位置: smartbutler/interface/                                             │
│                                                                          │
│   ┌──────────────────────┐  ┌──────────────────────┐  ┌──────────────┐  │
│   │ HTTP handler         │  │ WebSocket handler    │  │ 小程序入口   │  │
│   │ (App 文字消息)        │  │ (App 语音 / 音响)    │  │ (Phase 9)    │  │
│   │ POST /chat/{user}    │  │ /ws/{user}           │  │              │  │
│   └──────────┬───────────┘  └──────────┬───────────┘  └──────┬───────┘  │
│              │                         │                     │         │
│              └─────────────────────────┴─────────────────────┘         │
│                                       │                                  │
│                          orch.ainvoke(msg, user_id)  ← 直接调          │
│                                       │                                  │
│                                       ▼                                  │
│                              ReactiveLoop (Phase 4 已有)                │
│                                                                          │
│  ✅ 跟 EventBus 无关    ✅ 跟 BaseEvent 无关                              │
│  ❌ 不构造 EventSource  ❌ 不调 EventTrigger.route()                      │
└──────────────────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────────────────┐
│  Layer 2: 设备事件源（EventSourceAdapter）= 推 EventBus = Proactive       │
│  位置: smartbutler/events/adapters/                                       │
│                                                                          │
│   ┌──────────────┐ ┌──────────────┐ ┌──────────────┐ ┌──────────────┐  │
│   │ soundbox_    │ │ device_      │ │ timer_       │ │ webhook_     │  │
│   │ adapter      │ │ adapter      │ │ adapter      │ │ adapter      │  │
│   │ 音响 wakeup  │ │ 门锁/灯/空调 │ │ 定时器/闹钟  │ │ 外部回调     │  │
│   └──────┬───────┘ └──────┬───────┘ └──────┬───────┘ └──────┬───────┘  │
│          │                │                │                │          │
│          └────────────────┴────────────────┴────────────────┘          │
│                                       │                                  │
│         BaseEvent(source=DEVICE/VOICE/TIMER/WEBHOOK, ...) 构造          │
│                                       │                                  │
│                          event_bus.publish(event)                        │
│                                       │                                  │
│                                       ▼                                  │
│                  EventTrigger.route(event) → ProactiveLoop              │
│                                                                          │
│  ✅ 跟 ainvoke 无关       ❌ 不构造 user_input                            │
│  ❌ 不返回响应给发件人     ✅ fire-and-forget                             │
└──────────────────────────────────────────────────────────────────────────┘
```

**为什么"用户消息"不进 EventBus？** 因为 EventBus 上 `BaseEvent.source` 枚举只有
`DEVICE / VOICE / TIMER / WEBHOOK`（见 `smartbutler/events/core/event.py:EventSource`），
**没有 USER / INTERFACE**——这是设计上的有意排除。

**为什么不让 EventBus 收到"用户消息"再路由回 Reactive？** 三个理由：

1. **响应链路错位**：用户问"开灯"必须 1 秒内 TTS 回复，但 EventBus → ProactiveLoop →
   AnswerRouter 链路太重，且 Proactive 70%+ 应该沉默
2. **失败语义不同**：HTTP 失败要 500 给前端；EventBus 失败要 silent 不能骚扰
3. **EventTrigger 失去单职责**：它本来只管"非用户事件"路由，硬塞"用户消息"会让
   `route()` 函数的 source 枚举从 4 个变成 6 个，引入 USER/INTERFACE 两个空分支

**所以 EventTrigger.route() 的真实职责只有一条**：判断 `source ∈ {DEVICE/VOICE/TIMER/WEBHOOK}`
的事件应该走 ProactiveLoop 的哪个决策分支（沉默 / 主动）——**不判断"是不是用户消息"**。

---

#### 5.9.3 EventTrigger 路由规则（修订版）

```python
# smartbutler/thinking/proactive/triggers.py
_PROACTIVE_SOURCES = frozenset({"device", "voice", "timer", "webhook"})

def route(event: BaseEvent) -> LoopType:
    # EventBus 上不出现 USER/INTERFACE 事件
    # 用户消息走 interface/handler.py 直接 ainvoke(),不进 EventBus
    if event.source in _PROACTIVE_SOURCES:
        return LoopType.PROACTIVE
    return LoopType.PROACTIVE  # 兜底:未识别 source 也走 Proactive
```

| EventSource | 路由 | 理由 |
|---|---|---|
| `device`（智能门锁/灯/空调/热水器/烟雾）| **Proactive** | 设备事件,无人问 |
| `voice`（音响 wakeup 状态）| **Proactive** | 唤醒可能静默 |
| `timer`（定时器/闹钟）| **Proactive** | 定时器触发 |
| `webhook`（外部回调）| **Proactive** | 外部系统回调 |
| **未识别 source** | **Proactive**（兜底）| 宁可沉默不可骚扰 |
| `user` / `interface` | ❌ **根本不在 EventSource 枚举里** | 用户消息走 `interface/handler.py` → `ainvoke()`，**不经过 EventBus** |

**修订要点（vs 旧版）**：

- ❌ **删除**旧版 "user/interface → Reactive" 分支——这条是死代码，EventSource 枚举里没这两个值
- ✅ **保留** `_PROACTIVE_SOURCES` 集合——4 个 source 全部走 Proactive
- ✅ **新增**未识别 source 兜底——避免枚举扩展时漏路由

**验证用例**（`tests/unit/proactive/test_proactive_triggers.py`）：

```python
def test_route_all_known_sources_go_proactive():
    for source in ("device", "voice", "timer", "webhook"):
        assert route(BaseEvent(source=source, ...)) == LoopType.PROACTIVE

def test_route_unknown_source_defaults_proactive():
    assert route(BaseEvent(source="future_source", ...)) == LoopType.PROACTIVE
```

> **架构不变量**：**用户消息永远不进 EventBus**。
> 如果未来某个事件源的 source 字段取 "user" / "interface"，必须先在 `EventSource` 枚举里
> 注册，**并**修改 `interface/handler.py` 让它不构造这种事件（双保险）。

#### 5.9.4 ProactiveLoop 结构

```python
class ProactiveLoop:
    """主动循环——管家自己观察、思考、决定要不要开口。"""

    async def tick(self, event: BaseEvent) -> ProactiveResult:
        # 1. 触发判断: 这事儿要不要主动说?
        decision = await self.reasoning.should_respond(event)

        if not decision.should_respond:
            return ProactiveResult.silent()      # ← 沉默是默认

        # 2. 决定说什么: 走 ReAct 拿工具
        message = await self.react_chain.run(
            trigger=decision,
            event=event,
        )

        # 3. 主动推送
        return ProactiveResult.acted(
            message=message, urgency=decision.urgency
        )
```

#### 5.9.5 沉默是一等公民

```python
class ProactiveResult(BaseModel):
    acted: bool
    message: str | None = None
    push_channel: str | None = None
    silence_reason: str | None = None

    @classmethod
    def silent(cls, reason: str = "default_silent") -> "ProactiveResult":
        return cls(acted=False, silence_reason=reason)
```

**默认 70%+ 应该 silent**——"好管家话不多"。

#### 5.9.6 Reactive → Proactive 内部通道

Reactive 链尾**允许**调 Proactive 拿"主动建议"——但**仅在规则触发**：

```python
class ReactiveLoop:
    async def run(self, user_msg):
        answer = await self.react_chain.run(user_msg)

        # 规则触发: 用户问"该做什么" / "吃什么" → 追加建议
        if self._should_request_proactive(user_msg):
            advice = await self.proactive_loop.advise(
                context=user_msg, current_answer=answer
            )
            if advice:
                return answer + advice
        return answer
```

**判定规则**（不调 LLM——避免开销）：

| 用户消息模式 | 触发 Proactive 建议 |
|---|---|
| "该吃/做/喝什么" / "我该做/怎么办" | ✅ |
| "几点了" / "天气" | ❌ |
| 其他 | ❌ |

**未来升级**：Phase 6 emotion 上线后，规则可升级为 Persona 驱动判断。

#### 5.9.7 共享与独占

| 能力 | ReactiveLoop | ProactiveLoop |
|------|--------------|---------------|
| LLM 调用 / Tool / Memory / Personality / Skill | ✅ 共享 | ✅ 共享 |
| **沉默支持** | ❌ | ✅ |
| **主动推送路由 (AnswerRouter)** | ❌ | ✅ |
| **触发判断 (ProactiveReasoning)** | ❌ | ✅ |
| **写操作** | ✅ | ⚠️ 建议只读（防循环）|

#### 5.9.8 现在实现 vs Phase 6b 推迟

| 内容 | 现在实现 | 推迟 |
|------|---------|------|
| ProactiveLoop 框架 + ProactiveReasoning **降级后备** | ✅ | |
| EventTrigger.route() | ✅ | |
| ButlerOrchestrator.proactive_tick() 入口 | ✅ | |
| Reactive → Proactive 内部调用（规则触发）| ✅ | |
| 最小可用场景（定时器沉默判断）| ✅ | |
| LLM 综合判断（查 Memory + 读 Persona + 调 LLM）| | ✅ **Phase 6b**（待 Phase 6 完成）|
| Persona 驱动主动建议 | | ✅ **Phase 6b** |
| 多模态感知 | | ✅ Phase 9 |
| 真实设备接入 | | ✅ Phase 8 |

**理由**：ProactiveLoop 是 ReactiveLoop 的**镜像**结构，复用现有 thinking/ 底层，
**不依赖** emotion/memory 真实数据。Phase 6b 上线后**不重构**——只填实现，且
**保留降级后备**作为 LLM 故障时的安全网（见架构约束 #9：降级契约）。

#### 5.9.9 不变量

1. **ProactiveLoop 不调 ReactiveLoop**——主动不打断自己
2. **ReactiveLoop 可调 ProactiveLoop**——在用户对话中追加建议（规则触发，opt-in）
3. **沉默是一等公民**——`ProactiveResult.acted=False` 必须被上游正确处理
4. **写操作不暴露给 Proactive 触发的工具**——防"事件→管家→写设备→又发事件"循环
5. **parent_agent 字段语义不变**——只是不再作为路由依据
6. **两条循环共享** `thinking/reasoning/` `decision/` `memory_access/` `prompt/` `context/`
7. **🆕 用户消息永远不进 EventBus**——`interface/handler.py` 直接调 `ainvoke()`；
   `EventSource` 枚举里**不包含** USER/INTERFACE，从类型层面拒绝"用户消息"进 EventBus
8. **🆕 EventTrigger.route() 只为 Proactive 服务**——不存在"路由到 Reactive"分支，
   因为 EventBus 上根本没有 user 源事件；路由函数的单职责是"判断 4 种 Proactive source 的沉默/主动"
9. **🆕 ProactiveReasoning 降级契约**——`RuleBasedProactiveReasoning` 是**降级后备**而非临时占位，**永不被删**。LLM 故障/timeout/key 失效时自动降级，URGENT 事件不依赖 LLM 也能主动。详见 README 架构约束 #9。

#### 5.9.10 验证

- [ ] `EventTrigger.route()` 单测：4 个 source 全部 → Proactive；未识别 source 兜底 Proactive
- [ ] `EventSource` 枚举不包含 USER/INTERFACE（架构不变量 #7 单测）
- [ ] `interface/handler.py` 单测：HTTP/WS handler 直接调 `ainvoke()`，**不构造 BaseEvent**
- [ ] `ProactiveLoop.tick()` 单测：默认 silent；`should_respond=True` 调 react
- [ ] `ReactiveLoop._should_request_proactive()` 单测：规则匹配
- [ ] 1 e2e：定时器事件 → ProactiveLoop → 沉默
- [ ] 1 e2e：定时器事件 → ProactiveLoop → 主动推送

#### 5.9.11 完整 ADR

详见 `smartbutler/thinking/ADR-009-proactive-reactive-dual-loop.md`。

#### 5.9.12 🆕 分层设计原则（MessageIngress vs EventSource）

为了让团队对"什么进 EventBus、什么走 HTTP/WS"有**单一共识**，把上一节的分层图
抽象为 5 条设计原则：

**原则 1：触发器分两种，分别属于不同模块**

| 触发器类型 | 实现位置 | 入口 | 调谁 |
|---|---|---|---|
| **MessageIngress**（用户消息）| `smartbutler/interface/`（HTTP/WS）| HTTP/WS handler | `ButlerOrchestrator.ainvoke()` |
| **EventSourceAdapter**（设备事件）| `smartbutler/events/adapters/` | `soundbox/device/timer/webhook` adapter | `event_bus.publish()` → `EventTrigger.route()` |

**原则 2：EventSource 枚举从类型层面拒绝"用户消息"**

```python
# smartbutler/events/core/event.py
class EventSource(StrEnum):
    """事件来源分类——不包含 USER/INTERFACE,因为用户消息不进 EventBus。"""
    DEVICE  = "device"
    VOICE   = "voice"    # 音响 wakeup 状态,不是语音指令
    TIMER   = "timer"
    WEBHOOK = "webhook"
```

**类型层面**就拒绝让"用户消息"伪装成 EventSource——这是"用户消息永远不进 EventBus"的
**机器可验证**保障。

**原则 3：EventTrigger.route() 单职责——只为 Proactive 服务**

```python
def route(event: BaseEvent) -> LoopType:
    # EventBus 上不出现 USER/INTERFACE 事件,所以这里没有"路由到 Reactive"分支
    if event.source in _PROACTIVE_SOURCES:
        return LoopType.PROACTIVE
    return LoopType.PROACTIVE  # 兜底
```

EventTrigger **不知道** ReactiveLoop 的存在。它的存在意义是：让 ProactiveLoop
**收到事件后**决定"沉默 / 主动 / 怎么主动"——**不**决定"这是不是用户消息"。

**原则 4：失败语义不同源，必须分开处理**

| 失败来源 | 用户消息入口 | 设备事件入口 |
|---|---|---|
| 管家 LLM 报错 | HTTP 500 / WS 错误帧（**必须让前端知道**）| log + silent（**不能骚扰**）|
| 工具调用失败 | 错误信息返回给用户 | silent（**不能让用户被吵醒**）|
| EventBus 断流 | 不受影响（HTTP/WS 独立）| EventSource adapter 重试 + log |
| 管家 5xx | 用户看到"服务暂不可用"| 静默 + 事件回灌队列等恢复 |

**原则 5：双设备协议的入口分离——同一硬件可以两个触发器**

智能音响这种"既是用户输入设备又是状态源"的硬件，**自然有两个 adapter**：

| 入口 | 协议 | 触发器类型 | 处理路径 |
|---|---|---|---|
| 音响 **WebSocket**（用户说话）| WS 音频流 | **MessageIngress** | ASR → `ainvoke()` → Reactive |
| 音响 **状态事件**（wakeup / 空闲）| MQTT / HTTP 回调 | **EventSourceAdapter** | `soundbox.py` → EventBus → Proactive |

**两者同一设备、不同协议入口、不同处理路径**——这才是 EventSource 枚举里
"voice"只表示"wakeup 状态变化"而不是"用户语音指令"的原因。

---

## 6. 未来扩展方向

### 6.1 短期扩展

- [ ] 支持更多 ASR/TTS 引擎
- [ ] 支持更多 LLM 厂商
- [ ] 增加更多垂直领域 Agent
- [ ] 优化上下文压缩算法

### 6.2 中期扩展

- [ ] 多 Agent 协作机制
- [x] 主动服务能力（**已提前到 Phase 5+ 双循环架构**，详见 §5.9 / ADR-009）
- [ ] 个性化模型微调
- [ ] 分布式部署支持

### 6.3 长期愿景

- [ ] 自主学习能力
- [ ] 跨设备上下文同步
- [ ] 多模态情感理解
- [ ] 开放生态（插件系统）

---

## 7. 附录

### 7.1 术语表

| 术语 | 定义 |
|------|------|
| **Agent** | 智能体，能够感知环境、做出决策、执行动作的软件实体 |
| **Capability** | 能力，SmartButler 的基础能力单元（LLM、语音、视觉等） |
| **Loop** | 循环，Agent 处理单个请求的完整流程 |
| **State** | 状态，LangGraph 中各节点共享的数据结构 |
| **Personality** | 性格，SmartButler 的行为风格和人设特征 |
| **Memory** | 记忆，SmartButler 对话历史和用户信息的存储 |

### 7.2 参考资料

- LangGraph 官方文档：https://langchain-ai.github.io/langgraph/
- Pydantic v2 文档：https://docs.pydantic.dev/
- FastAPI 官方文档：https://fastapi.tiangolo.com/
