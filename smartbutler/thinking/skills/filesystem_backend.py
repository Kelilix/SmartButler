"""SmartButler 文件系统后端(精简版 — 不直接用 LangChain FilesystemBackend)。

为什么不直接用 deepagents.backends.filesystem.FilesystemBackend:
- 它的 ReadResult 是 dataclass 复杂结构,跟我们的 BaseTool ToolResult 协议不齐
- 它内置 max_file_size / ripgrep 等,Phase 5 用不到
- 简化实现 ~150 行,语义清晰,易单测

设计:
- 直接用 pathlib
- 路径沙箱:绝对路径必须落在 work_dir 内
- 路径返回:对外总是 POSIX 形式(以 / 开头)
- 写操作走 PermissionPolicy 校验

不依赖 LangChain / deepagents。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import structlog

from smartbutler.thinking.skills.permissions import (
    FileOperation,
    PermissionMode,
    PermissionPolicy,
    resolve_today_workspace,
)

_logger = structlog.get_logger(__name__)


# ----------------- 结果类型 -----------------


@dataclass
class ReadOutput:
    """读文件输出(对应 read_file 工具的返回)。"""

    content: str
    total_lines: int
    truncated: bool = False


@dataclass
class ListOutput:
    """列目录输出(对应 ls 工具的返回)。"""

    entries: list[dict[str, object]]


@dataclass
class GrepOutput:
    """grep 输出(对应 grep 工具的返回)。"""

    matches: list[dict[str, object]]


@dataclass
class GlobOutput:
    """glob 输出(对应 glob 工具的返回)。"""

    paths: list[str]


# ----------------- 异常 -----------------


class BackendError(Exception):
    """后端错误基类。"""


class PermissionRejected(BackendError):  # noqa: N818
    """权限拒绝(对应 INTERRUPT/DENY 模式)。"""


class PathOutsideSandbox(BackendError):  # noqa: N818
    """路径逃逸沙箱(对应 virtual_mode 的逃逸拦截)。"""


# ----------------- 后端本体 -----------------


class SmartButlerFilesystemBackend:
    """SmartButler 精简版 filesystem backend。

    能力:
    - read(path, limit=100) → 文本
    - write(path, content) → 写文件
    - edit(path, old, new) → 字符串替换
    - delete(path) → 删除文件
    - ls(path) → 列目录
    - grep(path, pattern) → 行搜索
    - glob(pattern) → glob 匹配
    - today_workspace() → 自动建 workspace/YYYYMMDD/

    约束:
    - 所有路径必须在 work_dir 内(沙箱)
    - 写操作走 PermissionPolicy 校验
    - INTERRUPT 模式:Phase 5 阶段抛 PermissionRejected(等 Phase 7 接 HITL)
    """

    def __init__(
        self,
        *,
        work_dir: Path,
        policy: PermissionPolicy,
    ) -> None:
        self._work_dir = work_dir.expanduser().resolve()
        self._policy = policy
        # 确保 work_dir 存在
        self._work_dir.mkdir(parents=True, exist_ok=True)
        _logger.info(
            "fs_backend.initialized",
            work_dir=str(self._work_dir),
            rule_count=len(self._policy.rules),
        )

    # ---------- 路径工具 ----------

    def _resolve(self, path: str) -> Path:
        """解析路径并校验在沙箱内。

        Phase 5 简化:path 总是相对于 work_dir 的虚拟路径(以 / 开头)。
        """
        if not path:
            msg = "path 不能为空"
            raise ValueError(msg)
        # 去除前导 /,拼到 work_dir
        relative = path.lstrip("/")
        target = (self._work_dir / relative).resolve()
        # 沙箱校验:必须以 work_dir 开头
        try:
            target.relative_to(self._work_dir)
        except ValueError as exc:
            raise PathOutsideSandbox(
                f"路径 {path} 逃逸沙箱(work_dir={self._work_dir})"
            ) from exc
        return target

    def _check_permission(
        self,
        op: FileOperation,
        target: Path,
    ) -> None:
        """校验 (op, target) 是否被 PermissionPolicy 允许。"""
        mode = self._policy.evaluate(op, target)
        if mode == PermissionMode.ALLOW:
            return
        if mode == PermissionMode.DENY:
            msg = f"权限拒绝(deny): {op.value} {target}"
            raise PermissionRejected(msg)
        if mode == PermissionMode.INTERRUPT:
            # Phase 5 简化:interrupt 模式视为拒绝(等 Phase 7 接 HITL 中间件)
            msg = (
                f"权限需要审批(interrupt 模式): {op.value} {target}。"
                "Phase 5 暂不支持出界审批,等 Phase 7 接入 LangChain HumanInTheLoop 中间件。"
            )
            _logger.warning("fs_backend.permission_interrupt", path=str(target), op=op.value)
            raise PermissionRejected(msg)
        msg = f"未知权限模式: {mode}"
        raise RuntimeError(msg)

    # ---------- 读 ----------

    def read(self, path: str, *, limit: int = 100) -> ReadOutput:
        """读文本文件,limit 是行数上限(默认 100,防止 prompt 爆炸)。

        大文件按 limit 截断,标记 truncated=True。
        """
        target = self._resolve(path)
        self._check_permission(FileOperation.READ, target)
        if not target.exists() or not target.is_file():
            msg = f"文件不存在: {path}"
            raise FileNotFoundError(msg)
        text = target.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        truncated = len(lines) > limit
        out_lines = lines[:limit]
        return ReadOutput(
            content="\n".join(out_lines),
            total_lines=len(lines),
            truncated=truncated,
        )

    # ---------- 写 ----------

    def write(self, path: str, content: str) -> None:
        """写文件(content 是完整内容,不是 patch)。"""
        target = self._resolve(path)
        self._check_permission(FileOperation.WRITE, target)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        _logger.info("fs_backend.write", path=str(target), size=len(content))

    def edit(self, path: str, old: str, new: str) -> int:
        """字符串替换编辑,返回替换次数(0 = 没找到)。"""
        target = self._resolve(path)
        self._check_permission(FileOperation.WRITE, target)
        if not target.exists():
            msg = f"文件不存在: {path}"
            raise FileNotFoundError(msg)
        text = target.read_text(encoding="utf-8")
        if old not in text:
            return 0
        count = text.count(old)
        new_text = text.replace(old, new)
        target.write_text(new_text, encoding="utf-8")
        _logger.info("fs_backend.edit", path=str(target), occurrences=count)
        return count

    def delete(self, path: str) -> None:
        """删除文件。不存在抛错(防止 LLM 误删)。"""
        target = self._resolve(path)
        self._check_permission(FileOperation.WRITE, target)
        if not target.exists():
            msg = f"文件不存在: {path}"
            raise FileNotFoundError(msg)
        target.unlink()
        _logger.info("fs_backend.delete", path=str(target))

    # ---------- 列 / 搜索 ----------

    def ls(self, path: str) -> ListOutput:
        """列目录(返回 entry 列表,每项含 path / is_dir / size)。"""
        target = self._resolve(path)
        self._check_permission(FileOperation.READ, target)
        if not target.exists():
            msg = f"目录不存在: {path}"
            raise FileNotFoundError(msg)
        if not target.is_dir():
            msg = f"不是目录: {path}"
            raise NotADirectoryError(msg)
        entries: list[dict[str, object]] = []
        for child in sorted(target.iterdir(), key=lambda p: p.name):
            is_dir = child.is_dir()
            size: int | None = child.stat().st_size if child.is_file() else None
            entries.append(
                {
                    "name": child.name,
                    "path": "/" + str(child.relative_to(self._work_dir).as_posix()),
                    "is_dir": is_dir,
                    "size": size,
                }
            )
        return ListOutput(entries=entries)

    def grep(self, path: str, pattern: str) -> GrepOutput:
        """简单 grep:在 path 下递归搜 pattern(子串匹配,大小写敏感)。"""
        import re

        target = self._resolve(path)
        self._check_permission(FileOperation.READ, target)
        if not target.exists():
            msg = f"路径不存在: {path}"
            raise FileNotFoundError(msg)
        rx = re.compile(pattern)
        matches: list[dict[str, object]] = []
        for file in target.rglob("*"):
            if not file.is_file():
                continue
            try:
                for lineno, line in enumerate(
                    file.read_text(encoding="utf-8", errors="replace").splitlines(),
                    start=1,
                ):
                    if rx.search(line):
                        matches.append(
                            {
                                "path": "/" + str(file.relative_to(self._work_dir).as_posix()),
                                "line": lineno,
                                "text": line,
                            }
                        )
            except OSError:
                continue
        return GrepOutput(matches=matches)

    def glob(self, pattern: str) -> GlobOutput:
        """glob 匹配(相对 work_dir)。

        Phase 5 简化:用 pathlib.rglob 实现 `**` 递归。
        """
        # 把绝对路径形式的 pattern 转成相对 work_dir
        if pattern.startswith("/"):
            rel_pattern = pattern.lstrip("/")
        else:
            rel_pattern = pattern
        hits: list[str] = []
        if "**" in rel_pattern:
            # rglob 不直接支持 "**/*.md" 形式,需要先 walk
            # 拆 pattern:dir/**/name
            parts = rel_pattern.split("**", 1)
            base = (self._work_dir / parts[0].rstrip("/")).resolve() if parts[0] else self._work_dir
            rest = parts[1].lstrip("/")
            if not base.exists() or not base.is_dir():
                return GlobOutput(paths=[])
            for p in base.rglob(rest):
                if p.is_file():
                    hits.append("/" + str(p.relative_to(self._work_dir).as_posix()))
        else:
            for p in self._work_dir.glob(rel_pattern):
                if p.is_file():
                    hits.append("/" + str(p.relative_to(self._work_dir).as_posix()))
        return GlobOutput(paths=sorted(hits))

    # ---------- 工作目录 ----------

    def today_workspace(self) -> str:
        """返回今天的工作子目录(workspace/YYYYMMDD/)的虚拟路径。

        自动创建。
        """
        d = resolve_today_workspace(self._work_dir)
        return "/" + str(d.relative_to(self._work_dir).as_posix())

    @property
    def work_dir(self) -> Path:
        return self._work_dir


__all__ = [
    "SmartButlerFilesystemBackend",
    "ReadOutput",
    "ListOutput",
    "GrepOutput",
    "GlobOutput",
    "BackendError",
    "PermissionRejected",
    "PathOutsideSandbox",
]
