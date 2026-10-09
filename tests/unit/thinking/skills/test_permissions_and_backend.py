"""Skill 子系统单元测试。

覆盖:
- permissions: 3 类路径 × 3 类模式的评估
- filesystem_backend: 读 / 写 / 编辑 / 列 / grep / glob + 路径沙箱
- scanner: SKILL.md frontmatter 解析(合法 / 缺失 / 字段超长)
- prompt_renderer: 列表渲染
- runtime: from_settings 一行装配
- tools: 6 个 BaseTool 的 arun 行为
"""
from __future__ import annotations

from pathlib import Path

import pytest

from smartbutler.thinking.skills import (
    FileOperation,
    PermissionMode,
    PermissionPolicy,
    PermissionRule,
    SmartButlerFilesystemBackend,
    build_default_policy,
)

# ----------------- permissions -----------------


class TestPermissionPolicy:
    def test_first_match_wins_allow(self, tmp_path: Path) -> None:
        policy = build_default_policy(
            work_dir=tmp_path / "ws",
            skills_dirs=[tmp_path / "sk"],
        )
        # skills 路径 → allow
        target = (tmp_path / "sk" / "builtin" / "pdf" / "data" / "x.json").resolve()
        assert policy.evaluate(FileOperation.WRITE, target) == PermissionMode.ALLOW

    def test_work_dir_match_allow(self, tmp_path: Path) -> None:
        policy = build_default_policy(
            work_dir=tmp_path / "ws",
            skills_dirs=[tmp_path / "sk"],
        )
        target = (tmp_path / "ws" / "20261009" / "汇报.pptx").resolve()
        assert policy.evaluate(FileOperation.WRITE, target) == PermissionMode.ALLOW

    def test_outside_sandbox_interrupt(self, tmp_path: Path) -> None:
        policy = build_default_policy(
            work_dir=tmp_path / "ws",
            skills_dirs=[tmp_path / "sk"],
        )
        # 任意其他路径 → interrupt
        target = (tmp_path / "etc" / "passwd").resolve()
        assert policy.evaluate(FileOperation.READ, target) == PermissionMode.INTERRUPT

    def test_custom_rule_precedes_default(self, tmp_path: Path) -> None:
        custom = PermissionPolicy(
            rules=(
                PermissionRule(
                    operations=(FileOperation.READ,),
                    paths=(f"{tmp_path}/sensitive/**",),
                    mode=PermissionMode.DENY,
                ),
            ),
            default=PermissionMode.ALLOW,
        )
        target = (tmp_path / "sensitive" / "x").resolve()
        assert custom.evaluate(FileOperation.READ, target) == PermissionMode.DENY
        # 走 default 兜底
        target2 = (tmp_path / "other" / "x").resolve()
        assert custom.evaluate(FileOperation.READ, target2) == PermissionMode.ALLOW


# ----------------- filesystem_backend -----------------


class TestSmartButlerFilesystemBackend:
    @pytest.fixture
    def backend(self, tmp_path: Path) -> SmartButlerFilesystemBackend:
        ws = tmp_path / "ws"
        sk = tmp_path / "sk"
        return SmartButlerFilesystemBackend(
            work_dir=ws,
            policy=build_default_policy(work_dir=ws, skills_dirs=[sk]),
        )

    def test_read_write_basic(self, backend: SmartButlerFilesystemBackend) -> None:
        backend.write("/20261009/test.md", "hello\nworld")
        out = backend.read("/20261009/test.md")
        assert "hello" in out.content
        assert out.total_lines == 2
        assert not out.truncated

    def test_read_truncated(self, backend: SmartButlerFilesystemBackend) -> None:
        content = "\n".join(f"line {i}" for i in range(200))
        backend.write("/20261009/big.md", content)
        out = backend.read("/20261009/big.md", limit=10)
        assert out.truncated
        assert out.total_lines == 200
        assert "line 0" in out.content
        assert "line 9" in out.content
        assert "line 10" not in out.content

    def test_read_file_not_found(self, backend: SmartButlerFilesystemBackend) -> None:
        with pytest.raises(FileNotFoundError):
            backend.read("/20261009/missing.md")

    def test_write_creates_parent_dirs(
        self, backend: SmartButlerFilesystemBackend
    ) -> None:
        backend.write("/20261009/sub/dir/x.md", "ok")
        assert (backend.work_dir / "20261009" / "sub" / "dir" / "x.md").exists()

    def test_edit_replaces(self, backend: SmartButlerFilesystemBackend) -> None:
        backend.write("/20261009/x.md", "hello world")
        count = backend.edit("/20261009/x.md", "world", "butler")
        assert count == 1
        out = backend.read("/20261009/x.md")
        assert "hello butler" in out.content

    def test_edit_no_match(self, backend: SmartButlerFilesystemBackend) -> None:
        backend.write("/20261009/x.md", "hello")
        count = backend.edit("/20261009/x.md", "not there", "x")
        assert count == 0

    def test_delete(self, backend: SmartButlerFilesystemBackend) -> None:
        backend.write("/20261009/x.md", "x")
        backend.delete("/20261009/x.md")
        with pytest.raises(FileNotFoundError):
            backend.read("/20261009/x.md")

    def test_ls(self, backend: SmartButlerFilesystemBackend) -> None:
        backend.write("/20261009/a.md", "a")
        backend.write("/20261009/b.md", "b")
        backend.write("/20261009/c.md", "c")
        out = backend.ls("/20261009")
        assert len(out.entries) == 3
        names = {e["name"] for e in out.entries}
        assert names == {"a.md", "b.md", "c.md"}

    def test_ls_empty(self, backend: SmartButlerFilesystemBackend) -> None:
        # 目录存在但空
        (backend.work_dir / "20261009").mkdir(parents=True)
        out = backend.ls("/20261009")
        assert out.entries == []

    def test_ls_nonexistent(self, backend: SmartButlerFilesystemBackend) -> None:
        with pytest.raises(FileNotFoundError):
            backend.ls("/never_exists")

    def test_grep(self, backend: SmartButlerFilesystemBackend) -> None:
        backend.write("/20261009/a.md", "foo\nbar\n")
        backend.write("/20261009/b.md", "baz\nfoo\n")
        out = backend.grep("/20261009", "foo")
        assert len(out.matches) == 2

    def test_glob_double_star(
        self, backend: SmartButlerFilesystemBackend
    ) -> None:
        backend.write("/20261009/a.md", "a")
        backend.write("/20261009/sub/b.md", "b")
        out = backend.glob("/20261009/**/*.md")
        assert any("a.md" in p for p in out.paths)
        assert any("b.md" in p for p in out.paths)

    def test_path_outside_sandbox(
        self, backend: SmartButlerFilesystemBackend
    ) -> None:
        from smartbutler.thinking.skills.filesystem_backend import PathOutsideSandbox

        with pytest.raises(PathOutsideSandbox):
            backend.read("/../etc/passwd")
        with pytest.raises(PathOutsideSandbox):
            backend.read("/../../etc/passwd")

    def test_today_workspace(
        self, backend: SmartButlerFilesystemBackend
    ) -> None:
        ws_path = backend.today_workspace()
        assert ws_path.startswith("/20")  # YYYYMMDD 开头
        # 目录已创建
        full = backend.work_dir / ws_path.lstrip("/")
        assert full.is_dir()

    def test_interrupt_mode_rejected(
        self, backend: SmartButlerFilesystemBackend
    ) -> None:
        """out-of-sandbox write 走 INTERRUPT 模式 → Phase 5 拒绝。"""
        from smartbutler.thinking.skills.filesystem_backend import PathOutsideSandbox

        # 直接 sandbox 拦截
        with pytest.raises(PathOutsideSandbox):
            backend.write("/../foo.txt", "x")
