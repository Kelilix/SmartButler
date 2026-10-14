"""Embedding 子模块配置（Phase 6.2 P0）。

环境变量前缀: ``SMARTBUTLER_EMBEDDING_``

启动期校验:
- ``dimension`` 必须跟 ``SMARTBUTLER_STORAGE_QDRANT_VECTOR_SIZE`` 一致,
  不一致 ``load_embedding_settings(strict=True)`` 会抛 ``ValueError``。
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from smartbutler.config.base import PROJECT_ROOT


class EmbeddingSettings(BaseSettings):
    """Embedding 子模块配置。"""

    model_config = SettingsConfigDict(
        env_prefix="SMARTBUTLER_EMBEDDING_",
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    provider: str = Field(
        default="qwen",
        description="Embedder 实现:qwen / stub。生产用 qwen,测试/CI 用 stub。",
    )
    api_key: str | None = Field(
        default=None,
        description="Qwen(OpenAI 兼容)API key。None 时工厂自动降级 StubEmbedder。",
    )
    base_url: str = Field(
        default="https://dashscope.aliyuncs.com/compatible-mode/v1",
        description="Qwen embedding 端点(OpenAI 兼容)。",
    )
    model: str = Field(
        default="qwen3.7-text-embedding-flash",
        description="模型名。qwen3.7-text-embedding-flash 默认 1024 维。",
    )
    dimension: int = Field(
        default=1024,
        description="向量维度。必须跟 qdrant_vector_size 一致。",
    )
    timeout: float = Field(
        default=30.0,
        description="HTTP 请求超时(秒)。",
    )
    max_retries: int = Field(
        default=2,
        description="失败重试次数(指数退避)。",
    )


def load_embedding_settings(
    *, strict: bool = False, qdrant_vector_size: int | None = None
) -> EmbeddingSettings:
    """加载 Embedding 配置。

    Args:
        strict: True 时强制校验 ``dimension`` 与 qdrant_vector_size 一致,
            不一致抛 ``ValueError``(生产启动用)。
        qdrant_vector_size: 传入时与 ``dimension`` 比对;不传则跳过。

    Returns:
        EmbeddingSettings 实例。
    """
    s = EmbeddingSettings()
    if strict and qdrant_vector_size is not None and s.dimension != qdrant_vector_size:
        msg = (
            f"Embedding 维度不匹配:SMARTBUTLER_EMBEDDING_DIMENSION={s.dimension} "
            f"!= SMARTBUTLER_STORAGE_QDRANT_VECTOR_SIZE={qdrant_vector_size}。"
            "请检查 .env,二者必须一致(默认 1024)。"
        )
        raise ValueError(msg)
    return s


__all__ = ["EmbeddingSettings", "load_embedding_settings"]
