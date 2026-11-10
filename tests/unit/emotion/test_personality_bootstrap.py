"""🆕 Personality 装配入口测试(2026-11-09)。

回归的 bug:生产入口脚本构造 ``ButlerOrchestrator`` 时**没传 personality**
→ ``_personality is None`` → ``personality_block`` 为空 →
``ButlerPromptBuilder`` 回落内置的通用 ``_BUTLER_PERSONA``
→ 用户配置的"英国贵族管家"人设**完全没生效**。

这些测试锁死"人设必须真的进 system prompt"这条不变量。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from smartbutler.emotion.personality.bootstrap import (
    attach_personality,
    build_orchestrator,
    load_personality_safe,
)
from smartbutler.emotion.personality.traits import Personality

_ROOT = Path(__file__).resolve().parents[3]


# ---------------------------------------------------------------------------
# 1. load_personality_safe
# ---------------------------------------------------------------------------


def test_load_safe_reads_personality_md(tmp_path: Path) -> None:
    """指定路径 → 读到该文件的 identity/name。"""
    path = tmp_path / "personality.md"
    path.write_text(
        "---\n"
        "name: 测试管家\n"
        "identity: 你是测试管家,来自虚构的某个郡。\n"
        "traits:\n"
        "  humor: 0.9\n"
        "---\n"
        "body ignored\n",
        encoding="utf-8",
    )
    p = load_personality_safe(str(path))
    assert p.name == "测试管家"
    assert "虚构的某个郡" in p.identity
    assert p.get_trait_value("humor") == pytest.approx(0.9)


def test_load_safe_missing_file_returns_default(tmp_path: Path) -> None:
    """文件不存在 → 降级到默认 Personality(不抛异常)。"""
    p = load_personality_safe(str(tmp_path / "nope.md"))
    assert isinstance(p, Personality)
    # 默认也是杰贝兹人设(不是空 identity)
    assert p.identity.strip()
    assert p.name


def test_load_safe_broken_file_returns_default(tmp_path: Path) -> None:
    """YAML 语法坏掉 → 降级到默认,不让管家崩。"""
    path = tmp_path / "personality.md"
    path.write_text(
        "---\n"
        "name: [unclosed\n"
        "  bad: : :\n"
        "---\n",
        encoding="utf-8",
    )
    p = load_personality_safe(str(path))
    assert isinstance(p, Personality)
    assert p.identity.strip()


def test_load_safe_validates_trait_range(tmp_path: Path) -> None:
    """trait 越界 → 校验失败降级,不会让非法值进 prompt。"""
    path = tmp_path / "personality.md"
    path.write_text(
        "---\n"
        "name: 越界管家\n"
        "traits:\n"
        "  humor: 5.0\n"
        "---\n",
        encoding="utf-8",
    )
    p = load_personality_safe(str(path))
    assert isinstance(p, Personality)
    # 降级后所有 trait 都在 [0, 1]
    for key in p.list_trait_keys():
        v = p.get_trait_value(key)
        assert v is not None
        assert 0.0 <= v <= 1.0


# ---------------------------------------------------------------------------
# 2. attach_personality
# ---------------------------------------------------------------------------


class _FakeOrchestrator:
    """最小 orchestrator 替身(只留 attach 用到的字段)。"""

    def __init__(self) -> None:
        self._personality: Any = None
        self._current_personality_block: str = ""


def test_attach_sets_personality_and_clears_cache(tmp_path: Path) -> None:
    """attach → 写入 _personality + 清掉渲染缓存。"""
    path = tmp_path / "personality.md"
    path.write_text(
        "---\nname: 甲管家\nidentity: 甲身份\n---\n", encoding="utf-8"
    )
    orch = _FakeOrchestrator()
    orch._current_personality_block = "STALE"

    p = attach_personality(orch, path=str(path))
    assert p is not None
    assert orch._personality is p
    assert orch._current_personality_block == ""


def test_attach_none_orchestrator_returns_none() -> None:
    """orchestrator 是 None → 返 None,不崩。"""
    assert attach_personality(None) is None


def test_attach_failure_keeps_orchestrator_usable(tmp_path: Path) -> None:
    """attach 抛异常 → 记 warning,orchestrator 保持原状。"""

    class _Broken:
        class _P:  # noqa: N801
            @property
            def _personality(self):  # noqa: ANN202
                raise RuntimeError("boom")

        _personality = None

        def __setattr__(self, name: str, value: Any) -> None:
            if name == "_personality":
                raise RuntimeError("boom")
            object.__setattr__(self, name, value)

    orch = _Broken()
    assert attach_personality(orch, path=str(tmp_path / "x.md")) is None


# ---------------------------------------------------------------------------
# 3. build_orchestrator(回归测试的核心)
# ---------------------------------------------------------------------------


def test_build_orchestrator_injects_personality(tmp_path: Path) -> None:
    """build_orchestrator → orchestrator 带上 personality。"""
    from unittest.mock import MagicMock

    path = tmp_path / "personality.md"
    path.write_text(
        "---\nname: 乙管家\nidentity: 乙身份\n---\n", encoding="utf-8"
    )
    orch = build_orchestrator(
        llm=MagicMock(),
        llm_settings=MagicMock(),
        personality_path=str(path),
    )
    assert orch._personality is not None
    assert orch._personality.name == "乙管家"


def test_personality_block_reaches_system_prompt(tmp_path: Path) -> None:
    """🔴 回归:人设必须真的进 system prompt,且覆盖内置默认人设。

    这正是线上"中世纪英国贵族管家没生效"的 bug —— orchestrator 拿到了
    personality,但没传进 prompt builder。
    """
    from unittest.mock import MagicMock

    from smartbutler.thinking.prompt.builder import ButlerPromptBuilder

    path = tmp_path / "personality.md"
    path.write_text(
        "---\n"
        "name: 丙管家\n"
        "identity: 丙身份,英国贵族管家。\n"
        "---\n",
        encoding="utf-8",
    )
    orch = build_orchestrator(
        llm=MagicMock(),
        llm_settings=MagicMock(),
        personality_path=str(path),
    )

    block = orch._build_personality_block_sync()
    assert block.strip(), "personality_block 不该是空串"
    assert "丙身份" in block

    prompt = ButlerPromptBuilder().build(
        tool_specs=[],
        memory_block="",
        personality_block=block,
        current_user_id="u1",
    )
    assert "丙身份" in prompt
    # 内置通用人设必须被替换掉
    assert "你是 SmartButler 管家" not in prompt


def test_orchestrator_without_personality_uses_builtin_persona() -> None:
    """对照组:没注入 personality → 走内置默认人设(老行为不变)。"""
    from unittest.mock import MagicMock

    from smartbutler.thinking.loop.orchestrator import ButlerOrchestrator
    from smartbutler.thinking.prompt.builder import ButlerPromptBuilder

    orch = ButlerOrchestrator(llm=MagicMock(), llm_settings=MagicMock())
    assert orch._personality is None

    block = orch._build_personality_block_sync()
    assert block == ""

    prompt = ButlerPromptBuilder().build(
        tool_specs=[],
        memory_block="",
        personality_block=block,
    )
    assert "你是 SmartButler 管家" in prompt


# ---------------------------------------------------------------------------
# 4. 仓库自带的 personality.md(真实用户配置)
# ---------------------------------------------------------------------------


def test_repo_personality_md_is_jeeves() -> None:
    """仓库根的 personality.md 就是杰贝兹人设(用户的中世纪英国贵族管家)。"""
    p = load_personality_safe(str(_ROOT / "personality.md"))
    assert p.name == "杰贝兹"
    assert "杰贝兹" in p.identity
    assert "管家学院" in p.identity
