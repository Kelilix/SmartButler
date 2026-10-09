"""SkillRuntime —— Skill 子系统统一入口。

职责:
1. 扫描 [builtin_skills_dir, user_skills_dir] 拿到 SkillMetadata 列表
2. 构造 SmartButlerFilesystemBackend(含 PermissionPolicy,多 skills_dir)
3. 构造 6 个文件工具(交给 ToolRegistry 注册)
4. 渲染 skill list 给 ButlerPromptBuilder

使用:
    10|    runtime = SkillRuntime.from_settings()
    tools = runtime.build_file_tools()  # 给 ToolRegistry
    prompt_snippet = runtime.render_prompt_snippet()  # 给 ButlerPromptBuilder

设计:不持有 LLM / graph,纯装配。

Phase 7+ 多源:
- builtin + user 同时加载
- builtin 权威:同名 user skill 被忽略 + 告警
- SkillMetadata.source 字段标注来源
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import structlog

from smartbutler.capabilities.tools.base import BaseTool
from smartbutler.capabilities.tools.skills.fs_tools import build_default_skill_tools
from smartbutler.config.skills import SkillSettings, load_skill_settings
from smartbutler.thinking.skills.filesystem_backend import SmartButlerFilesystemBackend
from smartbutler.thinking.skills.permissions import build_default_policy
from smartbutler.thinking.skills.prompt_renderer import render_skill_list_prompt
from smartbutler.thinking.skills.scanner import SkillMetadata, scan_skills_dirs

_logger = structlog.get_logger(__name__)


@dataclass
class SkillRuntime:
    """Skill 子系统运行时句柄。

    持有:
    - skills_dirs:Skill 目录根列表(Phase 7+ 多源:[builtin, user])
    - work_dir:工作目录根
    - backend:filesystem backend
    - skills:已扫描的 SkillMetadata 列表
    """

    skills_dirs: list[Path]
    work_dir: Path
    backend: SmartButlerFilesystemBackend
    skills: list[SkillMetadata]

    @classmethod
    def from_settings(
        cls,
        settings: SkillSettings | None = None,
    ) -> SkillRuntime:
        """从 SkillSettings 构造运行时(Phase 7+ 多源扫描)。

        步骤:
        1. 加载 settings(默认从 env)
        2. 构造 PermissionPolicy(支持多 skills_dir)
        3. 构造 SmartButlerFilesystemBackend
        4. 扫描 [builtin, user] — builtin 权威,user 同名被忽略 + 告警
        """
        s = settings or load_skill_settings()
        builtin_dir = s.builtin_skills_dir.expanduser().resolve()

        # Phase 7+:user 源。None 或空 = 关闭。
        roots: list[tuple[Path, Literal["builtin", "user"]]] = [
            (builtin_dir, "builtin"),
        ]
        if s.user_skills_dir is not None:
            user_dir = s.user_skills_dir.expanduser().resolve()
            # 与 builtin 同目录时跳过(避免自我叠加)
            if user_dir != builtin_dir:
                roots.append((user_dir, "user"))

        work_dir = s.workspace_dir.expanduser().resolve()
        skills_dirs_for_policy = [r[0] for r in roots]
        policy = build_default_policy(
            work_dir=work_dir,
            skills_dirs=skills_dirs_for_policy,
        )
        backend = SmartButlerFilesystemBackend(work_dir=work_dir, policy=policy)
        skills = scan_skills_dirs(roots)
        _logger.info(
            "skill_runtime.initialized",
            skills_dirs=[str(r[0]) for r in roots],
            work_dir=str(work_dir),
            skill_count=len(skills),
            skill_names=[m.name for m in skills],
        )
        return cls(
            skills_dirs=skills_dirs_for_policy,
            work_dir=work_dir,
            backend=backend,
            skills=skills,
        )

    # ---------- 导出 ----------

    def build_file_tools(self) -> list[BaseTool]:
        """构造 6 个文件工具(给 ToolRegistry.register_many 用)。"""
        return build_default_skill_tools(self.backend)

    def render_prompt_snippet(self) -> str:
        """渲染 skill 列表的 system prompt 片段(给 ButlerPromptBuilder)。"""
        return render_skill_list_prompt(self.skills)

    def find_skill_by_name(self, name: str) -> SkillMetadata | None:
        """按 name 查 skill(LLM 用 read_file 之前先查路径)。"""
        for s in self.skills:
            if s.name == name:
                return s
        return None


__all__ = ["SkillRuntime"]
