"""配置管理（按文档 §3.1 / §4.5）。

本包提供：
- SmartButlerBaseSettings：所有配置类的统一基类。
- 各子模块的独立 Settings 类（LLM / Storage / Logging / Agent）。
- 各子模块的 load_xxx_settings() 工厂函数。

设计原则：
1. 每个子模块配置独立加载、独立校验，避免一次加载全部配置造成启动失败。
2. 所有配置都从环境变量（或 .env 文件）读取，不在代码里硬编码。
3. 子模块配置类必须能被框架后续模块（capabilities / agents / thinking）直接 import 使用。
"""

from smartbutler.config.agent import AgentSettings, load_agent_settings
from smartbutler.config.base import PROJECT_ROOT, SmartButlerBaseSettings
from smartbutler.config.llm import LLMSettings, load_llm_settings
from smartbutler.config.logging import LoggingSettings, load_logging_settings
from smartbutler.config.skills import SkillSettings, load_skill_settings
from smartbutler.config.storage import StorageSettings, load_storage_settings

# EmbeddingSettings 定义在 capabilities/embedding/config.py(那里
# 跟 Embedder 实现绑在一起,放 config 包会让 import 链出现循环);
# 这里做延迟导出,让 ``from smartbutler.config import EmbeddingSettings`` 可用。
__all__ = [
    "PROJECT_ROOT",
    "SmartButlerBaseSettings",
    "LLMSettings",
    "StorageSettings",
    "LoggingSettings",
    "AgentSettings",
    "SkillSettings",
    "EmbeddingSettings",
    "load_llm_settings",
    "load_storage_settings",
    "load_logging_settings",
    "load_agent_settings",
    "load_skill_settings",
    "load_embedding_settings",
]


def __getattr__(name: str):  # type: ignore[no-untyped-def]
    """PEP 562 模块级 __getattr__,延迟导入避免循环。"""
    if name in ("EmbeddingSettings", "load_embedding_settings"):
        from smartbutler.capabilities.embedding.config import (
            EmbeddingSettings as _EmbeddingSettings,
            load_embedding_settings as _load_embedding_settings,
        )
        return {
            "EmbeddingSettings": _EmbeddingSettings,
            "load_embedding_settings": _load_embedding_settings,
        }[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
