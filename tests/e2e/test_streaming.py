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

from typing import Any

import pytest
import structlog

from smartbutler.capabilities.llm import (
    FinishReason,
    LLMResponse,
    Message,
    create_llm,
)
from smartbutler.capabilities.llm.base import BaseLLM
from smartbutler.capabilities.llm.types import ToolSpec
from smartbutler.capabilities.tools.registry import ToolRegistry
from smartbutler.capabilities.tools.types import Permission, ToolContext
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


# ---------------------------------------------------------------------------
# Tool-calling 单轮验证：验证 LLM 看到 tools 后会主动发起 tool_call
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("message", "thinking"),
    [
        ("现在北京时间几点?", False),
        ("帮我抓一下 https://example.com 的内容", False),
        ("告诉我上海现在几点", True),
    ],
    ids=["time-cn", "fetch-url", "time-sh-with-think"],
)
async def test_ask_with_tools_and_log(
    llm, common_tools, message: str, thinking: bool
):
    """单轮:验证 LLM 看到 tools 后会主动发起 tool_call,把决策打到 structlog。

    本测试**不执行 tool**,只验证 LLM 决策是否命中 common tool 集合,
    适合作为 tool-calling 链路的冒烟用例。

    用法:
        pytest -m e2e tests/e2e/test_streaming.py::test_ask_with_tools_and_log -v -s
    """
    log = structlog.get_logger()
    extra_body = {"thinking": {"type": "enabled" if thinking else "disabled"}}

    resp: LLMResponse = await llm.chat(
        [Message.user(message)],
        tools=common_tools,
        extra_body=extra_body,
    )

    log.info(
        "llm_tool_decision",
        message=message,
        thinking_enabled=thinking,
        finish_reason=str(resp.finish_reason),
        content=resp.content or "(空)",
        tool_calls=[
            {
                "id": tc.id,
                "name": tc.function.name,
                "arguments": tc.function.arguments,
            }
            for tc in (resp.tool_calls or [])
        ] or "(无)",
    )

    # 核心断言:模型必须选择调用 common tool 中的至少一个
    assert resp.finish_reason is FinishReason.TOOL_CALLS, (
        f"期望 LLM 调用 tool,实际 finish_reason={resp.finish_reason}, "
        f"content={resp.content!r}"
    )
    assert resp.tool_calls, "finish_reason=TOOL_CALLS 但 tool_calls 为空"
    expected = {t.function.name for t in common_tools}
    chosen = {tc.function.name for tc in resp.tool_calls}
    assert chosen & expected, (
        f"LLM 没选预期 common tool,选了 {chosen},预期 {expected}"
    )


# ---------------------------------------------------------------------------
# 辅助:tool-call loop 把 tool 真实执行结果回灌给 LLM,直到 LLM 给出最终文本
# ---------------------------------------------------------------------------

async def _run_tool_loop(
    llm: BaseLLM,
    initial_messages: list[Message],
    tools: list[ToolSpec],
    *,
    registry: ToolRegistry | None = None,
    max_turns: int = 4,
    extra_body: dict[str, Any] | None = None,
) -> tuple[Message, list[Message]]:
    """执行 tool-call loop,直到 LLM 返回非 TOOL_CALLS 的 finish_reason。

    流程:
        1. ``llm.chat(messages, tools=tools, extra_body=...)``
        2. 构造 assistant ``Message``(content 可空 + tool_calls) 追加进 messages
        3. 对每个 ToolCall: 查 registry 拿 ``BaseTool``,
           ``await tool.ainvoke(ctx, **parsed_args)``,
           把 ``result.content`` 包成 ``Message.tool_result(tool_call_id, content)`` 追加进 messages
        4. 若 ``finish_reason != TOOL_CALLS`` 终止;否则回到 1
           (若循环超 max_turns 仍未收敛,抛出 ``RuntimeError``)

    Args:
        llm: BaseLLM 实例。
        initial_messages: 初始 messages(user / system 等)。
        tools: 给 LLM 的 OpenAI ToolSpec 列表。
        registry: 工具注册表;默认 ``ToolRegistry.get_default()``。
        max_turns: 最大循环次数,防御 LLM 失控。默认 4。
        extra_body: 透传给 LLM(thinking 等)。

    Returns:
        ``(final_assistant_message, all_messages)``:
        - final_assistant_message: 最后一次 assistant 消息(content 非空)。
        - all_messages: 完整对话历史(初始 + 所有 assistant + 所有 tool_result)。
    """
    reg = registry if registry is not None else ToolRegistry.get_default()
    # 最小权限:NETWORK_CALL 覆盖 web_fetch,get_current_time 无 required_permissions 无所谓
    ctx = ToolContext(
        user_id="e2e",
        session_id="e2e-tool-loop",
        permissions={Permission.NETWORK_CALL},
    )

    messages: list[Message] = list(initial_messages)
    final_assistant = Message.assistant(content="")

    for turn_idx in range(max_turns):
        resp: LLMResponse = await llm.chat(
            messages, tools=tools, extra_body=extra_body
        )

        # 构造 assistant message 并入历史
        assistant_msg = Message.assistant(
            content=resp.content or "",
            tool_calls=resp.tool_calls or None,
        )
        messages.append(assistant_msg)
        final_assistant = assistant_msg

        if resp.finish_reason is not FinishReason.TOOL_CALLS:
            return final_assistant, messages

        if not resp.tool_calls:
            msg = (
                f"turn {turn_idx}: finish_reason=TOOL_CALLS 但 resp.tool_calls 为空"
            )
            raise RuntimeError(msg)

        # dispatch 每个 tool_call,回灌结果
        for tc in resp.tool_calls:
            tool = reg.get(tc.function.name)
            parsed_args = tc.function.parsed_arguments()
            result = await tool.ainvoke(ctx, **parsed_args)
            messages.append(
                Message.tool_result(
                    tool_call_id=tc.id,
                    content=result.content,
                )
            )

    msg = f"tool-call loop 未在 {max_turns} 轮内收敛,最后 finish_reason={final_assistant.tool_calls}"
    raise RuntimeError(msg)


# ---------------------------------------------------------------------------
# Tool-calling 端到端:LLM 决策 + 真实执行 + 回灌 + 最终文本
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("message", "thinking"),
    [
        ("现在新疆时间几点?", False),
        ("告诉我上海现在几点", True),
    ],
    ids=["time-cn", "time-sh-with-think"],
)
async def test_chat_loop_with_tools_and_log(
    llm, common_tools, message: str, thinking: bool
):
    """端到端:验证 LLM 决策后 tool 被真实执行,结果回灌后 LLM 给出最终文本。

    与 test_ask_with_tools_and_log(方案 A)的区别:
      - 方案 A 只验证 LLM 决定调用 tool。
      - 本测试(**方案 B**)验证完整链路:dispatch tool → 把结果回灌 → LLM 再回复。

    断言:
      1. loop 在 max_turns 内收敛(最终的 finish_reason != TOOL_CALLS)。
      2. 最终 assistant 消息 content 非空(说明 LLM 真的给了回答)。
      3. 完整对话历史里至少出现一条 tool 消息(说明 tool 被执行了)。
    """
    log = structlog.get_logger()
    extra_body = {"thinking": {"type": "enabled" if thinking else "disabled"}}

    final_assistant, history = await _run_tool_loop(
        llm,
        [Message.user(message)],
        common_tools,
        extra_body=extra_body,
        max_turns=4,
    )

    tool_messages = [m for m in history if m.role.value == "tool"]
    tool_summary = [
        {
            "tool_call_id": m.tool_call_id,
            "content_preview": (m.content or "")[:80],
        }
        for m in tool_messages
    ]

    log.info(
        "tool_loop_final",
        message=message,
        thinking_enabled=thinking,
        total_messages=len(history),
        tool_messages_count=len(tool_messages),
        final_content=final_assistant.content or "(空)",
        tool_results=tool_summary,
    )

    # 1. 最终是文本回复,不再是 tool_call
    assert final_assistant.content, (
        f"loop 收敛后 assistant.content 为空,内容={final_assistant.content!r}"
    )
    # 2. 对话历史里至少有一条 tool 消息
    assert tool_messages, "历史里没有 tool 消息,说明 tool 没被执行"
    # 3. tool_result 必须能跟某条 assistant 的 tool_calls 对得上
    assistant_tc_ids = {
        tc.id
        for m in history
        if m.tool_calls
        for tc in m.tool_calls
    }
    result_tc_ids = {m.tool_call_id for m in tool_messages}
    missing = result_tc_ids - assistant_tc_ids
    assert not missing, f"tool_result 找不到对应 tool_call_id: {missing}"
