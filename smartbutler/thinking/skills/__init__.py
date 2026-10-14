"""SmartButler 技能(Skill)系统 — Phase 5+。

参考文档:
- TECHNICAL_DESIGN.md §3.2.7 skills/ (Phase 5 目标)
- ADR-005 §5.5.5 skills 决策
- 2026-10-09 决议:基于 LangChain Deep Agents 中间件思路自建精简版

设计原则:
1. **零侵入**:业务层不 import LangChain 中间件,只看到 Skill 的 list 视图。
2. **沙箱化**:写文件只能在 work_dir/ 或 skills/builtin/<name>/ 下。
3. **声明式权限**:FilesystemPermission 三类(allow / interrupt / deny)。
4. **按日期工作目录**:work_dir/YYYYMMDD/ 自动创建。
5. **可演进**:Phase 5 阶段扫描 skills/builtin/ 静态加载;Phase 7+ 接 LangChain 中间件。

包结构:
- scanner.py:扫描 SKILL.md 解析 frontmatter
- runtime.py:SkillRuntime(扫描结果聚合 + 中间件 hook 包装)
- prompt_renderer.py:把 skill 列表渲染成 system prompt 片段
- permissions.py:FilesystemPermission 构造器
- filesystem_backend.py:精简版 pathlib backend

入口 SkillRuntime.from_settings() 一行装配。
"""

from smartbutler.thinking.skills.filesystem_backend import (
    BackendError,
    GlobOutput,
    GrepOutput,
    ListOutput,
    PathOutsideSandbox,
    PermissionRejected,
    ReadOutput,
    SmartButlerFilesystemBackend,
)
from smartbutler.thinking.skills.permissions import (
    FileOperation,
    PermissionMode,
    PermissionPolicy,
    PermissionRule,
    build_default_policy,
    resolve_today_workspace,
)
from smartbutler.thinking.skills.prompt_renderer import (
    render_skill_body_prompt,
    render_skill_list_prompt,
)
from smartbutler.thinking.skills.runtime import SkillRuntime
from smartbutler.thinking.skills.scanner import (
    SkillMetadata,
    SkillScanError,
    parse_skill_md,
    scan_skills_dir,
    scan_skills_dirs,
)

__all__ = [
    # runtime
    "SkillRuntime",
    # scanner
    "SkillMetadata",
    "SkillScanError",
    "parse_skill_md",
    "scan_skills_dir",
    "scan_skills_dirs",
    # prompt renderer
    "render_skill_list_prompt",
    "render_skill_body_prompt",
    # permissions
    "FileOperation",
    "PermissionMode",
    "PermissionRule",
    "PermissionPolicy",
    "build_default_policy",
    "resolve_today_workspace",
    # backend
    "SmartButlerFilesystemBackend",
    "ReadOutput",
    "ListOutput",
    "GrepOutput",
    "GlobOutput",
    "BackendError",
    "PermissionRejected",
    "PathOutsideSandbox",
]
