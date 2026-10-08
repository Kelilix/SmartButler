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

        # 拼 messages:system prompt + state.messages
        # state.messages 第一条通常是 user,系统 prompt 由本节点前置
        state_messages = state.get("messages", [])
        if not state_messages:
            _logger.warning("decide_node.empty_state_messages")
            # 防御:不该发生(state schema 必有 messages)
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


__all__ = ["make_decide_node", "LLMCallable"]
