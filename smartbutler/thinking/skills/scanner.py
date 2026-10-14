"""SKILL.md 扫描器 — 扫描工作区里的 Skill 目录,解析 frontmatter。

Frontmatter 格式(Anthropic Agent Skills 规范):

```markdown
---
name: my-skill           # 必填,1-64 字符,小写字母数字+连字符
description: ...         # 必填,1-1024 字符
license: MIT             # 可选
compatibility: ...       # 可选,≤500 字符
metadata: {key: value}   # 可选,dict
allowed-tools: tool1, tool2  # 可选,逗号或空格分隔
---

# My Skill
...markdown body...
```

Phase 5 简化:
- 只支持 YAML frontmatter(用 pyyaml.safe_load 解析)
- body 不解析(只是保留原文,Phase 6+ 可能拆成结构化步骤)
- 不递归扫描多级目录:每个 skill 一个子目录,子目录下有 SKILL.md

Phase 7+ 多源:
- scan_skills_dirs(roots) 支持扫多个根
- 冲突策略:builtin 赢(权威),user 源同名被忽略 + 告警
- SkillMetadata.source 字段标注来源(builtin/user)
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import yaml

# 简单 frontmatter 解析正则(避免 yaml 不在 / 没装的情况;Phase 5 仍依赖 yaml)
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.DOTALL)

# frontmatter 字段约束(对齐 Anthropic Agent Skills spec)
MAX_NAME_LENGTH = 64
MAX_DESCRIPTION_LENGTH = 1024
MAX_COMPATIBILITY_LENGTH = 500


@dataclass
class SkillMetadata:
    """单个 Skill 的元数据 + body。

    与 deepagents.middleware.skills.SkillMetadata 对齐,
    但额外保留 body 原文以便直接展示给 LLM。
    """

    name: str
    description: str
    path: Path  # SKILL.md 的绝对路径
    body: str  # frontmatter 之后的内容
    source: Literal["builtin", "user"] = "builtin"  # Phase 7+ 多源加载用
    license: str | None = None
    compatibility: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)
    allowed_tools: list[str] = field(default_factory=list)

    @property
    def directory(self) -> Path:
        return self.path.parent

    def to_prompt_summary(self) -> str:
        """生成 system prompt 里展示的 skill 摘要(LLM 路由用)。

        路径强制转 POSIX 形式(LLM 看到 /,不论操作系统)。
        """
        posix_path = self.path.as_posix() if hasattr(self.path, "as_posix") else str(self.path)
        return f"- **{self.name}**: {self.description} (path: `{posix_path}`)"


class SkillScanError(Exception):
    """扫描 skill 失败(含校验错误)。"""


def parse_skill_md(
    file_path: Path,
    *,
    source: Literal["builtin", "user"] = "builtin",
) -> SkillMetadata:
    """解析单个 SKILL.md。

    校验:
    - 必须有 frontmatter 且能 yaml 解析成 dict
    - 必填 name / description
    - name 满足 1-64 字符
    - description 满足 1-1024 字符
    """
    if not file_path.exists():
        msg = f"SKILL.md 不存在: {file_path}"
        raise FileNotFoundError(msg)
    text = file_path.read_text(encoding="utf-8")
    m = _FRONTMATTER_RE.match(text)
    if not m:
        msg = f"SKILL.md {file_path} 缺少 frontmatter(以 --- 开头)"
        raise SkillScanError(msg)
    fm_str = m.group(1)
    body = text[m.end() :]
    try:
        fm = yaml.safe_load(fm_str)
    except yaml.YAMLError as exc:
        msg = f"SKILL.md {file_path} frontmatter 不是合法 YAML: {exc}"
        raise SkillScanError(msg) from exc
    if not isinstance(fm, dict):
        msg = f"SKILL.md {file_path} frontmatter 不是 dict"
        raise SkillScanError(msg)

    name = str(fm.get("name", "")).strip()
    description = str(fm.get("description", "")).strip()
    if not name or not description:
        msg = f"SKILL.md {file_path} 缺少 name 或 description"
        raise SkillScanError(msg)
    if len(name) > MAX_NAME_LENGTH:
        msg = f"SKILL.md {file_path} name 超过 {MAX_NAME_LENGTH} 字符"
        raise SkillScanError(msg)
    if len(description) > MAX_DESCRIPTION_LENGTH:
        msg = f"SKILL.md {file_path} description 超过 {MAX_DESCRIPTION_LENGTH} 字符"
        raise SkillScanError(msg)
    compatibility = fm.get("compatibility")
    if compatibility is not None and len(str(compatibility)) > MAX_COMPATIBILITY_LENGTH:
        msg = f"SKILL.md {file_path} compatibility 超过 {MAX_COMPATIBILITY_LENGTH} 字符"
        raise SkillScanError(msg)

    # 解析 metadata + allowed_tools
    extra_meta = fm.get("metadata") or {}
    if not isinstance(extra_meta, dict):
        extra_meta = {}
    allowed_raw = fm.get("allowed-tools") or fm.get("allowed_tools") or []
    if isinstance(allowed_raw, str):
        allowed = [t.strip() for t in re.split(r"[\s,]+", allowed_raw) if t.strip()]
    elif isinstance(allowed_raw, list):
        allowed = [str(t).strip() for t in allowed_raw if t]
    else:
        allowed = []

    return SkillMetadata(
        name=name,
        description=description,
        path=file_path,
        body=body.strip(),
        source=source,
        license=str(fm["license"]) if fm.get("license") is not None else None,
        compatibility=str(compatibility) if compatibility is not None else None,
        metadata={str(k): str(v) for k, v in extra_meta.items()},
        allowed_tools=allowed,
    )


def scan_skills_dir(
    skills_dir: Path,
    *,
    source: Literal["builtin", "user"] = "builtin",
) -> list[SkillMetadata]:
    """扫描 skills_dir 下所有子目录,每个子目录一个 skill。

    跳过:
    - 没有 SKILL.md 的子目录
    - 解析失败的 SKILL.md(警告 + skip,不抛错)
    - 隐藏目录(.git / .vscode / __pycache__ 等)
    """
    if not skills_dir.exists():
        return []
    out: list[SkillMetadata] = []
    for child in sorted(skills_dir.iterdir()):
        if not child.is_dir():
            continue
        if child.name.startswith("."):
            continue
        if child.name.startswith("__"):
            continue
        skill_md = child / "SKILL.md"
        if not skill_md.exists():
            continue
        try:
            meta = parse_skill_md(skill_md, source=source)
            out.append(meta)
        except (SkillScanError, FileNotFoundError) as exc:
            # 警告但 skip,不阻塞其他 skill 加载
            import structlog

            structlog.get_logger(__name__).warning(
                "skill_scan.skipped",
                path=str(skill_md),
                error=str(exc),
            )
            continue
    return out


def scan_skills_dirs(
    roots: list[tuple[Path, Literal["builtin", "user"]]],
) -> list[SkillMetadata]:
    """扫描多个 skill 根目录,按顺序合并,builtin 优先。

    冲突策略(2026-10-09 决议):
    - builtin 胜出(权威性)
    - user 源同名 skill 被忽略 + 告警(不抛错,不阻塞)
    - 不同名则两个都保留

    Args:
        roots: [(目录路径, source)] 列表,顺序敏感——前面的 source 赢同名。

    Returns:
        合并后的 SkillMetadata 列表(顺序:builtin 先 / user 后)。
    """
    import structlog

    log = structlog.get_logger(__name__)
    by_name: dict[str, SkillMetadata] = {}
    for root, source in roots:
        for meta in scan_skills_dir(root, source=source):
            if meta.name in by_name:
                winner = by_name[meta.name]
                if winner.source == "builtin" and source == "user":
                    log.warning(
                        "skill_scan.user_shadowed",
                        skill=meta.name,
                        winner_source=winner.source,
                        winner_path=str(winner.path),
                        shadowed_path=str(meta.path),
                        reason="builtin 权威性优先",
                    )
                    continue
                # builtin 撞 builtin 也 warn(配置异常)
                log.warning(
                    "skill_scan.duplicate_name",
                    skill=meta.name,
                    winner_source=winner.source,
                    winner_path=str(winner.path),
                    loser_path=str(meta.path),
                )
                continue
            by_name[meta.name] = meta
    # 排序:builtin 先,user 后,都按名字升序
    return sorted(
        by_name.values(),
        key=lambda m: (0 if m.source == "builtin" else 1, m.name),
    )


__all__ = [
    "SkillMetadata",
    "SkillScanError",
    "parse_skill_md",
    "scan_skills_dir",
    "scan_skills_dirs",
]
