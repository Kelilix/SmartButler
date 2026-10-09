"""SkillRuntime 装配 + 7 个文件 BaseTool 测试。"""
from __future__ import annotations

from pathlib import Path

import pytest

from smartbutler.capabilities.tools.base import BaseTool
from smartbutler.thinking.skills import (
    SkillRuntime,
    SmartButlerFilesystemBackend,
    build_default_policy,
)
from smartbutler.thinking.skills.llm_tools import build_default_skill_tools
from smartbutler.thinking.skills.llm_tools.read_file import ReadFileTool
from smartbutler.thinking.skills.llm_tools.write_file import WriteFileTool


@pytest.fixture
def backend(tmp_path: Path) -> SmartButlerFilesystemBackend:
    ws = tmp_path / "ws"
    sk = tmp_path / "sk"
    return SmartButlerFilesystemBackend(
        work_dir=ws,
        policy=build_default_policy(work_dir=ws, skills_dirs=[sk]),
    )


class TestFileTools:
    def test_build_default_skill_tools_returns_six(
        self, backend: SmartButlerFilesystemBackend
    ) -> None:
        tools = build_default_skill_tools(backend)
        # 6 个工具 + 1 个 read_file
        assert len(tools) >= 6
        names = {t.name for t in tools}
        assert "read_file" in names
        assert "write_file" in names
        assert "ls" in names

    def test_read_file_tool_writes_and_reads(
        self, backend: SmartButlerFilesystemBackend
    ) -> None:
        tool = ReadFileTool(backend)
        # 先用 write_file tool 写
        WriteFileTool(backend)  # ensure exists
        # 直接通过 backend 写(避免 ad-hoc)
        backend.write("/20261009/x.md", "hello world")
        # read
        import asyncio

        out = asyncio.run(tool.arun(path="/20261009/x.md"))
        assert "hello world" in out

    def test_write_file_tool_returns_success(
        self, backend: SmartButlerFilesystemBackend
    ) -> None:
        tool = WriteFileTool(backend)
        import asyncio

        out = asyncio.run(tool.arun(path="/20261009/y.md", content="x" * 10))
        assert "✅" in out
        assert (backend.work_dir / "20261009" / "y.md").exists()

    def test_read_file_truncates(
        self, backend: SmartButlerFilesystemBackend
    ) -> None:
        backend.write("/20261009/big.md", "\n".join(f"L{i}" for i in range(200)))
        tool = ReadFileTool(backend)
        import asyncio

        out = asyncio.run(tool.arun(path="/20261009/big.md", limit=5))
        assert "已截断" in out
        assert "L0" in out
        assert "L4" in out
        assert "L5" not in out

    def test_read_file_not_found(
        self, backend: SmartButlerFilesystemBackend
    ) -> None:
        tool = ReadFileTool(backend)
        import asyncio

        out = asyncio.run(tool.arun(path="/20261009/missing.md"))
        assert "error" in out.lower()

    def test_write_file_outside_sandbox(
        self, backend: SmartButlerFilesystemBackend
    ) -> None:
        tool = WriteFileTool(backend)
        import asyncio

        out = asyncio.run(tool.arun(path="/../escape.txt", content="x"))
        assert "error" in out.lower()

    def test_tool_metadata(self, backend: SmartButlerFilesystemBackend) -> None:
        tools = build_default_skill_tools(backend)
        for t in tools:
            assert isinstance(t, BaseTool)
            assert t.name
            assert t.description
            # 每个工具都有 to_tool_spec
            spec = t.to_tool_spec()
            # ToolSpec 是 Pydantic model,用 attribute 访问
            assert spec.type == "function"
            assert spec.function.name == t.name
            assert spec.function.description
            assert spec.function.parameters.get("type") == "object"


class TestSkillRuntime:
    def test_from_settings_no_skills(self, tmp_path: Path, monkeypatch) -> None:
        """空 builtin 目录 → runtime 仍然可装配,只是 skills=[]."""
        # 临时 env 让 SkillSettings 指向 tmp_path
        monkeypatch.setenv("SMARTBUTLER_SKILL_WORKSPACE_DIR", str(tmp_path / "ws"))
        monkeypatch.setenv(
            "SMARTBUTLER_SKILL_BUILTIN_SKILLS_DIR", str(tmp_path / "sk")
        )
        # 这次 from_settings 不带参,让它从 env 读
        from smartbutler.config.skills import load_skill_settings

        settings = load_skill_settings()
        runtime = SkillRuntime.from_settings(settings=settings)
        assert runtime.skills == []
        assert runtime.backend.work_dir == settings.workspace_dir.expanduser().resolve()

    def test_from_settings_with_skill(self, tmp_path: Path, monkeypatch) -> None:
        # 准备 1 个 skill
        sk_dir = tmp_path / "sk" / "builtin" / "demo"
        sk_dir.mkdir(parents=True)
        (sk_dir / "SKILL.md").write_text(
            "---\nname: demo\ndescription: A demo skill\n---\nbody\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("SMARTBUTLER_SKILL_WORKSPACE_DIR", str(tmp_path / "ws"))
        monkeypatch.setenv("SMARTBUTLER_SKILL_BUILTIN_SKILLS_DIR", str(sk_dir.parent))

        from smartbutler.config.skills import load_skill_settings

        settings = load_skill_settings()
        runtime = SkillRuntime.from_settings(settings=settings)
        assert len(runtime.skills) == 1
        assert runtime.skills[0].name == "demo"
        # render_prompt_snippet 应含 "demo"
        prompt = runtime.render_prompt_snippet()
        assert "demo" in prompt

    def test_build_file_tools_includes_read(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setenv("SMARTBUTLER_SKILL_WORKSPACE_DIR", str(tmp_path / "ws"))
        monkeypatch.setenv(
            "SMARTBUTLER_SKILL_BUILTIN_SKILLS_DIR", str(tmp_path / "sk")
        )
        from smartbutler.config.skills import load_skill_settings

        settings = load_skill_settings()
        runtime = SkillRuntime.from_settings(settings=settings)
        tools = runtime.build_file_tools()
        names = {t.name for t in tools}
        assert "read_file" in names
        assert "write_file" in names
        assert "ls" in names

    def test_find_skill_by_name(self, tmp_path: Path, monkeypatch) -> None:
        sk_dir = tmp_path / "sk" / "builtin" / "demo"
        sk_dir.mkdir(parents=True)
        (sk_dir / "SKILL.md").write_text(
            "---\nname: demo\ndescription: ok\n---\nbody\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("SMARTBUTLER_SKILL_WORKSPACE_DIR", str(tmp_path / "ws"))
        monkeypatch.setenv("SMARTBUTLER_SKILL_BUILTIN_SKILLS_DIR", str(sk_dir.parent))

        from smartbutler.config.skills import load_skill_settings

        settings = load_skill_settings()
        runtime = SkillRuntime.from_settings(settings=settings)
        assert runtime.find_skill_by_name("demo") is not None
        assert runtime.find_skill_by_name("nonexistent") is None
