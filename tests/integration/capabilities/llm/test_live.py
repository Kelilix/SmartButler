"""tests/integration/capabilities/llm/test_live.py — 真实调用 DeepSeek 的连通性测试。

默认 skip；启用方式：
    pytest -m integration

或单跑本文件：
    pytest -m integration tests/integration/capabilities/llm/test_live.py -v

环境要求：
    .env 中 SMARTBUTLER_LLM_OPENAI_API_KEY / SMARTBUTLER_LLM_OPENAI_BASE_URL 已配置。
"""

from __future__ import annotations

import pytest

from smartbutler.capabilities.llm import (
    FinishReason,
    FunctionSpec,
    LLMResponse,
    Message,
    ToolSpec,
    create_llm,
)
from smartbutler.config import load_llm_settings

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_deepseek_simple_chat() -> None:
    """真实发请求,验证 DeepSeek-Flash 端到端连通。"""
    settings = load_llm_settings()
    llm = create_llm(settings)
    try:
        resp: LLMResponse = await llm.chat(
            [Message.user("用一句话介绍你自己,不超过 30 字。")],
            temperature=0.3,
            max_tokens=2048,
        )
        assert resp.content, "DeepSeek 返回 content 为空"
        assert resp.finish_reason is FinishReason.STOP
        assert resp.usage.total_tokens > 0
        assert resp.model != ""
    finally:
        await llm.aclose()


@pytest.mark.asyncio
async def test_deepseek_stream_chunks() -> None:
    """流式响应能正确切分 chunk 并拼接出完整文本。"""
    settings = load_llm_settings()
    llm = create_llm(settings)
    try:
        chunks: list[str] = []
        finish: FinishReason | None = None
        async for sc in llm.chat_stream(
            [Message.user("用一句话介绍你自己,不超过 30 字。")],
            temperature=0.3,
            max_tokens=2048,
            extra_body={"thinking": {"type": "disabled"}},
        ):
            chunks.append(sc.content_delta)
            if sc.finish_reason is not None:
                finish = sc.finish_reason
        joined = "".join(chunks)
        assert joined, "流式响应拼接结果为空"
        assert finish is FinishReason.STOP
    finally:
        await llm.aclose()


@pytest.mark.asyncio
async def test_deepseek_tool_choice() -> None:
    """tool_choice=auto 模式下,模型能正确返回 tool_calls。"""
    settings = load_llm_settings()
    llm = create_llm(settings)
    try:
        resp = await llm.chat(
            [Message.user("北京今天几度?")],
            tools=[
                ToolSpec(
                    function=FunctionSpec(
                        name="get_weather",
                        description="查询指定城市的天气",
                        parameters={
                            "type": "object",
                            "properties": {
                                "city": {
                                    "type": "string",
                                    "description": "城市名,如'北京'",
                                }
                            },
                            "required": ["city"],
                        },
                    )
                )
            ],
            temperature=0.0,
            max_tokens=200,
        )
        assert resp.finish_reason in (FinishReason.TOOL_CALLS, FinishReason.STOP)
        if resp.finish_reason is FinishReason.TOOL_CALLS:
            assert resp.tool_calls is not None and len(resp.tool_calls) >= 1
            tc = resp.tool_calls[0]
            assert tc.function.name == "get_weather"
            args = tc.function.parsed_arguments()
            assert "city" in args
    finally:
        await llm.aclose()
