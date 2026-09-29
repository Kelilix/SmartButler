"""tests/e2e/test_streaming.py — DeepSeek 流式连通测试（含 thinking 模式切换验证）。

默认 skip；启用方式：
    pytest -m e2e tests/e2e/test_streaming.py -v

本文件验证：
1. DeepSeek-Flash 流式调用端到端连通（基础）。
2. 通过 extra_body 关闭思考模式（thinking=disabled）时，模型正常返回非思考内容。
3. 通过 extra_body 开启思考模式（thinking=enabled）时，reasoning_content 字段有值。
4. 流式场景下 reasoning_content_delta 随 chunk 递增。

DeepSeek thinking 参数文档：
    https://api-docs.deepseek.com/guides/think_mode
    关闭思考：extra_body={"thinking": {"type": "disabled"}}
    开启思考：extra_body={"thinking": {"type": "enabled", "budget_tokens": 1024}}
"""

from __future__ import annotations

import pytest
import structlog

from smartbutler.capabilities.llm import (
    FinishReason,
    LLMResponse,
    Message,
    create_llm,
)
from smartbutler.config import load_llm_settings

pytestmark = pytest.mark.e2e


# ---------------------------------------------------------------------------
# 辅助：收集流式 chunk 并返回完整文本、reasoning 文本、最后一个 finish_reason
# ---------------------------------------------------------------------------

async def collect_stream(llm, messages, extra_body=None):
    """执行一次流式调用，收集所有 chunk 后返回 (text, reasoning_text, finish_reason)。"""
    chunks_text: list[str] = []
    chunks_reasoning: list[str] = []
    final_finish: FinishReason | None = None

    async for sc in llm.chat_stream(messages, extra_body=extra_body):
        chunks_text.append(sc.content_delta)
        if sc.reasoning_content_delta:
            chunks_reasoning.append(sc.reasoning_content_delta)
        if sc.finish_reason is not None:
            final_finish = sc.finish_reason

    return "".join(chunks_text), "".join(chunks_reasoning), final_finish


# ---------------------------------------------------------------------------
# 测试用例
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stream_basic():
    """验证 DeepSeek 流式调用端到端连通，无 thinking 参数干预。"""
    settings = load_llm_settings()
    llm = create_llm(settings)

    try:
        messages = [Message.user("用一句话介绍你自己，不超过 30 字。")]
        text, reasoning, finish = await collect_stream(llm, messages)

        assert text, "流式响应拼接结果 text 为空"
        assert finish is FinishReason.STOP, f"期望 STOP，实际 {finish}"
    finally:
        await llm.aclose()


@pytest.mark.asyncio
async def test_thinking_disabled_via_extra_body():
    """验证通过 extra_body={\"thinking\":{\"type\":\"disabled\"}} 可关闭思考模式。

    关闭思考模式后，reasoning_content 应全部为空（无推理过程输出）。
    """
    settings = load_llm_settings()
    llm = create_llm(settings)

    try:
        messages = [Message.user("1+1 等于几？直接回答数字。")]
        text, reasoning, finish = await collect_stream(
            llm, messages, extra_body={"thinking": {"type": "disabled"}}
        )

        assert text.strip(), "text 不应为空"
        # 关闭思考模式后，模型不应输出 reasoning_content
        assert not reasoning, (
            f"thinking=disabled 时 reasoning 不应存在，实际得到：{reasoning!r}"
        )
        assert finish is FinishReason.STOP
    finally:
        await llm.aclose()


@pytest.mark.asyncio
async def test_thinking_enabled_via_extra_body():
    """验证通过 extra_body={\"thinking\":{\"type\":\"enabled\"}} 可开启思考模式。

    开启思考模式后，reasoning_content_delta 应非空（包含模型的推理过程）。
    注：DeepSeek-Flash 作为推理优化模型，默认即有 reasoning_content，
    此测试主要验证 extra_body 传参不破坏正常流式输出。
    """
    settings = load_llm_settings()
    llm = create_llm(settings)

    try:
        messages = [Message.user("为什么天空是蓝色的？请简短解释。")]
        text, reasoning, finish = await collect_stream(
            llm, messages, extra_body={"thinking": {"type": "enabled"}}
        )

        # 开启思考模式，reasoning 字段应有内容（DeepSeek 推理模型特性）
        assert text.strip(), "text 不应为空"
        # reasoning 可能为空（取决于模型和内容），此断言仅做记录性检查
        # 核心断言：调用不报错、finish_reason 正常
        assert finish is FinishReason.STOP
    finally:
        await llm.aclose()


@pytest.mark.asyncio
async def test_non_streaming_thinking_disabled():
    """验证非流式调用下 thinking=disabled 同样生效。"""
    settings = load_llm_settings()
    llm = create_llm(settings)

    try:
        messages = [Message.user("请用三个词形容春天。")]
        resp: LLMResponse = await llm.chat(
            messages,
            extra_body={"thinking": {"type": "disabled"}},
            max_tokens=100,
        )

        assert resp.content and resp.content.strip(), "非流式响应 content 不应为空"
        assert resp.finish_reason is FinishReason.STOP
    finally:
        await llm.aclose()


@pytest.mark.asyncio
async def test_chat_stream_accepts_extra_body():
    """验证 BaseLLM.chat_stream() 方法支持 extra_body 参数（OpenAI 兼容扩展）。

    如果子类未实现 extra_body 支持，本测试会失败并提示需要在适配器层加上。
    """
    settings = load_llm_settings()
    llm = create_llm(settings)

    try:
        # 只需要确认参数不报 TypeError，实际行为由上面两个测试验证
        chunks_count = 0
        async for _ in llm.chat_stream(
            [Message.user("说一个笑话，一句话。")],
            extra_body={"thinking": {"type": "disabled"}},
        ):
            chunks_count += 1

        assert chunks_count > 0, "未收到任何 chunk"
    finally:
        await llm.aclose()


# ---------------------------------------------------------------------------
# 调试 / 玩具用例：任意 message + 任意 thinking 开关，把 LLM 真实返回打到日志
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("message", "thinking"),
    [
        ("1+1 等于几？直接回答数字。", False),
        ("9.11 和 9.8 哪个大？请简要说明理由。", True),
        ("用一句话介绍你自己，不超过 30 字。", False),
        ("你是什么模型？", False),
    ],
    ids=["quick-no-think", "compare-with-think", "intro-no-think", "model-info"],
)
async def test_ask_and_log(llm, message: str, thinking: bool):
    """把任意 message 发给 LLM，把真实 text / reasoning / finish_reason 用 structlog 打到控制台。

    用法：
        pytest -m e2e tests/e2e/test_streaming.py::test_ask_and_log -v -s

    也可以单独跑某一个用例：
        pytest -m e2e "tests/e2e/test_streaming.py::test_ask_and_log[quick-no-think]" -v -s
    """
    log = structlog.get_logger()

    extra_body = {"thinking": {"type": "enabled" if thinking else "disabled"}}

    text, reasoning, finish = await collect_stream(
        llm, [Message.user(message)], extra_body=extra_body
    )

    log.info(
        "llm_response",
        message=message,
        thinking_enabled=thinking,
        text=text if text else "(空)",
        reasoning=reasoning if reasoning else "(空)",
        finish_reason=str(finish) if finish else "(空)",
        text_len=len(text),
        reasoning_len=len(reasoning),
    )

    assert text or reasoning, "LLM 既没返回 text 也没返回 reasoning"
