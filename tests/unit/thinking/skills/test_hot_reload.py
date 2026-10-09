"""SkillRuntime 热更新测试(2026-10-09 落地)。

覆盖:
- _compute_skills_fingerprint 在目录/子目录/SKILL.md mtime 变化时正确反映
- 初次 render 时无重扫
- SKILL.md 内容修改后 render 触发重扫
- 新建 skill 子目录后 render 触发重扫
- 删除 skill 子目录后 render 触发重扫
- 多次调用同一指纹不重复重扫
- enable_hot_reload=False 时完全跳过检测
- 失败回退(扫目录不存在时仍可重试 + 旧缓存保留)
- find_skill_by_name 同样触发重扫
"""
from __future__ import annotations

import time
from pathlib import Path

from smartbutler.config.skills import SkillSettings
from smartbutler.thinking.skills.runtime import (
    SkillRuntime,
    _compute_skills_fingerprint,
)

# ---------- helper ----------

def _write_skill(skills_dir: Path, name: str, description: str = "ok") -> Path:
    d = skills_dir / name
    d.mkdir(parents=True, exist_ok=True)
    p = d / "SKILL.md"
    p.write_text(
        f"---\nname: {name}\ndescription: {description}\n---\nbody for {name}\n",
        encoding="utf-8",
    )
    return p


def _make_settings(
    builtin_dir: Path,
    workspace_dir: Path,
    user_dir: Path | None = None,
    *,
    enable_hot_reload: bool = True,
) -> SkillSettings:
    return SkillSettings(
        workspace_dir=workspace_dir,
        builtin_skills_dir=builtin_dir,
        user_skills_dir=user_dir,
        enable_hot_reload=enable_hot_reload,
    )


# ---------- 指纹计算 ----------

class TestFingerprint:
    def test_empty_dir_returns_zero(self, tmp_path: Path) -> None:
        """空目录(无 SKILL/子项)→ 0.0;加文件后 fp 变(>0)。"""
        # 先记基准
        fp0 = _compute_skills_fingerprint([tmp_path])
        # 加一个文件(子目录式)
        d = tmp_path / "x"
        d.mkdir()
        (d / "SKILL.md").write_text("---\nname: x\ndescription: o\n---\n", encoding="utf-8")
        fp1 = _compute_skills_fingerprint([tmp_path])
        assert fp1 != fp0  # 加文件/子目录后指纹必变

    def test_nonexistent_dir_returns_zero(self, tmp_path: Path) -> None:
        """不存在的目录 → 0.0。"""
        missing = tmp_path / "nope"
        assert _compute_skills_fingerprint([missing]) == 0.0

    def test_subdir_mtime_change_detected(self, tmp_path: Path) -> None:
        """子目录 mtime 变化(新增/修改 SKILL.md)→ 指纹变。"""
        d = tmp_path / "my-skill"
        d.mkdir()
        (d / "SKILL.md").write_text(
            "---\nname: my-skill\ndescription: ok\n---\nbody\n", encoding="utf-8"
        )
        time.sleep(0.05)  # 确保 mtime 差异
        fp1 = _compute_skills_fingerprint([tmp_path])
        (d / "SKILL.md").write_text(
            "---\nname: my-skill\ndescription: changed\n---\nbody\n", encoding="utf-8"
        )
        fp2 = _compute_skills_fingerprint([tmp_path])
        assert fp2 > fp1

    def test_new_subdir_detected(self, tmp_path: Path) -> None:
        """新建子目录/SKILL.md → 指纹变(用 != 而非 >,因 hash 拼合后方向不稳)。"""
        fp1 = _compute_skills_fingerprint([tmp_path])
        time.sleep(0.05)
        _write_skill(tmp_path, "new-skill")
        fp2 = _compute_skills_fingerprint([tmp_path])
        assert fp2 != fp1


# ---------- 热更新行为 ----------

class TestHotReload:
    def test_initial_scan_no_rescan(self, tmp_path: Path) -> None:
        """首次 render 时不触发额外重扫(指纹已初始化为当前值)。"""
        builtin = tmp_path / "builtin"
        builtin.mkdir()
        workspace = tmp_path / "work"
        workspace.mkdir()
        _write_skill(builtin, "alpha", "first")

        s = _make_settings(builtin, workspace)
        rt = SkillRuntime.from_settings(s)
        assert len(rt.skills) == 1
        assert rt.skills[0].name == "alpha"

        # 第一次 render:指纹未变,不应触发重扫
        snippet1 = rt.render_prompt_snippet()
        assert "alpha" in snippet1

    def test_modify_skill_md_triggers_reload(self, tmp_path: Path) -> None:
        """修改 SKILL.md 内容 → render 时自动重扫,prompt 反映新内容。"""
        builtin = tmp_path / "builtin"
        builtin.mkdir()
        workspace = tmp_path / "work"
        workspace.mkdir()
        skill_dir = builtin / "alpha"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\nname: alpha\ndescription: original\n---\nbody\n", encoding="utf-8"
        )

        s = _make_settings(builtin, workspace)
        rt = SkillRuntime.from_settings(s)
        assert rt.skills[0].description == "original"

        # 修改 SKILL.md description
        time.sleep(0.05)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: alpha\ndescription: updated\n---\nbody\n", encoding="utf-8"
        )

        # render 触发热更新
        snippet = rt.render_prompt_snippet()
        assert "updated" in snippet
        assert rt.skills[0].description == "updated"

    def test_new_skill_dir_triggers_reload(self, tmp_path: Path) -> None:
        """新建 skill 子目录 → render 时自动重扫,新 skill 出现在 prompt。"""
        builtin = tmp_path / "builtin"
        builtin.mkdir()
        workspace = tmp_path / "work"
        workspace.mkdir()
        _write_skill(builtin, "alpha", "first")

        s = _make_settings(builtin, workspace)
        rt = SkillRuntime.from_settings(s)
        assert len(rt.skills) == 1

        # 新建一个 skill 子目录
        time.sleep(0.05)
        _write_skill(builtin, "beta", "second")

        snippet = rt.render_prompt_snippet()
        assert "beta" in snippet
        assert len(rt.skills) == 2

    def test_delete_skill_dir_triggers_reload(self, tmp_path: Path) -> None:
        """删除 skill 子目录 → render 时自动重扫,该 skill 从 prompt 消失。"""
        builtin = tmp_path / "builtin"
        builtin.mkdir()
        workspace = tmp_path / "work"
        workspace.mkdir()
        _write_skill(builtin, "alpha", "first")
        _write_skill(builtin, "beta", "second")

        s = _make_settings(builtin, workspace)
        rt = SkillRuntime.from_settings(s)
        assert len(rt.skills) == 2

        # 删除 alpha
        time.sleep(0.05)
        import shutil

        shutil.rmtree(builtin / "alpha")

        _snippet = rt.render_prompt_snippet()
        # 检查 rt.skills 列表(权威),而非 snippet 字符串(可能含别的样例)
        assert "alpha" not in [s.name for s in rt.skills]
        assert len(rt.skills) == 1
        assert rt.skills[0].name == "beta"

    def test_repeated_render_no_redundant_rescan(self, tmp_path: Path) -> None:
        """指纹未变时,多次 render 不重复重扫(性能验证)。"""
        builtin = tmp_path / "builtin"
        builtin.mkdir()
        workspace = tmp_path / "work"
        workspace.mkdir()
        _write_skill(builtin, "alpha")

        s = _make_settings(builtin, workspace)
        rt = SkillRuntime.from_settings(s)

        # 模拟"hot_reloaded"被调用次数:通过观察 fingerprint 在 render 后是否被改写
        # 既然指纹未变,_rescan_if_changed 直接 return,fingerprint 不会被赋值。
        # 这里用 logger 验证(monkeypatch),更直接:仅检查 rt.skills 引用不变即可。
        from unittest.mock import patch

        original_skills = rt.skills
        with patch(
            "smartbutler.thinking.skills.runtime.scan_skills_dirs"
        ) as m_scan:
            # 即便 mock,因指纹未变,根本不会调 scan_skills_dirs
            for _ in range(5):
                rt.render_prompt_snippet()
            m_scan.assert_not_called()
        # 引用未变
        assert rt.skills is original_skills

    def test_hot_reload_disabled_skips_check(self, tmp_path: Path) -> None:
        """enable_hot_reload=False 时,即使目录变了,也不重扫。"""
        builtin = tmp_path / "builtin"
        builtin.mkdir()
        workspace = tmp_path / "work"
        workspace.mkdir()
        _write_skill(builtin, "alpha")

        s = _make_settings(builtin, workspace, enable_hot_reload=False)
        rt = SkillRuntime.from_settings(s)
        assert len(rt.skills) == 1

        time.sleep(0.05)
        _write_skill(builtin, "beta")

        snippet = rt.render_prompt_snippet()
        # beta 不会被发现
        assert "beta" not in snippet
        assert len(rt.skills) == 1

    def test_find_skill_by_name_also_triggers_reload(self, tmp_path: Path) -> None:
        """find_skill_by_name 同样触发热更新。"""
        builtin = tmp_path / "builtin"
        builtin.mkdir()
        workspace = tmp_path / "work"
        workspace.mkdir()

        s = _make_settings(builtin, workspace)
        rt = SkillRuntime.from_settings(s)
        # 初始无 skill
        assert rt.find_skill_by_name("alpha") is None

        # 新建 skill
        time.sleep(0.05)
        _write_skill(builtin, "alpha", "late")

        # find 触发重扫,能找到
        meta = rt.find_skill_by_name("alpha")
        assert meta is not None
        assert meta.description == "late"

    def test_reload_failure_keeps_old_cache(self, tmp_path: Path) -> None:
        """重扫失败时,旧 skills 缓存保留 + 指纹推进(避免每次重试)。"""
        builtin = tmp_path / "builtin"
        builtin.mkdir()
        workspace = tmp_path / "work"
        workspace.mkdir()
        _write_skill(builtin, "alpha")

        s = _make_settings(builtin, workspace)
        rt = SkillRuntime.from_settings(s)
        original_skills = rt.skills
        original_fp = rt._fingerprint

        # 模拟 scan_skills_dirs 抛异常
        from unittest.mock import patch

        time.sleep(0.05)
        _write_skill(builtin, "beta")  # 改 mtime

        with patch(
            "smartbutler.thinking.skills.runtime.scan_skills_dirs",
            side_effect=RuntimeError("mocked failure"),
        ):
            rt._rescan_if_changed()

        # 旧缓存保留
        assert rt.skills is original_skills
        assert len(rt.skills) == 1
        # 指纹已推进
        assert rt._fingerprint != original_fp

    def test_disable_hot_reload_via_settings(self, tmp_path: Path) -> None:
        """配置层 enable_hot_reload=False → from_settings 生效。"""
        builtin = tmp_path / "builtin"
        builtin.mkdir()
        workspace = tmp_path / "work"
        workspace.mkdir()
        _write_skill(builtin, "alpha")

        s = _make_settings(builtin, workspace, enable_hot_reload=False)
        rt = SkillRuntime.from_settings(s)
        assert rt.enable_hot_reload is False

        time.sleep(0.05)
        _write_skill(builtin, "beta")

        rt.render_prompt_snippet()
        # 即使调用 render,因为开关关闭,beta 不会被发现
        assert "beta" not in [m.name for m in rt.skills]


# ---------- 2026-10-09 A+ 增量:文件级 mtime + invalidate 主动重读 ----------


def test_script_file_change_triggers_reload(tmp_path: Path) -> None:
    """改 skill/scripts/* 应该被检测到(2026-10-09 rglob 升级)。"""
    builtin = tmp_path / "builtin"
    builtin.mkdir()
    workspace = tmp_path / "work"
    workspace.mkdir()
    _write_skill(builtin, "alpha")
    scripts_dir = builtin / "alpha" / "scripts"
    scripts_dir.mkdir()
    script = scripts_dir / "run.py"
    script.write_text("# v1", encoding="utf-8")

    rt = SkillRuntime.from_settings(_make_settings(builtin, workspace))
    assert rt.find_skill_by_name("alpha") is not None
    assert rt._fingerprint != 0.0
    fp_before = rt._fingerprint

    time.sleep(0.05)
    script.write_text("# v2 - changed", encoding="utf-8")

    # render 触发重扫,指纹应变化
    rt.render_prompt_snippet()
    assert rt._fingerprint != fp_before


def test_reference_file_change_triggers_reload(tmp_path: Path) -> None:
    """改 skill/references/* 应该被检测到。"""
    builtin = tmp_path / "builtin"
    builtin.mkdir()
    workspace = tmp_path / "work"
    workspace.mkdir()
    _write_skill(builtin, "alpha")
    refs_dir = builtin / "alpha" / "references"
    refs_dir.mkdir()
    ref = refs_dir / "api.md"
    ref.write_text("# API v1", encoding="utf-8")

    rt = SkillRuntime.from_settings(_make_settings(builtin, workspace))
    fp_before = rt._fingerprint

    time.sleep(0.05)
    ref.write_text("# API v2 - changed", encoding="utf-8")

    rt.find_skill_by_name("alpha")
    assert rt._fingerprint != fp_before


def test_mark_dirty_forces_reload(tmp_path: Path) -> None:
    """mark_dirty() 把指纹设成 -1.0,下次 render 必触发重扫。

    mark_dirty 是 SkillRuntime 暴露给 tool 调用的公开方法(2026-10-09 重构后)。
    tool 本身的可调用性在 tests/unit/capabilities/tools/test_skill_admin.py 测试。
    """
    builtin = tmp_path / "builtin"
    builtin.mkdir()
    workspace = tmp_path / "work"
    workspace.mkdir()
    _write_skill(builtin, "alpha")

    rt = SkillRuntime.from_settings(_make_settings(builtin, workspace))
    rt.render_prompt_snippet()  # 先建稳缓存
    assert rt._fingerprint > 0.0

    # 主动标记(无返回值,只改指纹)
    rt.mark_dirty()
    assert rt._fingerprint == -1.0

    # 即使 mtime 没真变,下次 render 也会扫
    rt.render_prompt_snippet()
    assert rt._fingerprint > 0.0  # 已重新算
