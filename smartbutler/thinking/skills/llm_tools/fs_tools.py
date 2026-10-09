"""edit_file / delete_file / ls / grep / glob 工具集合。

跟 read_file / write_file 一并暴露给 LLM。

所属:thinking/skills/llm_tools/。
这 5 个 tool 是 SmartButlerFilesystemBackend 对应方法的 LLM 适配层(以 BaseTool 形式暴露),
本质是"业务方法 + LLM 接口",不是 capabilities 层的通用 tool 能力,
所以住在 thinking 层,而非 capabilities/tools/skills/。
"""
from __future__ import annotations

import json
from typing import Any

from smartbutler.capabilities.tools.base import BaseTool
from smartbutler.capabilities.tools.types import Permission
from smartbutler.thinking.skills.filesystem_backend import (
    PathOutsideSandbox,
    PermissionRejected,
    SmartButlerFilesystemBackend,
)
from smartbutler.thinking.skills.llm_tools.read_file import ReadFileTool
from smartbutler.thinking.skills.llm_tools.write_file import WriteFileTool


class EditFileTool(BaseTool):
    """字符串替换编辑(对已存在的文件做局部修改)。"""

    name = "edit_file"
    description = (
        "对已存在文件做字符串替换(找到 old 替换为 new),返回替换次数。"
        "适合改 SKILL.md 的某一段、修改报告里的某段文字。"
    )
    scope = "global"  # type: ignore[assignment]
    required_permissions: frozenset[Permission] = frozenset({Permission.FILE_WRITE})
    readonly = False
    timeout_seconds: float | None = 30.0
    idempotent = False

    def __init__(self, backend: SmartButlerFilesystemBackend) -> None:
        self._backend = backend
        BaseTool.__init__(self)

    async def arun(self, *, path: str, old: str, new: str, **_: Any) -> str:
        try:
            count = self._backend.edit(path, old, new)
        except (FileNotFoundError, PermissionRejected, PathOutsideSandbox) as exc:
            return f"[edit_file error] {type(exc).__name__}: {exc}"
        if count == 0:
            return f"[edit_file warning] 没在 {path} 找到 {old!r} 的匹配"
        return f"✅ 已替换 {path} 中 {count} 处 {old!r} → {new!r}"

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
                        "path": {"type": "string"},
                        "old": {"type": "string", "description": "要被替换的原文"},
                        "new": {"type": "string", "description": "替换后的文本"},
                    },
                    "required": ["path", "old", "new"],
                },
            ),
        )


class DeleteFileTool(BaseTool):
    """删除文件(谨慎使用)。"""

    name = "delete_file"
    description = "删除文件(谨慎)。文件不存在会报错。"
    scope = "global"  # type: ignore[assignment]
    required_permissions: frozenset[Permission] = frozenset({Permission.FILE_WRITE})
    readonly = False
    timeout_seconds: float | None = 30.0
    idempotent = True  # 删两次效果一样

    def __init__(self, backend: SmartButlerFilesystemBackend) -> None:
        self._backend = backend
        BaseTool.__init__(self)

    async def arun(self, *, path: str, **_: Any) -> str:
        try:
            self._backend.delete(path)
        except (FileNotFoundError, PermissionRejected, PathOutsideSandbox) as exc:
            return f"[delete_file error] {type(exc).__name__}: {exc}"
        return f"✅ 已删除 {path}"

    def to_tool_spec(self) -> Any:  # type: ignore[override]
        from smartbutler.capabilities.llm.types import FunctionSpec, ToolSpec

        return ToolSpec(
            type="function",
            function=FunctionSpec(
                name=self.name,
                description=self.description,
                parameters={
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
            ),
        )


class LsTool(BaseTool):
    """列目录(看 work_dir 下有什么)。"""

    name = "ls"
    description = (
        "列目录(返回 name / path / is_dir / size)。"
        "用 `ls /workspace/20261009` 看今天的产出,或 `ls /skills/builtin` 看内置 skill 列表。"
    )
    scope = "global"  # type: ignore[assignment]
    required_permissions: frozenset[Permission] = frozenset({Permission.FILE_READ})
    readonly = True
    timeout_seconds: float | None = 15.0
    idempotent = True

    def __init__(self, backend: SmartButlerFilesystemBackend) -> None:
        self._backend = backend
        BaseTool.__init__(self)

    async def arun(self, *, path: str, **_: Any) -> str:
        try:
            out = self._backend.ls(path)
        except (FileNotFoundError, NotADirectoryError, PermissionRejected, PathOutsideSandbox) as exc:
            return f"[ls error] {type(exc).__name__}: {exc}"
        if not out.entries:
            return f"(空目录: {path})"
        lines = [f"{'[DIR] ' if e['is_dir'] else '      '}{e['name']:30s} {e['path']}" for e in out.entries]
        return "\n".join(lines)

    def to_tool_spec(self) -> Any:  # type: ignore[override]
        from smartbutler.capabilities.llm.types import FunctionSpec, ToolSpec

        return ToolSpec(
            type="function",
            function=FunctionSpec(
                name=self.name,
                description=self.description,
                parameters={
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
            ),
        )


class GrepTool(BaseTool):
    """在 work_dir 下递归搜正则(子串匹配,大小写敏感)。"""

    name = "grep"
    description = "在指定目录下递归搜 pattern(正则),返回匹配的文件 + 行号 + 文本。"
    scope = "global"  # type: ignore[assignment]
    required_permissions: frozenset[Permission] = frozenset({Permission.FILE_READ})
    readonly = True
    timeout_seconds: float | None = 30.0
    idempotent = True

    def __init__(self, backend: SmartButlerFilesystemBackend) -> None:
        self._backend = backend
        BaseTool.__init__(self)

    async def arun(self, *, path: str, pattern: str, **_: Any) -> str:
        try:
            out = self._backend.grep(path, pattern)
        except (FileNotFoundError, PermissionRejected, PathOutsideSandbox) as exc:
            return f"[grep error] {type(exc).__name__}: {exc}"
        if not out.matches:
            return f"(没在 {path} 下找到 {pattern!r})"
        return "\n".join(
            f"{m['path']}:{m['line']}: {m['text']}" for m in out.matches[:200]
        )

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
                        "path": {"type": "string"},
                        "pattern": {"type": "string", "description": "正则表达式"},
                    },
                    "required": ["path", "pattern"],
                },
            ),
        )


class GlobTool(BaseTool):
    """glob 匹配文件路径。"""

    name = "glob"
    description = (
        "glob 模式匹配(支持 `**` 递归),返回文件路径列表。"
        "例: `glob /workspace/**/*.md` 找所有工作目录下的 markdown。"
    )
    scope = "global"  # type: ignore[assignment]
    required_permissions: frozenset[Permission] = frozenset({Permission.FILE_READ})
    readonly = True
    timeout_seconds: float | None = 15.0
    idempotent = True

    def __init__(self, backend: SmartButlerFilesystemBackend) -> None:
        self._backend = backend
        BaseTool.__init__(self)

    async def arun(self, *, pattern: str, **_: Any) -> str:
        try:
            out = self._backend.glob(pattern)
        except (FileNotFoundError, PermissionRejected) as exc:
            return f"[glob error] {type(exc).__name__}: {exc}"
        if not out.paths:
            return f"(没匹配到: {pattern})"
        return "\n".join(out.paths)

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
                        "pattern": {
                            "type": "string",
                            "description": "glob 模式(支持 **)",
                        },
                    },
                    "required": ["pattern"],
                },
            ),
        )


def build_default_skill_tools(
    backend: SmartButlerFilesystemBackend,
) -> list[BaseTool]:
    """构造默认 7 个文件工具(给 ToolRegistry.register_many 用)。

    顺序:read / write / edit / delete / ls / grep / glob。
    """
    return [
        ReadFileTool(backend),  # type: ignore[abstract]
        WriteFileTool(backend),  # type: ignore[abstract]
        EditFileTool(backend),  # type: ignore[abstract]
        DeleteFileTool(backend),  # type: ignore[abstract]
        LsTool(backend),  # type: ignore[abstract]
        GrepTool(backend),  # type: ignore[abstract]
        GlobTool(backend),  # type: ignore[abstract]
    ]


__all__ = [
    "EditFileTool",
    "DeleteFileTool",
    "LsTool",
    "GrepTool",
    "GlobTool",
    "build_default_skill_tools",
]


# 抑制 json unused
_ = json
