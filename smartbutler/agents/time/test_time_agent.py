"""时间查询 Sub-Agent —— Phase 3 唯一示例,Phase 4 验证用。

设计目的:
1. **真实工具集成**: 不再是 mock / 随机回复 —— 直接调用 capabilities/tools 里已有的
   ``get_current_time`` BaseTool,让 "Sub-Agent 内部用 BaseTool" 这条路径被真实验证。
2. **驱动 Phase 4 集成测试**: Phase 4 ButlerOrchestrator 上线后,
   端到端冒烟测试就是 "管家 → delegate_to_test_time_agent → 返回时间",
   这条链路需要在 Phase 3 就建好可独立调通的 Sub-Agent。

设计原则(参考 TECHNICAL_DESIGN.md §3.2.3 + ADR-005 + Phase 3.5 增量):
1. **最小 LLM 循环**: decide → tool → 收集,最多 max_iterations 步,**不**用 LangGraph。
2. **独立 LLM 实例**: 构造时接受 ``llm=BaseLLM``(默认走 ``create_llm``)。
3. **失败回流**: tool 抛错包成字符串回流到 LLM,让 LLM 自主决定重试 / 道歉。
4. **声明式依赖**: 用 ``@requires_tools`` 装饰器把 tool 依赖挂到类头上方,一眼可见。
   框架在 ``BaseAgent.__init__`` 里自动从 ToolRegistry 解析 + 注册,子类无需手动 _register_tool。

边界:
- 不管 Sub-Agent 之间的通信(Phase 4 管家调度);
- 不管 Skill 加载(Phase 5);
- 不管记忆(Phase 6)。
- 不管真正的 HomeAgent —— 见 Phase 3.5 后续任务。

引入 Phase 3.5 后会被真实 HomeAgent 等替代,但 Sub-Agent 模式的实现可保留为参考。
"""
from __future__ import annotations

import json

import structlog

from smartbutler.agents.base.base import BaseAgent
from smartbutler.agents.base.requires_tools import requires_tools
from smartbutler.agents.base.types import AgentContext, AgentInput, AgentOutput
from smartbutler.capabilities.llm.base import BaseLLM, LLMError
from smartbutler.capabilities.llm.types import (
    Message,
    Role,
    ToolSpec,
)
from smartbutler.capabilities.tools.types import ToolError

_logger = structlog.get_logger(__name__)

# 内部循环上限:防止 LLM 死循环
_MAX_INTERNAL_ITERATIONS = 5

# system prompt:让 LLM 知道它能用什么 tool
_SYSTEM_PROMPT_TEMPLATE = """\
你是 SmartButler 的时间查询 Sub-Agent ({agent_name})。负责回答与时间相关的查询。

可用工具:
{tool_descriptions}

调用规则:
1. 仔细分析用户任务,决定调用哪个工具 (tz 时区参数必须从用户问题里提取或用 'UTC');
2. 如果一次调用结果不够,可以再调用其他工具 (最多 {max_iter} 轮);
3. 拿到所有需要的信息后,**直接**用中文回复用户 (不要再调 tool);
4. 工具调用出错时,根据错误信息判断是否换工具或向用户道歉。

用户语言:中文。
""".strip()


@requires_tools("get_current_time")
class TestTimeAgent(BaseAgent):
    """时间查询 Sub-Agent —— 唯一 Phase 3 上线、Phase 4 立刻可用的 Sub-Agent.

    典型用法::

        llm = create_llm(load_llm_settings())
        agent = TestTimeAgent(llm=llm)
        result = await agent.ainvoke(
            AgentInput(raw="现在北京时间几点?"),
            AgentContext(user_id="u1", session_id="s1"),
        )
        print(result.content)

    **为什么是 "TestTimeAgent" 而非 "TimeAgent"**: 它现阶段存在的首要目的是
    为 Phase 4 提供一个确定可用的 Sub-Agent 走通 "管家 delegate" 全链路。
    命名带 "Test" 是为了让所有看到这个类的开发者 (尤其你自己) 都意识到:
    这是脚手架级别的示例,不是产品级时间能力 —— 等 Phase 3.5 真正上线
    业务 Sub-Agent 时它会被取代或下沉到 capabilities 内部。

    注册到 AgentManager 时会暴露成 ``delegate_to_test_time_agent`` 工具。
    """

    name = "test_time_agent"
    description = (
        "查询当前时间 / 日期 / 时区。"
        "适用场景:'现在几点'/'今天几号'/'北京现在几点'/'纽约时间'等。"
        "输入必须能解析出 IANA 时区名 (Asia/Shanghai / UTC / America/New_York 等)。"
        "不适用:日程、记忆、家居设备、日志分析等问题。"
    )

    # 内部循环上限
    max_internal_iterations: int = _MAX_INTERNAL_ITERATIONS

    def __init__(self, llm: BaseLLM) -> None:
        # BaseAgent.__init__ 会按 @requires_tools 声明的 required_tool_names
        # 自动从 ToolRegistry.get_default() 解析 + _register_tool。
        # 这里子类只关心自己的 LLM 即可。
        super().__init__()
        self._llm = llm
        self._tool_specs: list[ToolSpec] = [t.to_tool_spec() for t in self.tools]

        _logger.info(
            "test_time_agent.initialized",
            agent_name=self.name,
            tool_count=len(self._tool_specs),
            tool_names=[t.name for t in self.tools],
        )

    def _build_system_prompt(self) -> str:
        tool_lines = []
        for t in self.tools:
            tool_lines.append(f"- {t.name}: {t.description}")
        return _SYSTEM_PROMPT_TEMPLATE.format(
            agent_name=self.name,
            tool_descriptions="\n".join(tool_lines),
            max_iter=self.max_internal_iterations,
        )

    async def handle(self, input: AgentInput, ctx: AgentContext) -> AgentOutput:
        """最小 LLM 循环:decide → tool → 收集 → 结束。"""
        tool_ctx = self._build_tool_context(ctx)
        messages: list[Message] = [
            Message(role=Role.SYSTEM, content=self._build_system_prompt()),
            Message(role=Role.USER, content=input.raw),
        ]
        used_tools: list[str] = []

        for iteration in range(1, self.max_internal_iterations + 1):
            try:
                resp = await self._llm.chat(messages, tools=self._tool_specs)
            except LLMError as exc:
                return AgentOutput(
                    content="",
                    success=False,
                    error=f"LLM 调用失败: {type(exc).__name__}: {exc}",
                )

            # LLM 决定要调 tool
            if resp.tool_calls:
                # 记录 assistant 消息 (含 tool_calls)
                messages.append(
                    Message(
                        role=Role.ASSISTANT,
                        content=resp.content,
                        tool_calls=resp.tool_calls,
                    )
                )
                for tc in resp.tool_calls:
                    used_tools.append(tc.function.name)
                    tool = self.get_tool(tc.function.name)
                    if tool is None:
                        # 未知 tool → 包错误回流给 LLM
                        tool_result = f"[ToolError] 未知工具: {tc.function.name}"
                    else:
                        try:
                            args = json.loads(tc.function.arguments or "{}")
                        except json.JSONDecodeError as exc:
                            tool_result = f"[ToolError] arguments 不是合法 JSON: {exc}"
                        else:
                            try:
                                result = await tool.ainvoke(tool_ctx, **args)
                                tool_result = (
                                    result.content
                                    if result.success
                                    else f"[ToolError] {result.error or 'unknown error'}"
                                )
                            except ToolError as exc:
                                tool_result = f"[ToolError] {type(exc).__name__}: {exc}"
                            except Exception as exc:  # noqa: BLE001 - 兜底
                                tool_result = f"[ToolError] {type(exc).__name__}: {exc}"

                    messages.append(
                        Message(
                            role=Role.TOOL,
                            content=tool_result,
                            tool_call_id=tc.id,
                        )
                    )
                continue  # 下一轮让 LLM 看完结果再决定

            # LLM 直接给出最终回复
            final_content = resp.content or ""
            return AgentOutput(
                content=final_content,
                success=True,
                data=(
                    {"iterations": iteration, "model": resp.model}
                    if resp.model
                    else {"iterations": iteration}
                ),
                tool_calls=used_tools,
            )

        # 超过最大迭代还没收敛 → 强制结束
        return AgentOutput(
            content="抱歉,处理这个任务需要的步骤太多,已自动终止。请换一种更简单的描述。",
            success=False,
            error=f"超过 max_internal_iterations={self.max_internal_iterations} 仍未收敛",
            tool_calls=used_tools,
        )


__all__ = ["TestTimeAgent"]
