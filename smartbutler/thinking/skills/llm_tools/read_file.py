"""read_file 工具 — Skill 加载 + workspace 读取。

LLM 看到的能力:
- 读 skill 的 SKILL.md(看完 frontmatter 后第二步,看正文)
- 读 workspace/YYYYMMDD/ 下的文件(回顾历史)
- 读 smartbutler/skills/builtin/<name>/data/ 下的 cookie/cache

所属:thinking/skills/llm_tools/。
这是 SmartButlerFilesystemBackend.read() 的 LLM 适配层(以 BaseTool 形式暴露给管家),
本质是"业务方法 + LLM 接口",不是 capabilities 层的通用 tool 能力。
所以住在 thinking 层,而非 capabilities/tools/skills/。
"""
from __future__ import annotations

from typing import Any

from smartbutler.capabilities.tools.base import BaseTool
from smartbutler.capabilities.tools.types import (
    Permission,
    ToolContext,
)
from smartbutler.thinking.skills.filesystem_backend import (
    PathOutsideSandbox,
    PermissionRejected,
    SmartButlerFilesystemBackend,
)


class ReadFileTool(BaseTool):
    """读取 work_dir 下的文本文件(限行数防 prompt 爆炸)。"""

    name = "read_file"
    description = (
        "读取文件内容,返回文本(限 100 行,超出截断)。"
        "用于:1) 加载 skill 指令(读 SKILL.md);"
        "2) 读 workspace/YYYYMMDD/ 下的历史产出;"
        "3) 读 cookie/cache。路径相对于工作目录(以 / 开头,如 /workspace/20261009/笔记.md)。"
    )
    scope = "global"  # type: ignore[assignment]
    required_permissions: frozenset[Permission] = frozenset({Permission.FILE_READ})
    readonly = True
    timeout_seconds: float | None = 30.0
    idempotent = True

    def __init__(self, backend: SmartButlerFilesystemBackend) -> None:
        self._backend = backend
        BaseTool.__init__(self)

    async def arun(self, *, path: str, limit: int = 100, **_: Any) -> str:
        try:
            out = self._backend.read(path, limit=limit)
        except (FileNotFoundError, IsADirectoryError, PermissionRejected, PathOutsideSandbox) as exc:
            return f"[read_file error] {type(exc).__name__}: {exc}"
        truncated_note = (
            f"\n\n[已截断,共 {out.total_lines} 行,本结果只显示前 {limit} 行]"
            if out.truncated
            else ""
        )
        return out.content + truncated_note

    def to_tool_spec(self) -> Any:  # type: ignore[override]
        # FunctionTool 风格 schema,让 LLM 看到参数细节
        from smartbutler.capabilities.llm.types import FunctionSpec, ToolSpec

        return ToolSpec(
            type="function",
            function=FunctionSpec(
                name=self.name,
                description=self.description,
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "虚拟路径(以 / 开头,相对 work_dir)",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "最大行数,默认 100",
                        },
                    },
                    "required": ["path"],
                },
            ),
        )


__all__ = ["ReadFileTool"]


# 抑制 ToolContext unused
_ = ToolContext
