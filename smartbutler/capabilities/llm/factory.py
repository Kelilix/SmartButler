"""LLM 能力层 — 工厂路由（按文档 §5.2）。

create_llm(settings) -> BaseLLM 按 settings.provider 路由到具体实现。
当前阶段支持的 provider：
- openai：OpenAI 兼容协议（覆盖 DeepSeek、Qwen、OpenRouter、Azure 兼容模式）

后续若接入 Anthropic / Ollama 等非 OpenAI 兼容厂商,在此处追加 elif 分支。
"""

from __future__ import annotations

from smartbutler.capabilities.llm.base import BaseLLM
from smartbutler.capabilities.llm.openai_compatible import OpenAICompatibleLLM
from smartbutler.config.llm import LLMSettings


def create_llm(settings: LLMSettings) -> BaseLLM:
    """根据 settings.provider 路由到具体 LLM 实现。

    Args:
        settings: LLM 配置（由 load_llm_settings() 加载）。

    Returns:
        BaseLLM: 已构造完成的 LLM 实例（已绑定 httpx.AsyncClient）。

    Raises:
        ValueError: provider 不被支持,或配置不完整。
    """
    provider = settings.provider.lower()

    if provider == "openai":
        api_key = settings.openai_api_key
        base_url = settings.openai_base_url
        if not api_key:
            msg = "provider=openai 需要 SMARTBUTLER_LLM_OPENAI_API_KEY,请检查 .env 或环境变量"
            raise ValueError(msg)
        if not base_url:
            msg = "provider=openai 需要 SMARTBUTLER_LLM_OPENAI_BASE_URL,请检查 .env 或环境变量"
            raise ValueError(msg)
        return OpenAICompatibleLLM(
            settings=settings,
            api_key=api_key,
            base_url=base_url,
        )

    msg = f"暂不支持的 LLM provider: {provider!r}（当前支持: openai）"
    raise ValueError(msg)
