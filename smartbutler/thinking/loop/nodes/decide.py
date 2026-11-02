"""管家 LangGraph 循环节点。

设计原则（参考 TECHNICAL_DESIGN.md §3.2.5 + ADR-005）：
1. **LangGraph 原生 StateGraph + ToolNode**：不自己写 decide→invoke→collect 状态机，
   只保留一个 decide 业务节点，其余由 LangGraph 框架处理。
2. **decide_node 是唯一业务节点**：调 LLM → 产 AI message（含 tool_calls 或 final）。
3. **iteration_count 自增防御死循环**：由 decide 节点写入 state，
   should_continue 条件边据此判断是否强制结束。
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import structlog
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, SystemMessage

from smartbutler.thinking.loop.state import (
    DEFAULT_MAX_ITERATIONS,
    ButlerState,
)

_logger = structlog.get_logger(__name__)

# LLM 工厂:接受 messages+tools,返回 AIMessage。
# 由 ButlerGraphBuilder 在 build() 时注入,避免节点直接 import LangChain 客户端。
LLMCallable = Callable[[list[BaseMessage], list[Any] | None], Awaitable[AIMessage]]


def make_decide_node(
    llm_with_tools: BaseChatModel,
    *,
    system_prompt: str,
    max_iterations: int = DEFAULT_MAX_ITERATIONS,
) -> Callable[[ButlerState], Awaitable[dict[str, Any]]]:
    """工厂:返回真正的 decide_node 函数,绑定 LLM + system prompt。

    工厂模式而非闭包全局变量,便于:
    1. 单测里随便换 FakeChatModel。
    2. 多实例 ButlerGraphBuilder 各自有独立 LLM。

    Args:
        llm_with_tools: 已 bind_tools() 过的 LangChain BaseChatModel。
        system_prompt: 管家固定人设 + Skills(由 ButlerPromptBuilder.build() 产出)。
        max_iterations: 单请求 decide→tools 循环硬上限,默认 10。

    Returns:
        async decide_node(state) -> dict(state 增量)
    """

    async def decide_node(state: ButlerState) -> dict[str, Any]:  # noqa: ARG001
        """decide 节点:调 LLM,返回 AIMessage + iteration_count 自增。"""
        # iteration_count 自增(从 0 开始,首次 decide 后变 1)
        prev_count = int(state.get("iteration_count", 0))
        new_count = prev_count + 1

        # 🆕 2026-11-01: "当前会话上下文"段**每轮动态渲染**。
        #
        # 为什么不能只放在编译期的 system prompt 里:
        #   system prompt 是 build() 时拼好的,塞进闭包。而"当前在跟谁说话"
        #   是每轮变化的 —— 爸爸说完妈妈说,同一条 thread 里 user_id 一直在变。
        #   之前只改 orchestrator._current_user_id 而不重建 graph,
        #   prompt 里就停留在上一轮的值 → LLM 被告知"你在跟爸爸说话",
        #   实际是妈妈在说话。
        #
        # 现在从 state 每轮读,永远和本轮 user_id 一致。
        session_context = _render_session_context(
            current_user_id=state.get("current_user_id", ""),
            current_user_name=state.get("current_user_name", ""),
        )

        # 拼 messages:system prompt + session context + state.messages
        # state.messages 第一条通常是 user,系统 prompt 由本节点前置
        state_messages = state.get("messages", [])
        if not state_messages:
            _logger.warning("decide_node.empty_state_messages")            # 防御:不该发生(state schema 必有 messages)
            return {
                "messages": [
                    AIMessage(content="(内部错误: 输入消息为空,请重试。)"),
                ],
                "iteration_count": new_count,
            }

        # system prompt 复用检测:state 不持有 system prompt,
        # 但 state_messages 第一个若已经是 SystemMessage 且内容一致就跳过
        llm_messages: list[BaseMessage] = []
        if (
            state_messages
            and isinstance(state_messages[0], SystemMessage)
            and state_messages[0].content == system_prompt
        ):
            llm_messages = list(state_messages)
        else:
            llm_messages = [SystemMessage(content=system_prompt), *state_messages]

        # 把"当前会话上下文"插在 system prompt 之后、用户消息之前。
        # ⚠️ 不能追加到 messages 末尾 —— 那会变成"用户说完话之后管家才被告知
        # 用户是谁",LLM 读到的顺序就反了。
        if session_context:
            llm_messages = [
                llm_messages[0],
                SystemMessage(content=session_context),
                *llm_messages[1:],
            ]

        # 调 LLM —— 错误回流(异常包成 AIMessage),让 LangGraph 走到 END 不再循环
        try:
            raw: BaseMessage = await llm_with_tools.ainvoke(llm_messages)
        except Exception as exc:  # noqa: BLE001
            _logger.error("decide_node.llm_failed", error=str(exc), exc_type=type(exc).__name__)
            return {
                "messages": [
                    AIMessage(content=f"(LLM 调用失败: {type(exc).__name__}: {exc})"),
                ],
                "iteration_count": new_count,
            }

        # ainvoke 返回 BaseMessage(我们故意放宽),cast 回 AIMessage 拿 tool_calls
        if not isinstance(raw, AIMessage):
            # 理论上 LangGraph chat model 一定返回 AIMessage,防御性 fallback
            ai_message = AIMessage(content=str(getattr(raw, "content", "")))
        else:
            ai_message = raw

        # 防御:超过 max_iterations 还没收敛,强制终止
        if new_count >= max_iterations and ai_message.tool_calls:
            _logger.warning(
                "decide_node.max_iterations_reached",
                iteration_count=new_count,
                max_iterations=max_iterations,
            )
            # 去掉 tool_calls,改成道歉
            ai_message = AIMessage(
                content=(
                    "抱歉,这个问题需要的步骤太多,管家已自动终止。"
                    "请换一种更简单的描述,或拆成几个小问题。"
                ),
                # 显式置空 tool_calls,LangGraph 看到空 tool_calls 会走 END
            )

        # 记录关键诊断
        _logger.info(
            "decide_node.complete",
            iteration=new_count,
            has_tool_calls=bool(ai_message.tool_calls),
            tool_count=len(ai_message.tool_calls) if ai_message.tool_calls else 0,
            content_len=len(ai_message.content or ""),
        )

        return {
            "messages": [ai_message],
            "iteration_count": new_count,
        }

    return decide_node


def _render_session_context(
    *, current_user_id: str, current_user_name: str = ""
) -> str:
    """渲染"当前会话上下文"段(2026-11-01)。

    🆕 从编译期的 system prompt 挪到**每轮 state 驱动**。

    为什么:system prompt 是 build() 时拼死的,而"当前在跟谁说话"每轮都在变
    (爸爸说完妈妈说)。只改 orchestrator._current_user_id 而不重建 graph,
    prompt 里就停留在上一轮的值 —— LLM 被告知"你在跟爸爸说话",
    实际是妈妈在说话。现在每轮从 state 读,永远一致。

    Args:
        current_user_id: 本轮说话人的 user_id(如 ``speaker_xxx``)。
        current_user_name: 本轮说话人的可读姓名(如 ``"爸爸"``)。

    Returns:
        渲染后的 markdown 段;current_user_id 为空时返回 ""(不注入)。
    """
    if not current_user_id or not current_user_id.strip():
        return ""

    who = current_user_name.strip() or current_user_id
    return (
        "## 当前会话上下文\n"
        f"- 当前对话用户: **{who}** (user_id: {current_user_id})\n"
        f"- **重要**:你正在和【{who}】对话,不要把其他人说过的话当成眼前用户的身份。\n"
        "  历史记忆里每条都有 \"[xxx 说]\" 前缀,这是信息来源标记——如果前缀不是【"
        f"{who}】,那说明这条记忆是别人说的,与当前对话用户无关。"
    )


__all__ = ["make_decide_node", "LLMCallable"]
