"""tests/unit/capabilities/llm/test_factory.py — 工厂路由测试。"""

from __future__ import annotations

import pytest

from smartbutler.capabilities.llm.base import BaseLLM
from smartbutler.capabilities.llm.factory import create_llm
from smartbutler.capabilities.llm.langchain_adapter import LangChainLLMAdapter
from smartbutler.capabilities.llm.openai_compatible import OpenAICompatibleLLM
from smartbutler.config.llm import LLMSettings


def _settings(
    *,
    provider: str = "openai",
    backend: str = "http",
    model: str = "deepseek-flash",
    api_key: str | None = "sk-test",
    base_url: str | None = "https://api.deepseek.com",
    temperature: float = 0.5,
    max_tokens: int = 1024,
) -> LLMSettings:
    return LLMSettings(
        provider=provider,
        backend=backend,
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
        openai_api_key=api_key,
        openai_base_url=base_url,
    )


class TestFactoryHttpBackend:
    """backend=http（默认）路由到 OpenAICompatibleLLM。"""

    def test_default_backend_is_http(self) -> None:
        # 不传 backend,应默认走 http
        settings = LLMSettings(
            provider="openai",
            model="deepseek-flash",
            openai_api_key="sk-test",
            openai_base_url="https://api.deepseek.com",
        )
        llm = create_llm(settings)
        assert isinstance(llm, BaseLLM)
        assert isinstance(llm, OpenAICompatibleLLM)

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

    def test_http_with_non_openai_provider_raises(self) -> None:
        with pytest.raises(ValueError, match="backend=http 当前仅支持 provider=openai"):
            create_llm(_settings(provider="anthropic"))


class TestFactoryLangChainBackend:
    """backend=langchain 路由到 LangChainLLMAdapter。"""

    def test_returns_langchain_adapter(self) -> None:
        llm = create_llm(_settings(backend="langchain"))
        assert isinstance(llm, BaseLLM)
        assert isinstance(llm, LangChainLLMAdapter)

    def test_missing_api_key_raises(self) -> None:
        with pytest.raises(ValueError, match="SMARTBUTLER_LLM_OPENAI_API_KEY"):
            create_llm(_settings(backend="langchain", api_key=None))

    def test_missing_base_url_raises(self) -> None:
        with pytest.raises(ValueError, match="SMARTBUTLER_LLM_OPENAI_BASE_URL"):
            create_llm(_settings(backend="langchain", base_url=None))

    def test_langchain_with_non_openai_provider_raises(self) -> None:
        with pytest.raises(
            ValueError, match="backend=langchain 当前仅支持 provider=openai"
        ):
            create_llm(_settings(backend="langchain", provider="anthropic"))


class TestFactoryInvalidBackend:
    def test_unknown_backend_raises(self) -> None:
        with pytest.raises(ValueError, match="暂不支持的 LLM backend"):
            create_llm(_settings(backend="something-weird"))

    def test_unknown_provider_raises(self) -> None:
        # backend=http 时,非 openai provider 必须报错
        with pytest.raises(ValueError, match="backend=http 当前仅支持 provider=openai"):
            create_llm(_settings(provider="unknown-provider"))
