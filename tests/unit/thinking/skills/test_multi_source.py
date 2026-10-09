"""多源 Skill 扫描测试(Phase 7+)。

覆盖:
- scan_skills_dirs 多根扫描 + 合并
- builtin > user 冲突策略(user 被忽略 + 告警)
- 不同源同名时 builtin 胜出,user 同名被丢
- 不存在目录静默跳过
- 排序:builtin 先,user 后
- SkillMetadata.source 字段
- parse_skill_md / scan_skills_dir 的 source 参数默认值
- build_default_policy 多 skills_dir 接受
"""
from __future__ import annotations

from pathlib import Path

from smartbutler.thinking.skills import (
    FileOperation,
    PermissionMode,
    build_default_policy,
    parse_skill_md,
    scan_skills_dir,
    scan_skills_dirs,
)

# ---------- helper ----------

def _write_skill(skills_dir: Path, name: str, body: str) -> Path:
    d = skills_dir / name
    d.mkdir(parents=True, exist_ok=True)
    p = d / "SKILL.md"
    p.write_text(body, encoding="utf-8")
    return p


def _body(n: str, d: str) -> str:
    return f"---\nname: {n}\ndescription: {d}\n---\nbody for {n}\n"


# ---------- source 字段默认值 ----------

class TestSourceField:
    def test_default_source_is_builtin(self, tmp_path: Path) -> None:
        """scan_skills_dir 默认 source=builtin(保持向后兼容)。"""
        p = _write_skill(tmp_path, "x", _body("x", "ok"))
        meta = parse_skill_md(p)
        assert meta.source == "builtin"

        out = scan_skills_dir(tmp_path)
        assert out[0].source == "builtin"

    def test_explicit_source_user(self, tmp_path: Path) -> None:
        p = _write_skill(tmp_path, "y", _body("y", "ok"))
        meta = parse_skill_md(p, source="user")
        assert meta.source == "user"


# ---------- scan_skills_dirs 多源 ----------

class TestScanSkillsDirs:
    def test_two_disjoint_sources(self, tmp_path: Path) -> None:
        """两个源完全不同的 skill,都返回,builtin 先 user 后。"""
        builtin = tmp_path / "builtin"
        user = tmp_path / "user"
        _write_skill(builtin, "alpha", _body("alpha", "builtin-only"))
        _write_skill(user, "beta", _body("beta", "user-only"))

        out = scan_skills_dirs(
            [(builtin, "builtin"), (user, "user")],
        )
        assert {m.name for m in out} == {"alpha", "beta"}
        # 排序:builtin 先
        assert out[0].source == "builtin"
        assert out[0].name == "alpha"
        assert out[1].source == "user"
        assert out[1].name == "beta"

    def test_builtin_wins_over_user(self, tmp_path: Path) -> None:
        """同名时 builtin 赢,user 同名被忽略。"""
        builtin = tmp_path / "builtin"
        user = tmp_path / "user"
        _write_skill(builtin, "shared", _body("shared", "builtin version"))
        _write_skill(user, "shared", _body("shared", "user version"))

        out = scan_skills_dirs(
            [(builtin, "builtin"), (user, "user")],
        )
        # 只剩 builtin 那份
        assert len(out) == 1
        assert out[0].source == "builtin"
        assert "builtin version" in out[0].description

    def test_user_only_skills_loaded(self, tmp_path: Path) -> None:
        """user-only skill(builtin 没同名)正常加载。"""
        builtin = tmp_path / "builtin"
        user = tmp_path / "user"
        _write_skill(builtin, "a", _body("a", "A"))
        _write_skill(user, "b", _body("b", "B"))
        _write_skill(user, "c", _body("c", "C"))

        out = scan_skills_dirs(
            [(builtin, "builtin"), (user, "user")],
        )
        names = {m.name for m in out}
        assert names == {"a", "b", "c"}

    def test_nonexistent_user_dir_silent_skip(self, tmp_path: Path) -> None:
        """user 源不存在时静默跳过(不抛错)。"""
        builtin = tmp_path / "builtin"
        _write_skill(builtin, "a", _body("a", "A"))

        out = scan_skills_dirs(
            [(builtin, "builtin"), (tmp_path / "no-such-user-dir", "user")],
        )
        # 只有 builtin
        assert {m.name for m in out} == {"a"}
        assert out[0].source == "builtin"

    def test_builtin_only(self, tmp_path: Path) -> None:
        """只传 builtin 也工作。"""
        builtin = tmp_path / "builtin"
        _write_skill(builtin, "a", _body("a", "A"))

        out = scan_skills_dirs([(builtin, "builtin")])
        assert len(out) == 1
        assert out[0].name == "a"
        assert out[0].source == "builtin"

    def test_empty_roots(self) -> None:
        """空 roots → 空 list。"""
        out = scan_skills_dirs([])
        assert out == []

    def test_skill_ordering_stable(self, tmp_path: Path) -> None:
        """同源多 skill 按名字升序。"""
        builtin = tmp_path / "builtin"
        _write_skill(builtin, "zebra", _body("zebra", "Z"))
        _write_skill(builtin, "apple", _body("apple", "A"))
        _write_skill(builtin, "mango", _body("mango", "M"))

        out = scan_skills_dirs([(builtin, "builtin")])
        names = [m.name for m in out]
        assert names == ["apple", "mango", "zebra"]


# ---------- build_default_policy 多 skills_dirs ----------

class TestBuildDefaultPolicyMultiDirs:
    def test_multiple_skills_dirs_all_allow(self, tmp_path: Path) -> None:
        """多 skills_dir 都生成 ALLOW 规则。"""
        policy = build_default_policy(
            work_dir=tmp_path / "ws",
            skills_dirs=[tmp_path / "builtin", tmp_path / "user"],
        )
        for d in ("builtin", "user"):
            target = (tmp_path / d / "pdf" / "data" / "x.json").resolve()
            mode = policy.evaluate(FileOperation.WRITE, target)
            assert mode == PermissionMode.ALLOW, f"{d} should be ALLOW"

    def test_single_skill_dir_list_one(self, tmp_path: Path) -> None:
        """单元素 list 跟旧版一致。"""
        policy = build_default_policy(
            work_dir=tmp_path / "ws",
            skills_dirs=[tmp_path / "sk"],
        )
        target = (tmp_path / "sk" / "pdf" / "x").resolve()
        assert policy.evaluate(FileOperation.WRITE, target) == PermissionMode.ALLOW
        # 仍 INTERRUPT 兜底
        target2 = (tmp_path / "other" / "x").resolve()
        assert policy.evaluate(FileOperation.READ, target2) == PermissionMode.INTERRUPT

    def test_empty_skills_dirs_works(self, tmp_path: Path) -> None:
        """空 skills_dirs(全关掉)也能跑——只有 workspace ALLOW。"""
        policy = build_default_policy(
            work_dir=tmp_path / "ws",
            skills_dirs=[],
        )
        target = (tmp_path / "ws" / "x").resolve()
        assert policy.evaluate(FileOperation.WRITE, target) == PermissionMode.ALLOW
        target2 = (tmp_path / "skills" / "x").resolve()
        assert policy.evaluate(FileOperation.WRITE, target2) == PermissionMode.INTERRUPT
