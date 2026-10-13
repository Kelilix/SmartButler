"""Phase 6.6: Personality loader 单元测试。

覆盖:
- 文件不存在 → 默认 Personality
- 正常 frontmatter 解析
- 文件只有部分字段 → 走默认补全
- 文件格式烂(无 frontmatter / YAML 解析失败 / 数据校验失败)→ 默认 Personality
- 保存 + 加载往返
- 默认候选路径
"""
from __future__ import annotations

from pathlib import Path

import pytest

from smartbutler.emotion.personality.loader import (
    DEFAULT_PERSONALITY_FILENAME,
    _split_frontmatter,
    load_personality,
    save_personality,
)
from smartbutler.emotion.personality.traits import (
    DEFAULT_NAME,
    DEFAULT_TRAITS,
    TRAIT_HUMOR,
    TRAIT_VERBOSITY,
    Personality,
)


class TestSplitFrontmatter:
    def test_no_frontmatter(self) -> None:
        data, body = _split_frontmatter("hello world")
        assert data == {}
        assert body == "hello world"

    def test_empty_file(self) -> None:
        data, body = _split_frontmatter("")
        assert data == {}
        assert body == ""

    def test_only_opening_marker(self) -> None:
        """只有 ``---`` 没有收尾 → 当成没 frontmatter。"""
        data, body = _split_frontmatter("---\nfoo: bar\n")
        assert data == {}
        # body 会经过 strip(),去掉首尾空白
        assert "foo: bar" in body
        assert body == "---\nfoo: bar"  # 末尾 \n 被 strip 掉了

    def test_valid_frontmatter(self) -> None:
        raw = "---\nname: X\ntraits:\n  humor: 0.7\n---\n# body\nfree text"
        data, body = _split_frontmatter(raw)
        assert data == {"name": "X", "traits": {"humor": 0.7}}
        assert "# body" in body

    def test_malformed_yaml_returns_empty(self) -> None:
        raw = "---\n[unclosed\n---"
        data, body = _split_frontmatter(raw)
        assert data == {}


class TestLoadPersonality:
    def test_nonexistent_returns_default(self, tmp_path: Path) -> None:
        p = load_personality(tmp_path / "nope.md")
        assert isinstance(p, Personality)
        assert p.name == DEFAULT_NAME  # DEFAULT_NAME
        assert p.updated_by == "default"

    def test_load_existing_file(self, tmp_path: Path) -> None:
        path = tmp_path / "personality.md"
        path.write_text(
            "---\n"
            "name: 严管家\n"
            "traits:\n"
            "  humor: 0.1\n"
            "  verbosity: 0.9\n"
            "guidelines:\n"
            "  - 严谨回答\n"
            "  - 多用专业术语\n"
            "updated_by: user_explicit\n"
            "---\n"
            "# 这是注释,会被忽略\n",
            encoding="utf-8",
        )
        p = load_personality(path)
        assert p.name == "严管家"
        assert p.get_trait_value(TRAIT_HUMOR) == pytest.approx(0.1)
        assert p.get_trait_value(TRAIT_VERBOSITY) == pytest.approx(0.9)
        assert p.guidelines == ["严谨回答", "多用专业术语"]
        assert p.updated_by == "user_explicit"

    def test_partial_fields_use_defaults(self, tmp_path: Path) -> None:
        path = tmp_path / "personality.md"
        path.write_text(
            "---\nname: 部分覆盖\n---\n",
            encoding="utf-8",
        )
        p = load_personality(path)
        assert p.name == "部分覆盖"
        # 其他字段走默认
        assert len(p.traits) == 5
        assert len(p.guidelines) >= 1

    def test_empty_file_returns_default(self, tmp_path: Path) -> None:
        path = tmp_path / "personality.md"
        path.write_text("", encoding="utf-8")
        p = load_personality(path)
        assert p.name == DEFAULT_NAME
        assert p.updated_by == "default"

    def test_invalid_trait_value_returns_default(self, tmp_path: Path) -> None:
        """越界值 → Pydantic 校验失败 → 兜底默认。"""
        path = tmp_path / "personality.md"
        path.write_text(
            "---\ntraits:\n  humor: 2.0\n---\n",
            encoding="utf-8",
        )
        p = load_personality(path)
        # 不应崩溃,humor 走默认
        assert p.get_trait_value(TRAIT_HUMOR) == DEFAULT_TRAITS[TRAIT_HUMOR]

    def test_invalid_yaml_returns_default(self, tmp_path: Path) -> None:
        path = tmp_path / "personality.md"
        path.write_text(
            "---\n[broken: yaml: here\n---\n",
            encoding="utf-8",
        )
        p = load_personality(path)
        assert p.name == DEFAULT_NAME

    def test_default_candidates_finds_cwd(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``path=None`` → 找 ``<cwd>/personality.md``,找到即用。"""
        target = tmp_path / DEFAULT_PERSONALITY_FILENAME
        target.write_text(
            "---\nname: cwd版\n---\n",
            encoding="utf-8",
        )
        monkeypatch.chdir(tmp_path)
        p = load_personality(None)
        assert p.name == "cwd版"


class TestSavePersonality:
    def test_save_creates_file(self, tmp_path: Path) -> None:
        path = tmp_path / "out" / "personality.md"
        p = Personality(name="保存测试")
        p.adjust_trait(TRAIT_HUMOR, new_value=0.7, source="user_explicit")
        result = save_personality(p, path)
        assert result == path
        assert path.is_file()

    def test_save_then_load_roundtrip(self, tmp_path: Path) -> None:
        path = tmp_path / "personality.md"
        original = Personality(name="往返")
        original.adjust_trait(TRAIT_HUMOR, new_value=0.7, source="user_explicit")
        original.replace_guidelines(["A", "B"], source="user_explicit")
        save_personality(original, path)
        loaded = load_personality(path)
        assert loaded.name == "往返"
        assert loaded.get_trait_value(TRAIT_HUMOR) == pytest.approx(0.7)
        assert loaded.guidelines == ["A", "B"]

    def test_save_creates_parent_dir(self, tmp_path: Path) -> None:
        path = tmp_path / "nested" / "deep" / "personality.md"
        save_personality(Personality(), path)
        assert path.is_file()

    def test_saved_file_has_frontmatter_and_body(self, tmp_path: Path) -> None:
        path = tmp_path / "personality.md"
        save_personality(Personality(name="X"), path)
        content = path.read_text(encoding="utf-8")
        assert content.startswith("---\n")
        assert "name: X" in content
        assert "tricks" not in content  # body 关键词,这里只断言能写
