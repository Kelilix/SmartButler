"""ButlerPromptBuilder 单测(Phase 6.2 P0 memory_block 新增)。"""

from __future__ import annotations

from smartbutler.thinking.prompt.builder import ButlerPromptBuilder


def test_build_includes_memory_block_when_provided() -> None:
    """非空 memory_block 拼到 system prompt。"""
    prompt = ButlerPromptBuilder().build(
        memory_block="### 相关历史记忆\n- 用户喜欢咖啡",
    )
    assert "## 相关历史记忆" in prompt
    assert "用户喜欢咖啡" in prompt


def test_build_empty_memory_block_shows_placeholder() -> None:
    """空 memory_block 拼"(无相关历史)"占位。"""
    prompt = ButlerPromptBuilder().build(memory_block="")
    assert "## 相关历史记忆" in prompt
    assert "无相关历史" in prompt


def test_build_default_memory_block_empty() -> None:
    """不传 memory_block = 默认空 → 占位文本。"""
    prompt = ButlerPromptBuilder().build()
    assert "## 相关历史记忆" in prompt
    assert "无相关历史" in prompt


def test_memory_block_stripped() -> None:
    """memory_block 前后空白被 strip。"""
    prompt = ButlerPromptBuilder().build(
        memory_block="  \n### 记忆\n- 内容\n  ",
    )
    # 应该 strip 后,不会留前导空行
    assert "  \n### 记忆" not in prompt
    assert "### 记忆" in prompt
