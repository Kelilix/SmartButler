"""tests/unit/capabilities/llm/test_factory.py — 工厂路由测试。"""

from __future__ import annotations

import pytest

from smartbutler.capabilities.llm.base import BaseLLM
from smartbutler.capabilities.llm.factory import create_llm
from smartbutler.capabilities.llm.openai_compatible import OpenAICompatibleLLM
from smartbutler.config.llm import LLMSettings


def _settings(
    *,
    provider: str = "openai",
    model: str = "deepseek-flash",
    api_key: str | None = "sk-test",
    base_url: str | None = "https://api.deepseek.com",
    temperature: float = 0.5,
    max_tokens: int = 1024,
) -> LLMSettings:
    return LLMSettings(
        provider=provider,
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
        openai_api_key=api_key,
        openai_base_url=base_url,
    )


class TestFactoryOpenAI:
    def test_returns_base_llm_instance(self) -> None:
        llm = create_llm(_settings())
        assert isinstance(llm, BaseLLM)
        assert isinstance(llm, OpenAICompatibleLLM)

    def test_missing_api_key_raises(self) -> None:
        with pytest.raises(ValueError, match="SMARTBUTLER_LLM_OPENAI_API_KEY"):
            create_llm(_settings(api_key=None))

    def test_missing_base_url_raises(self) -> None:
        with pytest.raises(ValueError, match="SMARTBUTLER_LLM_OPENAI_BASE_URL"):
            create_llm(_settings(base_url=None))

    def test_unknown_provider_raises(self) -> None:
        with pytest.raises(ValueError, match="暂不支持的 LLM provider"):
            create_llm(_settings(provider="anthropic"))
