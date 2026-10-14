"""ButlerPromptBuilder 单元测试。"""
from __future__ import annotations

from langchain_core.tools import StructuredTool
from pydantic import BaseModel

from smartbutler.thinking.prompt.builder import ButlerPromptBuilder


class _NoArgs(BaseModel):
    pass


def _fake_tool(name: str, desc: str) -> StructuredTool:
    return StructuredTool.from_function(
        coroutine=lambda: f"called {name}",
        name=name,
        description=desc,
        args_schema=_NoArgs,
    )


class TestButlerPromptBuilder:
    def test_persona_is_present(self) -> None:
        prompt = ButlerPromptBuilder().build()
        assert "SmartButler" in prompt
        assert "管家" in prompt

    def test_includes_tools(self) -> None:
        tools = [_fake_tool("get_time", "查询当前时间"), _fake_tool("add", "加法")]
        prompt = ButlerPromptBuilder().build(tool_specs=tools)
        assert "get_time" in prompt
        assert "add" in prompt
        assert "查询当前时间" in prompt
        assert "加法" in prompt

    def test_includes_skills(self) -> None:
        prompt = ButlerPromptBuilder().build(
            skill_prompt_snippets=["# pdf-summary\n1. 第一步\n2. 第二步"]
        )
        assert "pdf-summary" in prompt
        assert "第一步" in prompt
        assert "已加载 Skills" in prompt

    def test_skips_empty_skill_snippets(self) -> None:
        prompt = ButlerPromptBuilder().build(
            skill_prompt_snippets=["", "  ", "real content"]
        )
        # 空的应被跳过,只剩 real content
        assert "real content" in prompt

    def test_no_skills_no_section(self) -> None:
        prompt = ButlerPromptBuilder().build()
        assert "已加载 Skills" not in prompt

    def test_truncates_long_descriptions(self) -> None:
        long_desc = "x" * 1000
        tools = [_fake_tool("noop", long_desc)]
        prompt = ButlerPromptBuilder().build(tool_specs=tools)
        # 截断到 237 x + "..." = 240 字符,原始 1000 x 不应全在
        assert "x" * 237 in prompt  # 截断后保留部分
        assert "x" * 1000 not in prompt  # 完整 1000 字符不应存在
        assert "..." in prompt  # 截断标识
        # 整体 desc 在 prompt 里的长度 < 240 + 工具行装饰 < 300
        noop_line = next(ln for ln in prompt.splitlines() if "**noop**" in ln)
        assert len(noop_line) < 300
