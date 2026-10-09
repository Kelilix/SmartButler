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
    # Phase 6.1 起改为支持嵌入式模式：默认 path = data/qdrant,零部署成本。
    # 用户在 .env 显式设 SMARTBUTLER_STORAGE_QDRANT_URL=http://... 时切到服务模式
    # (Phase 8+ 部署到中心化机器或团队共享时用)。
    qdrant_url: str | None = Field(default=None, description="Qdrant 服务 URL（设了则走服务模式；不设走嵌入式 path=）")
    qdrant_path: Path = Field(
        default=PROJECT_ROOT / "data" / "qdrant",
        description="Qdrant 嵌入式模式数据目录（QdrantClient(path=...)）",
    )
    qdrant_api_key: str | None = Field(default=None, description="Qdrant API key（服务模式才用）")
    qdrant_collection: str = Field(
        default="smartbutler_memory",
        description="默认向量集合名",
    )
    # Phase 6.1:向量维度。1536 是 OpenAI text-embedding-3-small/ada-002 的默认维度,
    # 也跟 DeepSeek/Qwen 的常用嵌入维度兼容。Phase 6.2 接 langmem 后会按嵌入模型
    # 自动匹配;此字段先给一个安全 default,避免 Qdrant 嵌入式启动报"维度不匹配"。
    qdrant_vector_size: int = Field(
        default=1536,
        description="Qdrant 集合向量维度（必须跟嵌入模型输出一致）",
    )
    qdrant_distance: str = Field(
        default="Cosine",
        description="向量距离度量（Cosine / Euclid / Dot）",
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
