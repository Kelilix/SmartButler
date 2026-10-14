"""ButlerGraphBuilder —— 管家 LangGraph 循环组装器。

职责(参考 TECHNICAL_DESIGN.md §3.2.5 + ADR-005):
1. 接受 LLM + 工具列表 + system prompt。
2. 构造 StateGraph(butler_state) + decide_node + ToolNode。
3. 加条件边:decide 输出有 tool_calls → tools;否则 → END。
4. tools → decide 固定边(tool 结果回灌)。
5. compile() 返回 ``CompiledStateGraph``。

**为什么自己组装而不是用 ``create_supervisor``**:
详见 ADR-005 §5.5.5。简单说:我们要控状态字段(iteration_count / skill_prompt_snippets)
和 checkpointer 注入,create_supervisor 的抽象过度。

输入约定:
- ``llm_with_tools``: 已经是 ``bind_tools(all_tools)`` 过的 LangChain ``BaseChatModel``。
- ``system_prompt``: ``ButlerPromptBuilder.build()`` 产出的字符串。
- ``max_iterations``: 循环硬上限,默认 10。
- ``checkpointer``: Phase 4 默认 ``MemorySaver``,Phase 8 部署阶段换 Postgres。

输出:
- ``CompiledStateGraph`` —— 调用 ``.ainvoke(input)`` / ``.astream(input)`` 即可跑循环。
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import structlog
from langchain_core.messages import BaseMessage
from langchain_core.tools import BaseTool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode

from smartbutler.thinking.loop.nodes.decide import make_decide_node
from smartbutler.thinking.loop.state import (
    DEFAULT_MAX_ITERATIONS,
    ButlerState,
)

_logger = structlog.get_logger(__name__)

# should_continue 的返回值与条件边 ``path_map`` 键对齐
_GOTO_TOOLS = "tools"
_GOTO_END = END


def _should_continue(state: ButlerState) -> str:
    """decide → ? 条件边的判定函数。

    规则:
    - state 最后一条是 AIMessage 且有 tool_calls → 走 tools 节点。
    - 否则 → 走 END。

    iteration_count 防御由 decide 节点内部完成(把 tool_calls 抹掉变成纯文本),
    这里只判定 AIMessage.tool_calls 字段。
    """
    messages: Sequence[BaseMessage] = state.get("messages", [])
    if not messages:
        return _GOTO_END
    last = messages[-1]
    # 仅在 AIMessage 时查 tool_calls
    tool_calls = getattr(last, "tool_calls", None)
    if tool_calls:
        return _GOTO_TOOLS
    return _GOTO_END


class ButlerGraphBuilder:
    """管家 LangGraph 循环构建器。

    用法::

        builder = ButlerGraphBuilder()
        compiled = (
            builder
            .with_llm(llm_adapter)
            .with_tools(all_tools)
            .with_system_prompt(prompt)
            .build()
        )
        result = await compiled.ainvoke({"messages": [HumanMessage("...")], ...})
    """

    def __init__(self) -> None:
        self._llm: Any = None
        self._tools: list[BaseTool] = []
        self._system_prompt: str = ""
        self._max_iterations: int = DEFAULT_MAX_ITERATIONS
        self._checkpointer: Any | None = None

    # ---------- fluent API ----------

    def with_llm(self, llm: Any) -> ButlerGraphBuilder:
        """注入 LLM(ButlerChatModelAdapter 或其它 BaseChatModel)。

        必须支持 ``bind_tools(tools) -> Runnable`` 与 ``ainvoke(messages) -> AIMessage``。
        """
        self._llm = llm
        return self

    def with_tools(self, tools: Sequence[BaseTool]) -> ButlerGraphBuilder:
        """注入工具列表(BaseTool / LangChain StructuredTool)。"""
        self._tools = list(tools)
        return self

    def with_system_prompt(self, prompt: str) -> ButlerGraphBuilder:
        """注入 system prompt(由 ButlerPromptBuilder.build() 产出)。"""
        self._system_prompt = prompt
        return self

    def with_max_iterations(self, n: int) -> ButlerGraphBuilder:
        """循环硬上限,默认 10。"""
        if n < 1:
            msg = f"max_iterations 必须 >= 1,收到 {n}"
            raise ValueError(msg)
        self._max_iterations = n
        return self

    def with_checkpointer(self, cp: Any) -> ButlerGraphBuilder:
        """注入 checkpointer(默认 InMemorySaver)。"""
        self._checkpointer = cp
        return self

    # ---------- 构造 ----------

    def build(self) -> Any:
        """编译 LangGraph,返回 ``CompiledStateGraph``。"""
        if self._llm is None:
            msg = "ButlerGraphBuilder 必须先 .with_llm()"
            raise ValueError(msg)
        if not self._system_prompt:
            msg = "ButlerGraphBuilder 必须先 .with_system_prompt()"
            raise ValueError(msg)

        # 1. 绑定工具到 LLM
        llm_with_tools = self._llm.bind_tools(self._tools) if self._tools else self._llm

        # 2. 构造 decide 节点
        decide = make_decide_node(
            llm_with_tools,
            system_prompt=self._system_prompt,
            max_iterations=self._max_iterations,
        )

        # 3. 构造 ToolNode(空 tools 列表时 LangGraph 会警告,这里禁用)
        if self._tools:
            tool_node: Any = ToolNode(self._tools)
        else:
            tool_node = None

        # 4. 拼 StateGraph
        graph: Any = StateGraph(ButlerState)
        graph.add_node("decide", decide)
        if tool_node is not None:
            graph.add_node("tools", tool_node)

        graph.add_edge(START, "decide")

        if tool_node is not None:
            graph.add_conditional_edges(
                "decide",
                _should_continue,
                path_map={
                    _GOTO_TOOLS: "tools",
                    _GOTO_END: END,
                },
            )
            graph.add_edge("tools", "decide")
        else:
            # 无 tool 时 decide 直接 END
            graph.add_edge("decide", END)

        checkpointer = self._checkpointer or InMemorySaver()
        compiled = graph.compile(checkpointer=checkpointer)

        _logger.info(
            "butler_graph.built",
            tool_count=len(self._tools),
            max_iterations=self._max_iterations,
            has_checkpointer=self._checkpointer is not None,
        )
        return compiled


__all__ = ["ButlerGraphBuilder"]
