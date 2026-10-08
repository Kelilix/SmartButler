"""ButlerState —— LangGraph 循环的状态 Schema。

设计原则（参考 TECHNICAL_DESIGN.md §3.2.5 + ADR-005）：
1. **继承 MessagesState**：与 LangGraph 原生 ToolNode 兼容（ToolNode 期望 messages 字段）。
2. **仅追加业务字段**：不要重写 messages 的合并逻辑，让 LangGraph 走默认。
3. **字段可空**：checkpointer 重启时只有 user_id 等元数据，messages 由调用方填。
4. **iteration_count 防御**：防止 decide ↔ tools 死循环。
"""
from __future__ import annotations

from langgraph.graph import MessagesState
from typing_extensions import TypedDict

# 单轮 decide→tools 循环的硬上限
DEFAULT_MAX_ITERATIONS: int = 10


class ButlerState(MessagesState):
    """管家 LangGraph 循环状态。

    继承 MessagesState 后自动获得 ``messages: Annotated[list, add_messages]``。
    业务层只追加字段,**不要**重写 messages 的 reducer。

    字段:

    - ``user_id`` / ``session_id``: 透传到 Sub-Agent / Tool 的标识。
      Phase 4 暂未使用,Phase 6 情感层/记忆层会消费。
    - ``parent_agent``: 调用管家的人是谁,默认 ``"user"``(用户直调)或 ``"api"``(HTTP 入口)。
    - ``skill_prompt_snippets``: Phase 5 注入的 Skill system_prompt 片段。
      Phase 4 留空 list,PromptBuilder 阶段拼接。
    - ``iteration_count``: decide 节点自增,达到 ``max_iterations`` 强制结束,
      防止 LLM 死循环把 token 烧光。
    - ``max_iterations``: 单请求上限,默认 10。
    """

    user_id: str
    session_id: str
    parent_agent: str
    skill_prompt_snippets: list[str]
    iteration_count: int
    max_iterations: int


class ButlerStateDelta(TypedDict, total=False):
    """节点返回的增量更新。

    LangGraph 节点**只**返回 state 字段的子集,框架自动 merge。
    TypedDict + total=False 表达"可选键"。
    """


__all__ = ["ButlerState", "ButlerStateDelta", "DEFAULT_MAX_ITERATIONS"]
