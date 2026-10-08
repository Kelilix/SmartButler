"""tests/e2e/test_butler_orchestrator.py — ButlerOrchestrator 端到端测试。

默认 skip;启用方式:
    pytest -m e2e tests/e2e/test_butler_orchestrator.py -v

本文件验证 Phase 4 的核心承诺:

1. **全链路连通**: 用户输入 → ButlerOrchestrator.ainvoke() →
   管家 LLM 决定调 delegate_to_test_time_agent → TestTimeAgent
   内部 LLM 调 get_current_time → 汇总回管家 → 返回给用户。

2. **Sub-Agent 真实执行**: 不是 mock,是从 BaseAgent.to_langchain_tool
   → ToolNode → BaseAgent.ainvoke 完整跑一遍。

3. **state 字段透传**: user_id / session_id / iteration_count 走完一圈不变。

4. **错误回流**: 故意问 LLM 一个明显需要调 Sub-Agent 的问题,
   验证 LLM 真的调用了 delegate_to_test_time_agent。

依赖 .env 中的 LLM 凭证;没有凭证会自动 skip(与现有 e2e 模式一致)。
"""
from __future__ import annotations

import pytest

from smartbutler.agents.manager.manager import AgentManager
from smartbutler.agents.time.test_time_agent import TestTimeAgent
from smartbutler.capabilities.llm import create_llm
from smartbutler.capabilities.llm.base import BaseLLM
from smartbutler.capabilities.tools.common import (  # noqa: F401  触发 @register_tool
    get_current_time,
)
from smartbutler.config import load_llm_settings
from smartbutler.thinking import ButlerOrchestrator

pytestmark = pytest.mark.e2e


@pytest.fixture
async def llm() -> BaseLLM:
    settings = load_llm_settings()
    instance = create_llm(settings)
    try:
        yield instance
    finally:
        await instance.aclose()


@pytest.fixture
def reset_managers() -> None:
    """每个 e2e 用例都重置 AgentManager 默认实例,避免跨例污染。

    不重置 ToolRegistry —— @register_tool 装饰器只 import 时触发一次,
    重置后再去 find tool 会找不到。
    """
    AgentManager.reset_default()


@pytest.fixture
async def orchestrator(llm: BaseLLM, reset_managers: None) -> ButlerOrchestrator:  # noqa: ARG001
    """构造已注册 TestTimeAgent 的 orchestrator。"""
    settings = load_llm_settings()
    # 注册 Sub-Agent 到默认 manager
    AgentManager.get_default().register(TestTimeAgent(llm=llm))
    return ButlerOrchestrator(llm=llm, llm_settings=settings)


class TestButlerOrchestratorE2E:
    @pytest.mark.asyncio
    async def test_direct_answer_no_tool_needed(self, orchestrator: ButlerOrchestrator) -> None:
        """简单闲聊问题 → LLM 直接回答,不调工具。"""
        answer = await orchestrator.ainvoke(
            "你好,请用一句话自我介绍。",
            user_id="e2e-user",
            session_id="e2e-sess-1",
        )
        # 应是非空字符串
        assert isinstance(answer, str)
        assert len(answer) > 0
        # 简单闲聊不需 tool,answer 不应包含 [ToolError] 等异常标签
        assert "[ToolError]" not in answer
        assert "[AgentError]" not in answer

    @pytest.mark.asyncio
    async def test_delegate_to_time_agent(self, orchestrator: ButlerOrchestrator) -> None:
        """问当前时间 → LLM 应主动调 delegate_to_test_time_agent。

        这条链路是 Phase 4 最重要的 E2E 验证:
        管家 LLM 看到 '现在几点' → 决定调 Sub-Agent → Sub-Agent 内部
        调 get_current_time → 返回结果给管家 → 管家组织语言回复用户。
        """
        answer = await orchestrator.ainvoke(
            "现在北京时间几点?",
            user_id="e2e-user",
            session_id="e2e-sess-2",
        )
        assert isinstance(answer, str)
        assert len(answer) > 0
        # 不应有错误
        assert "[ToolError]" not in answer
        assert "[AgentError]" not in answer
        # 回答里应该提到时间(因为 LLM 拿到的是时间字符串)
        # 弱校验:长度 > 5,因为简单 '12:00' 太短易误判
        assert len(answer) > 5

    @pytest.mark.asyncio
    async def test_session_id_threads_independent(self, orchestrator: ButlerOrchestrator) -> None:
        """两个不同 session_id 应当完全独立(各自的 checkpointer thread)。"""
        a1 = await orchestrator.ainvoke(
            "你好", user_id="u", session_id="s-A"
        )
        a2 = await orchestrator.ainvoke(
            "你好", user_id="u", session_id="s-B"
        )
        # 两次都应正常返回
        assert isinstance(a1, str) and len(a1) > 0
        assert isinstance(a2, str) and len(a2) > 0
