"""SkillRuntime —— Skill 子系统统一入口。

职责:
1. 扫描 [builtin_skills_dir, user_skills_dir] 拿到 SkillMetadata 列表
2. 构造 SmartButlerFilesystemBackend(含 PermissionPolicy,多 skills_dir)
3. 构造 6 个文件工具(交给 ToolRegistry 注册)
4. 渲染 skill list 给 ButlerPromptBuilder

使用:
    runtime = SkillRuntime.from_settings()
    tools = runtime.build_file_tools()  # 给 ToolRegistry
    prompt_snippet = runtime.render_prompt_snippet()  # 给 ButlerPromptBuilder

设计:不持有 LLM / graph,纯装配。

Phase 7+ 多源:
- builtin + user 同时加载
- builtin 权威:同名 user skill 被忽略 + 告警
- SkillMetadata.source 字段标注来源

🆕 Phase 7+ 热更新(2026-10-09 落地):
- 借鉴 deepagents `before_agent` 钩子 + `state["skills_metadata"]=None` 触发重扫的设计思想
- 不接 LangChain `AgentMiddleware` 抽象(与 §5.7.6 决策一致)
- 机制:惰性 mtime 检测 + 重扫,配置开关 `enable_hot_reload`
- 检测时机:每次 `render_prompt_snippet()` / `find_skill_by_name()` 之前
- 失败回退:重扫失败 → 保留旧缓存 + warn(不抛)
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import structlog

from smartbutler.capabilities.tools.base import BaseTool
from smartbutler.config.skills import SkillSettings, load_skill_settings
from smartbutler.thinking.skills.filesystem_backend import SmartButlerFilesystemBackend
from smartbutler.thinking.skills.llm_tools import build_default_skill_tools
from smartbutler.thinking.skills.permissions import build_default_policy
from smartbutler.thinking.skills.prompt_renderer import render_skill_list_prompt
from smartbutler.thinking.skills.scanner import SkillMetadata, scan_skills_dirs

_logger = structlog.get_logger(__name__)


def _compute_skills_fingerprint(skills_dirs: list[Path]) -> float:
    """计算 skills_dirs 树的文件级 mtime 指纹(取所有文件最大 mtime)。

    用于热更新检测:值变化 → 触发重扫。0 表示树不存在(尚未创建)。

    覆盖范围(用户 4 类操作):
    ① 新增/删除 skill 目录 → 根 mtime 变
    ② 修改 SKILL.md → SKILL.md mtime 变
    ③ 修改 skill/scripts/* → 脚本 mtime 变
    ④ 修改 skill/references/* → 参考 mtime 变
    """
    max_mtime = 0.0
    for root in skills_dirs:
        if not root.exists():
            continue
        try:
            for path in root.rglob("*"):
                if path.is_file():
                    try:
                        max_mtime = max(max_mtime, os.stat(path).st_mtime)
                    except OSError:
                        continue
        except OSError:
            continue
    # 文件路径集合混入 hash,避免"删除/新增 skill 目录但 mtime 巧合相同"的漏检
    # (Windows FAT mtime 精度 = 2s;NTFS 100ns,但用户批量写文件常落在同一时间)
    files: list[str] = []
    for root in skills_dirs:
        if not root.exists():
            continue
        try:
            for path in root.rglob("*"):
                if path.is_file():
                    files.append(str(path.relative_to(root)))
        except OSError:
            continue
    if not files:
        return 0.0  # 真正无文件 → 0.0(覆盖空目录 + 不存在)
    import hashlib
    hasher = hashlib.sha256()
    for rel in sorted(files):
        hasher.update(rel.encode("utf-8"))
    path_hash = int.from_bytes(hasher.digest()[:8], "big")
    return max_mtime + path_hash * 1e-9


@dataclass
class SkillRuntime:
    """Skill 子系统运行时句柄。

    持有:
    - skills_dirs:Skill 目录根列表(Phase 7+ 多源:[builtin, user])
    - work_dir:工作目录根
    - backend:filesystem backend
    - skills:已扫描的 SkillMetadata 列表
    - 🆕 热更新:enable_hot_reload + _fingerprint + _rescan_if_changed
    """

    skills_dirs: list[Path]
    work_dir: Path
    backend: SmartButlerFilesystemBackend
    skills: list[SkillMetadata]
    enable_hot_reload: bool = True
    _fingerprint: float = field(default=0.0, init=False, repr=False, compare=False)

    @classmethod
    def from_settings(
        cls,
        settings: SkillSettings | None = None,
    ) -> SkillRuntime:
        """从 SkillSettings 构造运行时(Phase 7+ 多源扫描 + 热更新初始化)。

        步骤:
        1. 加载 settings(默认从 env)
        2. 构造 PermissionPolicy(支持多 skills_dir)
        3. 构造 SmartButlerFilesystemBackend
        4. 扫描 [builtin, user] — builtin 权威,user 同名被忽略 + 告警
        5. 初始化 mtime 指纹(给热更新用)
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
        runtime = cls(
            skills_dirs=skills_dirs_for_policy,
            work_dir=work_dir,
            backend=backend,
            skills=skills,
            enable_hot_reload=s.enable_hot_reload,
        )
        # 初始化指纹(给首次重扫判断用)
        runtime._fingerprint = _compute_skills_fingerprint(skills_dirs_for_policy)
        _logger.info(
            "skill_runtime.initialized",
            skills_dirs=[str(r[0]) for r in roots],
            work_dir=str(work_dir),
            skill_count=len(skills),
            skill_names=[m.name for m in skills],
            hot_reload=s.enable_hot_reload,
            fingerprint=runtime._fingerprint,
        )
        return runtime

    # ---------- 导出 ----------

    def build_file_tools(self) -> list[BaseTool]:
        """构造 6 个文件工具(给 ToolRegistry.register_many 用)。"""
        return build_default_skill_tools(self.backend)

    def _rescan_if_changed(self) -> None:
        """🆕 热更新:如果 skills_dirs 树 mtime 变化,重扫。

        借鉴 deepagents 思路:
        - 触发语义 = "数据指纹变化 → 重新加载"(不是 state 字段 None,因我们没 LangGraph state)
        - 调用时机 = 惰性,在 render / find 之前
        - 失败回退 = 保留旧缓存 + warn,不抛
        - 可关闭 = enable_hot_reload=False 时跳过

        重建说明:policy 重建成本高(涉及 PermissionRule 解析),所以本方法只重扫 skills 列表;
        policy 是基于目录路径静态生成,目录不变 → policy 也不变。backend 同理。
        """
        if not self.enable_hot_reload:
            return
        new_fp = _compute_skills_fingerprint(self.skills_dirs)
        if new_fp == self._fingerprint:
            return
        # 指纹变了 → 重扫;复用 from_settings 同款多源拆分规则
        # builtin = 含 "builtin" 路径段(对齐 from_settings 的判断:目录名是 builtin 或路径含 skills/builtin)
        roots: list[tuple[Path, Literal["builtin", "user"]]] = []
        for d in self.skills_dirs:
            # 简化判断:第一个目录(builtin_dir)总是 builtin;其余(若有)是 user
            if d == self.skills_dirs[0]:
                roots.append((d, "builtin"))
            else:
                roots.append((d, "user"))
        try:
            new_skills = scan_skills_dirs(roots)
        except Exception as exc:  # noqa: BLE001
            _logger.warning(
                "skill_runtime.hot_reload_failed",
                error=str(exc),
                old_count=len(self.skills),
                fingerprint=new_fp,
            )
            # 仍更新指纹(避免每次调用都重试),但保留旧 skills
            self._fingerprint = new_fp
            return
        old_names = sorted(s.name for s in self.skills)
        new_names = sorted(s.name for s in new_skills)
        added = sorted(set(new_names) - set(old_names))
        removed = sorted(set(old_names) - set(new_names))
        self.skills = new_skills
        self._fingerprint = new_fp
        _logger.info(
            "skill_runtime.hot_reloaded",
            old_count=len(self.skills),
            new_count=len(new_skills),
            added=added,
            removed=removed,
        )

    def render_prompt_snippet(self) -> str:
        """渲染 skill 列表的 system prompt 片段(给 ButlerPromptBuilder)。

        🆕 热更新:每次调用前先检查 mtime,变化则重扫。
        """
        self._rescan_if_changed()
        return render_skill_list_prompt(self.skills)

    def mark_dirty(self) -> None:
        """🆕 供 tool 调用(2026-10-09 增):标记缓存脏,下次访问必重扫。

        入口在 smartbutler/capabilities/tools/common/skill_admin.py
        的 reload_skills tool。Tool 不直接动 SkillRuntime 状态,
        只调这个公开方法 → thinking 层的封装边界保留。
        """
        self._fingerprint = -1.0

    def find_skill_by_name(self, name: str) -> SkillMetadata | None:
        """按 name 查 skill(LLM 用 read_file 之前先查路径)。

        🆕 热更新:每次调用前先检查 mtime,变化则重扫。
        """
        self._rescan_if_changed()
        for s in self.skills:
            if s.name == name:
                return s
        return None


__all__ = ["SkillRuntime"]
