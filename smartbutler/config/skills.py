"""Skill 子模块配置(参考 2026-10-09 决议)。

字段:
- workspace_dir:工作目录根(默认 ./{work_dir})
- builtin_skills_dir:内置 skill 目录(随代码仓库分发)
- user_skills_dir:用户级 skill 目录(默认 ~/.smartbutler/skills/,
  留空可关;Phase 7+ 多源加载)

环境变量前缀:SMARTBUTLER_SKILL_

注意:workspace_dir / builtin_skills_dir / user_skills_dir 默认相对路径,
会以 PROJECT_ROOT 解析成绝对路径;user_skills_dir 走 ~ 展开。
"""
from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from smartbutler.config.base import PROJECT_ROOT


class SkillSettings(BaseSettings):
    """Skill 子模块配置。

    环境变量前缀:``SMARTBUTLER_SKILL_``
    """

    model_config = SettingsConfigDict(
        env_prefix="SMARTBUTLER_SKILL_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    workspace_dir: Path = Field(
        default=PROJECT_ROOT / "workspace",
        description=(
            "用户工作目录根。LLM 写文件时会创建 {workspace_dir}/YYYYMMDD/ 子目录。"
            "可在 .env 里用 SMARTBUTLER_SKILL_WORKSPACE_DIR 覆盖。"
        ),
    )
    builtin_skills_dir: Path = Field(
        default=PROJECT_ROOT / "smartbutler" / "skills" / "builtin",
        description="内置 Skill 目录(随代码仓库分发)。",
    )
    user_skills_dir: Path | None = Field(
        default=Path("~/.smartbutler/skills").expanduser(),
        description=(
            "用户级 Skill 目录(Phase 7+ 多源)。"
            "默认 ~/.smartbutler/skills/——不存在则静默跳过。"
            "在 .env 里置空(SMARTBUTLER_SKILL_USER_SKILLS_DIR=)可关闭用户源。"
        ),
    )


def load_skill_settings() -> SkillSettings:
    """加载 Skill 子模块配置(每次重新读环境变量,便于运行时切换)。"""
    return SkillSettings()


__all__ = ["SkillSettings", "load_skill_settings"]
