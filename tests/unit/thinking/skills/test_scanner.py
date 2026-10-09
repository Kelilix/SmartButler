"""SKILL.md 扫描器 + 渲染器 测试。"""
from __future__ import annotations

from pathlib import Path

import pytest

from smartbutler.thinking.skills import (
    SkillMetadata,
    parse_skill_md,
    render_skill_list_prompt,
    scan_skills_dir,
)
from smartbutler.thinking.skills.scanner import SkillScanError

# ----------------- parse_skill_md -----------------


def _write_skill(skills_dir: Path, name: str, body: str) -> Path:
    d = skills_dir / name
    d.mkdir(parents=True, exist_ok=True)
    p = d / "SKILL.md"
    p.write_text(body, encoding="utf-8")
    return p


class TestParseSkillMd:
    def test_valid(self, tmp_path: Path) -> None:
        body = """---
name: pdf-summary
description: Summarize a PDF into bullet points
license: MIT
---

# PDF Summary

## Steps
1. Read
2. Summarize
"""
        p = _write_skill(tmp_path, "pdf-summary", body)
        meta = parse_skill_md(p)
        assert meta.name == "pdf-summary"
        assert "PDF" in meta.description
        assert meta.license == "MIT"
        assert "PDF Summary" in meta.body
        assert "## Steps" in meta.body

    def test_missing_frontmatter(self, tmp_path: Path) -> None:
        p = _write_skill(tmp_path, "bad", "# No frontmatter\n")
        with pytest.raises(SkillScanError, match="缺少 frontmatter"):
            parse_skill_md(p)

    def test_missing_name(self, tmp_path: Path) -> None:
        body = """---
description: only description
---

body
"""
        p = _write_skill(tmp_path, "x", body)
        with pytest.raises(SkillScanError, match="name 或 description"):
            parse_skill_md(p)

    def test_name_too_long(self, tmp_path: Path) -> None:
        long_name = "a" * 100
        body = f"""---
name: {long_name}
description: ok
---

x
"""
        p = _write_skill(tmp_path, "x", body)
        with pytest.raises(SkillScanError, match="name 超过"):
            parse_skill_md(p)

    def test_description_too_long(self, tmp_path: Path) -> None:
        body = f"""---
name: ok
description: {'a' * 2000}
---

x
"""
        p = _write_skill(tmp_path, "ok", body)
        with pytest.raises(SkillScanError, match="description 超过"):
            parse_skill_md(p)

    def test_allowed_tools_string(self, tmp_path: Path) -> None:
        body = """---
name: ok
description: ok
allowed-tools: tool1, tool2, tool3
---

x
"""
        p = _write_skill(tmp_path, "ok", body)
        meta = parse_skill_md(p)
        assert meta.allowed_tools == ["tool1", "tool2", "tool3"]

    def test_allowed_tools_list(self, tmp_path: Path) -> None:
        body = """---
name: ok
description: ok
allowed-tools:
  - tool1
  - tool2
---

x
"""
        p = _write_skill(tmp_path, "ok", body)
        meta = parse_skill_md(p)
        assert meta.allowed_tools == ["tool1", "tool2"]


# ----------------- scan_skills_dir -----------------


class TestScanSkillsDir:
    def test_scans_subdirs(self, tmp_path: Path) -> None:
        _write_skill(
            tmp_path,
            "skill-a",
            "---\nname: skill-a\ndescription: A\n---\nbody\n",
        )
        _write_skill(
            tmp_path,
            "skill-b",
            "---\nname: skill-b\ndescription: B\n---\nbody\n",
        )
        out = scan_skills_dir(tmp_path)
        names = {s.name for s in out}
        assert names == {"skill-a", "skill-b"}

    def test_skip_no_skill_md(self, tmp_path: Path) -> None:
        (tmp_path / "no-md").mkdir()
        out = scan_skills_dir(tmp_path)
        assert out == []

    def test_skip_invalid(self, tmp_path: Path) -> None:
        # bad 目录:frontmatter 缺失,被 warn 后 skip
        _write_skill(tmp_path, "bad", "no frontmatter")
        # good 目录:正常
        _write_skill(
            tmp_path,
            "good",
            "---\nname: good\ndescription: ok\n---\nbody\n",
        )
        out = scan_skills_dir(tmp_path)
        names = {s.name for s in out}
        assert names == {"good"}

    def test_empty_dir(self, tmp_path: Path) -> None:
        assert scan_skills_dir(tmp_path) == []

    def test_nonexistent_dir(self, tmp_path: Path) -> None:
        assert scan_skills_dir(tmp_path / "nope") == []


# ----------------- prompt_renderer -----------------


class TestPromptRenderer:
    def test_empty(self) -> None:
        assert render_skill_list_prompt([]) == ""

    def test_one_skill(self, tmp_path: Path) -> None:
        # 用 tmp_path 保证跨平台路径
        m = SkillMetadata(
            name="pdf",
            description="PDF summary",
            path=tmp_path / "pdf" / "SKILL.md",
            body="body",
        )
        out = render_skill_list_prompt([m])
        assert "pdf" in out
        assert "PDF summary" in out
        # POSIX 形式路径(不论操作系统)
        posix_path = (tmp_path / "pdf" / "SKILL.md").as_posix()
        assert posix_path in out

    def test_with_allowed_tools(self) -> None:
        m = SkillMetadata(
            name="x",
            description="x skill",
            path=Path("/x"),
            body="",
            allowed_tools=["t1", "t2"],
        )
        out = render_skill_list_prompt([m])
        assert "t1" in out
        assert "t2" in out
