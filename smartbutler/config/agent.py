"""Agent 子模块配置（按文档 §3.2.3）。

Agent 层对外的关键约定：
- 所有 Agent 必须实现 agents/base/ 中的接口契约（name / description / tools / handle）
- agents/manager/ 是唯一对外的注册/调度入口
- 本配置仅描述 Agent 的运行时参数（超时、重试、最大并发等），
  不存放具体 Agent 的 YAML（那部分在 storage.agent_config_dir）
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class AgentSettings(BaseSettings):
    """Agent 子模块配置。

    环境变量前缀：``SMARTBUTLER_AGENT_``
    """

    model_config = SettingsConfigDict(
        env_prefix="SMARTBUTLER_AGENT_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # Agent 通用运行时参数
    default_timeout: float = Field(default=60.0, gt=0.0, description="Agent.handle() 默认超时（秒）")
    default_max_retries: int = Field(default=3, ge=0, description="Agent 失败重试次数")
    max_concurrent_agents: int = Field(default=8, gt=0, description="并发 Agent 数上限")

    # Manager 注册相关
    auto_discover: bool = Field(
        default=True,
        description="启动时是否自动发现并注册所有内置 Agent（home / schedule / search）",
    )


def load_agent_settings() -> AgentSettings:
    """加载 Agent 子模块配置。"""
    return AgentSettings()


__all__ = ["AgentSettings", "load_agent_settings"]
