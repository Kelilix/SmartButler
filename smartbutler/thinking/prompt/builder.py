"""ButlerPromptBuilder —— 管家 system prompt 组装器。

职责:
1. 拼装管家人设(PERSONA)。
2. 注入 Skill snippets(Phase 5 占位,Phase 4 接 list[str])。
3. 告知 LLM 可用工具列表(name + description,LLM 据此路由)。

不做什么:
- 不做情感层(personality / memory),那些是 Phase 6 注入。
- 不做意图识别,那是 decide 节点的事,不是 prompt 层的事。

设计原则(参考 TECHNICAL_DESIGN.md §3.2.5):
1. **零业务耦合**: 本类不知道具体 Sub-Agent / Tool,只接受 list 注入。
2. **可单测**: build() 纯函数,无副作用。
3. **可演进**: 后续 Phase 6 注入 personality / memory 时,在 PERSONA 段后面追加即可。
"""
from __future__ import annotations

from langchain_core.tools import BaseTool

# 管家固定人设 —— 简短、有性格、不啰嗦。
# 后续 Phase 6 会用 Personality 注入器动态改写,这里只保留骨架。
_BUTLER_PERSONA = """\
你是 SmartButler 管家,一个**有性格、有记忆、懂上下文**的智能助手。
你的目标:以最自然的方式帮用户解决问题,而不是机械地列工具。

回答原则:
1. **简洁优先**:能一句话说清就别写三段。
2. **承认不知道**:信息不足时,直接问用户,不要瞎猜。
3. **优先自己回答**:能直接答的(常识、闲聊、简单计算)就直接答,**不要**无意义调工具。
4. **工具调用要果断**:确实需要外部信息 / 操作时,选最合适的工具,一次只调一个。""".strip()

_TOOL_AWARENESS_TEMPLATE = """\
## 可用工具

你有以下工具可用。**只在确实需要时**调用:
{tool_lines}""".strip()

_SKILL_SNIPPETS_HEADER = """\
## 已加载 Skills(按需遵循)

以下 Skills 已加载,内容会指导你处理特定类型任务时该怎么做。**不是**所有任务都要用,看场景。"""


class ButlerPromptBuilder:
    """管家 system prompt 组装器。

    用法::

        builder = ButlerPromptBuilder()
        prompt = builder.build(
            tool_specs=[tool1.to_tool_spec(), tool2.to_langchain_tool()],
            skill_prompt_snippets=["# pdf-summary\\n...步骤..."],
        )
        llm_with_tools = llm.bind_tools(tool_specs)
        # 把 prompt 作为第一条 system message 喂给 LLM
    """

    def build(
        self,
        *,
        tool_specs: list[BaseTool] | None = None,
        skill_prompt_snippets: list[str] | None = None,
    ) -> str:
        """组装管家 system prompt。

        Args:
            tool_specs: 可用工具列表(BaseTool 实例或 LangChain StructuredTool)。
                兼容 BaseTool(取 name/description)和 LangChain tool(也取 name/description)。
            skill_prompt_snippets: Phase 5 注入的 Skill system_prompt 片段。

        Returns:
            最终 system prompt 字符串。
        """
        parts: list[str] = [_BUTLER_PERSONA]

        if tool_specs:
            parts.append(self._render_tool_awareness(tool_specs))

        if skill_prompt_snippets:
            parts.append(self._render_skill_snippets(skill_prompt_snippets))

        return "\n\n".join(parts)

    @staticmethod
    def _render_tool_awareness(tool_specs: list[BaseTool]) -> str:
        lines: list[str] = []
        for tool in tool_specs:
            name = getattr(tool, "name", "<unknown>")
            desc = (getattr(tool, "description", "") or "").strip()
            # 截断过长 description,避免 prompt 爆炸
            if len(desc) > 240:
                desc = desc[:237] + "..."
            lines.append(f"- **{name}**: {desc}")
        return _TOOL_AWARENESS_TEMPLATE.format(tool_lines="\n".join(lines))

    @staticmethod
    def _render_skill_snippets(snippets: list[str]) -> str:
        body = "\n\n---\n\n".join(s.strip() for s in snippets if s and s.strip())
        return f"{_SKILL_SNIPPETS_HEADER}\n\n{body}" if body else ""


__all__ = ["ButlerPromptBuilder"]
