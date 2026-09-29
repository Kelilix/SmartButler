"""存储子模块配置（按文档 §4.3）。

不同类型的数据使用不同的存储后端：
- 对话上下文（短期）：SQLite 或 Redis
- 用户记忆（长期）：SQLite + Qdrant
- 性格状态：SQLite
- Agent 配置：YAML/TOML（版本友好）

本配置仅描述后端选择与连接信息，具体后端实现位于
``smartbutler/storage/`` 各子目录（待后续模块实现）。
"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from smartbutler.config.base import PROJECT_ROOT


class StorageSettings(BaseSettings):
    """存储子模块配置。

    环境变量前缀：``SMARTBUTLER_STORAGE_``
    """

    model_config = SettingsConfigDict(
        env_prefix="SMARTBUTLER_STORAGE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---- SQLite（默认后端） ----
    sqlite_path: Path = Field(
        default=PROJECT_ROOT / "data" / "smartbutler.db",
        description="SQLite 数据库文件路径（默认存储）",
    )

    # ---- 向量库（Qdrant） ----
    qdrant_url: str | None = Field(default=None, description="Qdrant 服务 URL")
    qdrant_api_key: str | None = Field(default=None, description="Qdrant API key")
    qdrant_collection: str = Field(
        default="smartbutler_memory",
        description="默认向量集合名",
    )

    # ---- Redis（可选，用于高频短期数据） ----
    redis_url: str | None = Field(default=None, description="Redis 连接 URL")

    # ---- Agent 配置目录 ----
    agent_config_dir: Path = Field(
        default=PROJECT_ROOT / "config" / "agents",
        description="Agent YAML 配置文件目录",
    )


def load_storage_settings() -> StorageSettings:
    """加载存储子模块配置。"""
    return StorageSettings()


__all__ = ["StorageSettings", "load_storage_settings"]
