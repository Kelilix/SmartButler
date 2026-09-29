"""LLM 子模块配置（按文档 §4.2）。

字段说明：
- provider：选择哪个 LLM 实现（如 openai / anthropic / ollama）。
- model：当前 provider 下的具体模型名。
- temperature / max_tokens / timeout：通用采样参数。
- provider 专属字段（api_key、base_url）按 provider 独立配置，
  运行时 capabilities/llm 工厂读取本配置创建对应的 ChatModel。
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class LLMSettings(BaseSettings):
    """LLM 子模块配置。

    环境变量前缀：``SMARTBUTLER_LLM_``
    示例：``SMARTBUTLER_LLM_PROVIDER=anthropic``
    """

    model_config = SettingsConfigDict(
        env_prefix="SMARTBUTLER_LLM_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---- 通用 ----
    provider: str = Field(
        default="openai",
        description="LLM 厂商标识：openai / anthropic / ollama / ...",
    )
    model: str = Field(
        default="gpt-4o-mini",
        description="provider 下的具体模型名",
    )
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    max_tokens: int = Field(default=4096, gt=0)
    timeout: float = Field(default=30.0, gt=0.0, description="单次调用超时（秒）")
    max_retries: int = Field(default=2, ge=0)

    # ---- OpenAI 兼容 ----
    openai_api_key: str | None = Field(default=None, description="OpenAI API key")
    openai_base_url: str | None = Field(
        default=None,
        description="OpenAI 兼容 base_url（用于自建网关 / 第三方代理）",
    )

    # ---- Anthropic ----
    anthropic_api_key: str | None = Field(default=None, description="Anthropic API key")
    anthropic_base_url: str | None = Field(default=None, description="Anthropic base_url")

    # ---- 本地（Ollama 等） ----
    ollama_base_url: str | None = Field(default=None, description="Ollama 服务地址")


def load_llm_settings() -> LLMSettings:
    """加载 LLM 子模块配置。

    每次调用都重新读取环境变量，便于在测试或运行时动态切换 provider。
    """
    return LLMSettings()


__all__ = ["LLMSettings", "load_llm_settings"]
