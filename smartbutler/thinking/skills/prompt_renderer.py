"""把 SkillMetadata 列表渲染成 system prompt 片段。

对接 ButlerPromptBuilder.skill_prompt_snippets 字段。
"""
from __future__ import annotations

from smartbutler.thinking.skills.scanner import SkillMetadata

# 跟 ButlerPromptBuilder._SKILL_SNIPPETS_HEADER 保持一致风格
_SKILL_LIST_HEADER = """\
## 已加载 Skills(按需遵循)

以下 Skills 已加载,内容会指导你处理特定类型任务时该怎么做。**不是**所有任务都要用,看场景。

**使用流程**:
1. 看用户请求是否匹配某个 skill 的 description
2. 匹配 → 用 `read_file` 读该 skill 的 SKILL.md(传 limit=1000)
3. 按 SKILL.md 里的步骤执行
"""


def render_skill_list_prompt(skills: list[SkillMetadata]) -> str:
    """渲染 skill 列表(给 LLM 路由用)——只展示 name + description + path。

    不展示 body(body 在第二步 read_file 时才读)。
    """
    if not skills:
        return ""
    lines = [_SKILL_LIST_HEADER, ""]
    for s in skills:
        lines.append(s.to_prompt_summary())
        if s.allowed_tools:
            tools_text = ", ".join(s.allowed_tools)
            lines.append(f"  - 配套工具: {tools_text}")
    return "\n".join(lines)


def render_skill_body_prompt(skill: SkillMetadata) -> str:
    """渲染单条 skill 完整内容(L2 加载用,read_file 之后塞到对话里)。"""
    return f"# Skill: {skill.name}\n\n{skill.body}"


__all__ = ["render_skill_list_prompt", "render_skill_body_prompt"]
