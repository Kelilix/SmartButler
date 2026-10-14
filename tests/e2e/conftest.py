"""tests/e2e/conftest.py — 共享 fixtures 与日志配置。"""

from __future__ import annotations

import sys

# 必须在导入 structlog 之前重配置 stdout，否则后续 PrintLoggerFactory
# 会捕获到 GBK 编码的控制台，导致 LLM 返回的中文 / 数学符号抛 UnicodeEncodeError。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]

import pytest
import structlog

from smartbutler.capabilities.llm import create_llm
from smartbutler.capabilities.tools.common import (  # noqa: F401  触发 @register_tool
    get_current_time,
    web_fetch,
)
from smartbutler.capabilities.tools.registry import ToolRegistry
from smartbutler.config import load_llm_settings

# ---------------------------------------------------------------------------
# structlog 控制台渲染配置（仅作用于 e2e 测试）
# ---------------------------------------------------------------------------

def pytest_configure(config: pytest.Config) -> None:
    """让 e2e 测试里的 structlog 调用输出到控制台而非默认静默通道。"""
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="%H:%M:%S", utc=False),
            structlog.dev.ConsoleRenderer(colors=False),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(20),  # INFO
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )


@pytest.fixture
async def llm():
    """创建一个 LLM 实例（使用 .env 中的配置）。"""
    settings = load_llm_settings()
    llm = create_llm(settings)
    try:
        yield llm
    finally:
        await llm.aclose()


@pytest.fixture
def common_tools():
    """取出 common 目录下的 tool（get_current_time / web_fetch）的 OpenAI 协议描述。

    供 tool-calling e2e 测试使用（test_ask_with_tools_and_log 等）。
    返回 list[ToolSpec]，可直接喂给 ``llm.chat(tools=...)``。
    """
    wanted = {"get_current_time", "web_fetch"}
    return [
        tool.to_tool_spec()
        for tool in ToolRegistry.get_default().get_global()
        if tool.name in wanted
    ]
