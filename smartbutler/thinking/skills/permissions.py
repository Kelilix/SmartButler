"""SmartButler 写文件权限系统(基于 deepagents FilesystemPermission)。

参考 2026-10-09 决议:
- 3 类路径 / 3 类模式
  1. Skill 自有数据 (skills/builtin/<name>/**)
     - mode=allow  → 写不打断
  2. 工作目录(workspace/ + workspace/YYYYMMDD/**)
     - mode=allow  → 写不打断
  3. 任何其他路径
     - mode=interrupt → 弹审批框(Phase 5 阶段,interrupt 暂以 deny 模拟
       —— Phase 7 接 LangChain HumanInTheLoop 中间件后真正生效)

不直接依赖 deepagents(middleware.permissions.FilesystemPermission) 字段名,
改成内部 dataclass,后续若升级 LangChain 可平滑替换。

为什么不用 deepagents.middleware.permissions.FilesystemPermission:
- 它是给 FilesystemMiddleware 用的,本类不需要
- 我们要把权限规则嵌进 BaseTool.ainvoke 自己做校验,跟 LangChain 解耦
- dataclass 形式更易单测

注意:字段含义跟 deepagents 一一对应,后续可直接换实现。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path


class PermissionMode(StrEnum):
    """权限规则模式(对齐 deepagents.FilesystemPermission.mode)。"""

    ALLOW = "allow"          # 直接放行
    DENY = "deny"            # 直接拒绝
    INTERRUPT = "interrupt"  # 弹审批框(Phase 5 = 拒绝 + 日志;Phase 7 = 真中断)


class FileOperation(StrEnum):
    """文件操作类型。"""

    READ = "read"   # 覆盖 read_file/ls/glob/grep
    WRITE = "write"  # 覆盖 write_file/edit_file/delete


@dataclass(frozen=True)
class PermissionRule:
    """单条声明式权限规则。

    字段对齐 deepagents.FilesystemPermission,但用 dataclass 表示,
    不引入 deepagents 依赖以便解耦。
    """

    operations: tuple[FileOperation, ...]
    paths: tuple[str, ...]
    mode: PermissionMode

    def matches(self, operation: FileOperation, abs_path: Path) -> bool:
        """判断本规则是否作用于给定 (operation, abs_path)。

        路径匹配规则(对齐 deepagents 行为):
        - paths 是 glob 模式列表,任一匹配即算命中
        - 支持 `**` 递归、`{a,b}` 替代(我们 Phase 5 简化:只支持 `**`)
        - 匹配是相对 work_dir 的虚拟路径
        """
        if operation not in self.operations:
            return False
        # 把 abs_path 视作相对 work_dir 的虚拟路径
        for pattern in self.paths:
            if _glob_match(pattern, abs_path):
                return True
        return False


@dataclass(frozen=True)
class PermissionPolicy:
    """一组权限规则 + 评估函数。

    评估语义(对齐 deepagents first-match-wins):
    - 规则按声明顺序评估
    - 命中即返回该规则的 mode
    - 全部不命中 → 默认 ALLOW(保持 LLM 调用顺畅,
      真正的安全靠 root_dir 沙箱兜底)
    """

    rules: tuple[PermissionRule, ...]
    default: PermissionMode = PermissionMode.ALLOW

    def evaluate(self, operation: FileOperation, abs_path: Path) -> PermissionMode:
        """评估 (operation, path) 应该走哪个模式。"""
        for rule in self.rules:
            if rule.matches(operation, abs_path):
                return rule.mode
        return self.default


# ----------------- 内部工具 -----------------


def _glob_match(pattern: str, candidate: Path) -> bool:
    """简化版 glob 匹配 —— 支持 `**` 递归,不支持 `{}` 替代。

    Phase 5 简化:
    - pattern 是相对于 work_dir 的虚拟路径(以 `/` 开头)
    - candidate 是 Path
    - 把 candidate 视作 POSIX 字符串与 PurePosixPath 匹配

    例子:
        - "/workspace/**"    ↔ "/workspace/20261009/foo.txt"  ✓
        - "/skills/**"       ↔ "/skills/builtin/pdf/data/x"   ✓
        - "/skills"          ↔ "/skills/builtin/pdf/SKILL.md" ✗(要求精确段)
    """
    # 把 candidate 转 POSIX 形式
    candidate_posix = "/" + str(candidate.as_posix()).lstrip("/")

    # 把 pattern 标准化
    if not pattern.startswith("/"):
        pattern = "/" + pattern

    # 简化匹配:用 fnmatch
    import fnmatch

    # PurePosixPath.match 不直接支持 **,转 fnmatch
    # 把 ** 转换成 fnmatch 风格的递归:
    # /workspace/**  →  /workspace/*  (只匹配一段)
    # 真正的 ** 语义需要更复杂处理,Phase 5 简化:把 ** 当作 *
    simplified = pattern.replace("**", "*")
    return fnmatch.fnmatch(candidate_posix, simplified) or fnmatch.fnmatch(
        candidate_posix + "/", simplified
    )


# ----------------- 构造器 -----------------


def build_default_policy(
    *,
    work_dir: Path,
    skills_dirs: list[Path],
) -> PermissionPolicy:
    """构造 SmartButler 默认权限策略。

    规则(顺序敏感 — first-match-wins):
    1. 每个 skills_dir 及其子目录 → ALLOW(支持多源:builtin + user)
    2. work_dir 及其子目录 → ALLOW
    3. 其他所有路径 → INTERRUPT(默认)

    Args:
        work_dir: 用户工作目录根
        skills_dirs: Skill 存放根列表(可含 builtin + user 多源)

    Returns:
        不可变 PermissionPolicy
    """
    # 全部转绝对路径并 POSIX 化
    work_root = "/" + str(work_dir.expanduser().resolve().as_posix()).lstrip("/")

    rules: list[PermissionRule] = []
    # 1. 多个 skills_dir 各自一条 ALLOW 规则
    for skills_dir in skills_dirs:
        skills_root = "/" + str(
            skills_dir.expanduser().resolve().as_posix()
        ).lstrip("/")
        rules.append(
            PermissionRule(
                operations=(FileOperation.READ, FileOperation.WRITE),
                paths=(f"{skills_root}/**",),
                mode=PermissionMode.ALLOW,
            )
        )
    # 2. work_dir 一条 ALLOW 规则
    rules.append(
        PermissionRule(
            operations=(FileOperation.READ, FileOperation.WRITE),
            paths=(f"{work_root}/**",),
            mode=PermissionMode.ALLOW,
        )
    )
    # 3. 其他路径 INTERRUPT(用 default 而不是显式规则,避免写很长的 / 路径)
    return PermissionPolicy(rules=tuple(rules), default=PermissionMode.INTERRUPT)


def resolve_today_workspace(work_dir: Path) -> Path:
    """按日期创建并返回工作子目录(workspace/YYYYMMDD/)。

    Args:
        work_dir: workspace 根

    Returns:
        当天日期子目录(不存在则创建)
    """
    today = datetime.now().strftime("%Y%m%d")
    today_dir = work_dir / today
    today_dir.mkdir(parents=True, exist_ok=True)
    return today_dir


__all__ = [
    "FileOperation",
    "PermissionMode",
    "PermissionRule",
    "PermissionPolicy",
    "build_default_policy",
    "resolve_today_workspace",
]
