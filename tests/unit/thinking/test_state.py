"""ButlerState 单元测试。"""
from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage

from smartbutler.thinking.loop.state import (
    DEFAULT_MAX_ITERATIONS,
    ButlerState,
)


class TestButlerState:
    def test_state_accepts_required_fields(self) -> None:
        state: ButlerState = {
            "messages": [HumanMessage(content="hi")],
            "user_id": "u1",
            "session_id": "s1",
            "parent_agent": "user",
            "skill_prompt_snippets": [],
            "iteration_count": 0,
            "max_iterations": DEFAULT_MAX_ITERATIONS,
        }
        assert state["user_id"] == "u1"
        assert state["iteration_count"] == 0

    def test_state_messages_use_langchain_reducer(self) -> None:
        """``messages`` 字段由 MessagesState 注入 add_messages reducer,
        两次写入会累加而非覆盖。"""
        from langgraph.graph.message import add_messages

        msg1 = HumanMessage(content="hi")
        msg2 = AIMessage(content="hello")
        out = add_messages([msg1], [msg2])
        assert len(out) == 2
        assert out[0].content == "hi"
        assert out[1].content == "hello"

    def test_state_iteration_can_increment(self) -> None:
        """decide 节点把 iteration_count 自增 1。"""
        state: ButlerState = {
            "messages": [],
            "user_id": "u",
            "session_id": "s",
            "parent_agent": "user",
            "skill_prompt_snippets": [],
            "iteration_count": 3,
            "max_iterations": 10,
        }
        # 模拟 decide 节点返回的增量
        delta: dict = {"iteration_count": state["iteration_count"] + 1}
        merged = {**state, **delta}
        assert merged["iteration_count"] == 4
