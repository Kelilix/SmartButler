"""配置基类。

所有子模块配置类（LLM / Storage / Logging / Agent）继承本类，
统一约定：
- 配置文件：项目根目录下的 .env
- 字段名匹配：大小写不敏感
- 多余字段：忽略（避免 .env 中混入无关变量报错）
- 嵌套分隔符：__ （允许环境变量扁平化嵌套字段）
"""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import SettingsConfigDict

# 项目根目录：smartbutler/config/base.py -> 项目根
PROJECT_ROOT: Path = Path(__file__).resolve().parents[2]


class SmartButlerBaseSettings:
    """所有配置类的统一基类（仅声明 model_config，不持有字段）。

    子类继承本类后，必须在自身上设置 env_prefix（子模块前缀），
    然后定义具体字段。例如：

        class LLMSettings(SmartButlerBaseSettings):
            model_config = SettingsConfigDict(env_prefix="SMARTBUTLER_LLM_")
            provider: str = "openai"
    """

    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        extra="ignore",
        case_sensitive=False,
    )
