"""LLM 能力层 — 工厂路由（按文档 §5.2）。

``create_llm(settings) -> BaseLLM`` 按 ``settings.backend`` 路由到具体实现。

当前支持的 backend：
- ``http``（默认）：``OpenAICompatibleLLM``，自建 httpx 直调 OpenAI 兼容协议。
- ``langchain``：``LangChainLLMAdapter``，包装 ``langchain_openai.ChatOpenAI``，
  复用 LangChain 的消息转换 / 工具绑定 / 流式 chunk 处理能力。

backend 之上还按 ``provider`` 区分厂商组合（openai / 后续 anthropic / ollama）。
当前 backend=http 仅支持 provider=openai；backend=langchain 当前仅支持
provider=openai（DeepSeek / Qwen / OpenRouter 等 OpenAI 兼容协议厂商
通过 base_url 切换实现）。
"""

from __future__ import annotations

import structlog

from smartbutler.capabilities.llm.base import BaseLLM
from smartbutler.capabilities.llm.langchain_adapter import LangChainLLMAdapter
from smartbutler.capabilities.llm.openai_compatible import OpenAICompatibleLLM
from smartbutler.config.llm import LLMSettings

_logger = structlog.get_logger(__name__)


def create_llm(settings: LLMSettings) -> BaseLLM:
    """根据 settings.backend / settings.provider 路由到具体 LLM 实现。

    Args:
        settings: LLM 配置（由 load_llm_settings() 加载）。

    Returns:
        BaseLLM: 已构造完成的 LLM 实例。

    Raises:
        ValueError: backend 或 provider 不被支持,或配置不完整。
    """
    backend = settings.backend.lower()
    provider = settings.provider.lower()

    if backend == "http":
        if provider != "openai":
            msg = (
                f"backend=http 当前仅支持 provider=openai,实际 provider={provider!r}。"
                "如需 Anthropic / Ollama,请使用 backend=langchain 或联系维护者扩展。"
            )
            raise ValueError(msg)
        api_key = settings.openai_api_key
        base_url = settings.openai_base_url
        if not api_key:
            msg = "provider=openai 需要 SMARTBUTLER_LLM_OPENAI_API_KEY,请检查 .env 或环境变量"
            raise ValueError(msg)
        if not base_url:
            msg = "provider=openai 需要 SMARTBUTLER_LLM_OPENAI_BASE_URL,请检查 .env 或环境变量"
            raise ValueError(msg)
        _logger.info(
            "llm.backend_selected",
            backend=backend,
            provider=provider,
            impl="OpenAICompatibleLLM",
            model=settings.model,
            base_url=base_url,
        )
        return OpenAICompatibleLLM(
            settings=settings,
            api_key=api_key,
            base_url=base_url,
        )

    if backend == "langchain":
        if provider != "openai":
            msg = (
                f"backend=langchain 当前仅支持 provider=openai"
                f"（DeepSeek / Qwen / OpenRouter / Azure 兼容）,实际 provider={provider!r}"
            )
            raise ValueError(msg)
        api_key = settings.openai_api_key
        base_url = settings.openai_base_url
        if not api_key:
            msg = (
                "backend=langchain + provider=openai 需要 "
                "SMARTBUTLER_LLM_OPENAI_API_KEY,请检查 .env 或环境变量"
            )
            raise ValueError(msg)
        if not base_url:
            msg = (
                "backend=langchain + provider=openai 需要 "
                "SMARTBUTLER_LLM_OPENAI_BASE_URL,请检查 .env 或环境变量"
            )
            raise ValueError(msg)
        _logger.info(
            "llm.backend_selected",
            backend=backend,
            provider=provider,
            impl="LangChainLLMAdapter",
            model=settings.model,
            base_url=base_url,
        )
        return LangChainLLMAdapter(
            settings=settings,
            api_key=api_key,
            base_url=base_url,
        )

    msg = (
        f"暂不支持的 LLM backend: {backend!r}"
        "（当前支持: http / langchain）。可通过 SMARTBUTLER_LLM_BACKEND 配置。"
    )
    raise ValueError(msg)