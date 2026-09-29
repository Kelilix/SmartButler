"""日志子模块配置（按文档 §4.5）。"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from smartbutler.config.base import PROJECT_ROOT


class LoggingSettings(BaseSettings):
    """日志子模块配置。

    环境变量前缀：``SMARTBUTLER_LOGGING_``
    """

    model_config = SettingsConfigDict(
        env_prefix="SMARTBUTLER_LOGGING_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    level: str = Field(
        default="INFO",
        description="日志级别：DEBUG / INFO / WARNING / ERROR",
    )
    json_output: bool = Field(
        default=False,
        description="是否输出 JSON 格式（生产环境建议 True）",
    )
    log_file: Path | None = Field(
        default=PROJECT_ROOT / "logs" / "smartbutler.log",
        description="日志文件路径；为 None 时仅输出到 stdout",
    )
    rotation: str = Field(
        default="100 MB",
        description="日志切分阈值（保留字段；后续接入 loguru 时启用）",
    )
    retention: str = Field(
        default="14 days",
        description="日志保留时长（保留字段；后续接入 loguru 时启用）",
    )


def load_logging_settings() -> LoggingSettings:
    """加载日志子模块配置。"""
    return LoggingSettings()


__all__ = ["LoggingSettings", "load_logging_settings"]
