"""TestTimeAgent 集成测试 (默认 skip,需要真 LLM)。

启用方式: ``pytest -m integration tests/integration/agents/``

要求:
- ``.env`` 配置了 ``SMARTBUTLER_LLM_OPENAI_API_KEY`` 和 ``SMARTBUTLER_LLM_OPENAI_BASE_URL``
- 默认 backend=langchain (因为需要 tool_calls 循环)
"""
from __future__ import annotations

import os

import pytest

from smartbutler.agents.base.types import AgentContext, AgentInput
from smartbutler.agents.time.test_time_agent import TestTimeAgent
from smartbutler.config.llm import load_llm_settings

pytestmark = pytest.mark.integration


def _has_llm_config() -> bool:
    return bool(
        os.environ.get("SMARTBUTLER_LLM_OPENAI_API_KEY")
        and os.environ.get("SMARTBUTLER_LLM_OPENAI_BASE_URL")
    )


def _ctx() -> AgentContext:
    return AgentContext(
        user_id="integration-test",
        session_id="integration-test",
        parent_agent="butler",
    )


@pytest.mark.skipif(not _has_llm_config(), reason="未配置 LLM API key,跳过集成测试")
class TestTimeAgentLive:
    @pytest.mark.asyncio
    async def test_query_beijing_time(self) -> None:
        """查北京时间:LLM 至少调一次 get_current_time 工具,回答含时区名。"""
        from smartbutler.capabilities.llm import create_llm

        settings = load_llm_settings()
        llm = create_llm(settings)
        try:
            agent = TestTimeAgent(llm=llm)
            result = await agent.ainvoke(
                AgentInput(raw="帮我看看现在北京时间几点"),
                _ctx(),
            )
            assert result.success is True
            assert "get_current_time" in result.tool_calls
            # LLM 回答应该提及北京时间或 ISO 格式
            content = result.content
            assert "北京" in content or "Asia/Shanghai" in content or "+08:00" in content
        finally:
            await llm.aclose()

    @pytest.mark.asyncio
    async def test_query_utc_time(self) -> None:
        """查 UTC 时间,验多时区路径也通。"""
        from smartbutler.capabilities.llm import create_llm

        settings = load_llm_settings()
        llm = create_llm(settings)
        try:
            agent = TestTimeAgent(llm=llm)
            result = await agent.ainvoke(
                AgentInput(raw="现在 UTC 时间几点?"),
                _ctx(),
            )
            assert result.success is True
            assert "get_current_time" in result.tool_calls
        finally:
            await llm.aclose()
