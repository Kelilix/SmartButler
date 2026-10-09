"""write_file 工具 — 写文件到 work_dir。

LLM 看到的能力:
- 写 skill 自有数据(skills/builtin/<name>/data/)
- 写工作产出(workspace/YYYYMMDD/汇报.pptx 等)
- 不在 work_dir 内的路径会触发 PermissionRejected
"""
from __future__ import annotations

from typing import Any

from smartbutler.capabilities.tools.base import BaseTool
from smartbutler.capabilities.tools.types import Permission
from smartbutler.thinking.skills.filesystem_backend import (
    PathOutsideSandbox,
    PermissionRejected,
    SmartButlerFilesystemBackend,
)


class WriteFileTool(BaseTool):
    """写文件(覆盖)。"""

    name = "write_file"
    description = (
        "写入文件(覆盖现有内容)。"
        "用于:1) 写 skill 自有数据;"
        "2) 写工作产出到 workspace/YYYYMMDD/;"
        "3) 写报告/PPT/笔记。"
        "路径必须在 work_dir 内(以 / 开头),否则会被权限拒绝。"
    )
    scope = "global"  # type: ignore[assignment]
    required_permissions: frozenset[Permission] = frozenset({Permission.FILE_WRITE})
    readonly = False
    timeout_seconds: float | None = 30.0
    idempotent = False  # 写文件非幂等

    def __init__(self, backend: SmartButlerFilesystemBackend) -> None:
        self._backend = backend
        BaseTool.__init__(self)

    async def arun(self, *, path: str, content: str, **_: Any) -> str:
        try:
            self._backend.write(path, content)
        except (PermissionRejected, PathOutsideSandbox) as exc:
            return f"[write_file error] {type(exc).__name__}: {exc}"
        return f"✅ 已写入 {path}({len(content)} 字符)"

    def to_tool_spec(self) -> Any:  # type: ignore[override]
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
                        "content": {
                            "type": "string",
                            "description": "要写入的完整内容",
                        },
                    },
                    "required": ["path", "content"],
                },
            ),
        )


__all__ = ["WriteFileTool"]
