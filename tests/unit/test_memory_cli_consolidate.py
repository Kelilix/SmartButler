"""memory_cli.cmd_consolidate 单元测试(Phase 6.3 P1)。

覆盖:
1. argparse 接受 --level / --dry-run
2. dry-run 不写新条目(只读)
3. 真跑有 0 候选时优雅返回 0
"""
from __future__ import annotations

import argparse
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from smartbutler.scripts.memory_cli import cmd_consolidate, main


def _args(
    *,
    level: str = "daily",
    dry_run: bool = True,
    qdrant_path: str = "./data/qdrant",
    archive_path: str = "./data/deleted_archive.db",
    collection: str = "smartbutler_memory",
    vector_size: int = 1024,
    no_llm: bool = True,
) -> argparse.Namespace:
    return argparse.Namespace(
        level=level,
        dry_run=dry_run,
        qdrant_path=qdrant_path,
        qdrant_url=None,
        qdrant_api_key=None,
        collection=collection,
        vector_size=vector_size,
        archive_path=archive_path,
        # 🆕 2026-10-12:测试不调真实 LLM(默认 no_llm=True)
        no_llm=no_llm,
    )


class TestConsolidateSubcommand:
    """consolidate 子命令 argparse 接线。"""

    def test_help_works(self, capsys: pytest.CaptureFixture[str]) -> None:
        """``memory_cli consolidate --help`` 跑通不抛。"""
        with pytest.raises(SystemExit) as exc:
            main(["consolidate", "--help"])
        assert exc.value.code == 0
        captured = capsys.readouterr()
        assert "--level" in captured.out
        assert "--dry-run" in captured.out
        assert "daily" in captured.out
        assert "weekly" in captured.out
        assert "monthly" in captured.out


class TestCmdConsolidateDryRun:
    """cmd_consolidate dry-run 分支(不写 Qdrant,只看统计)。"""

    def test_dry_run_on_empty_returns_zero(
        self,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
    ) -> None:
        """空 Qdrant → 0 候选,优雅返回 0。"""
        # 用临时 Qdrant 目录避免污染
        qp = str(tmp_path / "qdrant")
        ap = str(tmp_path / "archive.db")
        args = _args(
            level="daily",
            dry_run=True,
            qdrant_path=qp,
            archive_path=ap,
        )
        rc = cmd_consolidate(args)
        assert rc == 0
        out = capsys.readouterr().out
        assert "DRY RUN" in out
        assert "candidates_scanned" in out
        # 🆕 2026-10-12:提示语改成"--no-llm / 去掉 dry-run",不再提 /remember
        assert "--no-llm" in out

    def test_dry_run_level_weekly_uses_7d_threshold(
        self,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
    ) -> None:
        """weekly dry-run 用 7 天阈值(即使空库,代码路径要走对)。"""
        qp = str(tmp_path / "qdrant")
        ap = str(tmp_path / "archive.db")
        args = _args(
            level="weekly",
            dry_run=True,
            qdrant_path=qp,
            archive_path=ap,
        )
        rc = cmd_consolidate(args)
        assert rc == 0
        out = capsys.readouterr().out
        assert "DRY RUN" in out
        assert "weekly" in out

    def test_dry_run_level_monthly_uses_30d_threshold(
        self,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
    ) -> None:
        """monthly dry-run 用 30 天阈值。"""
        qp = str(tmp_path / "qdrant")
        ap = str(tmp_path / "archive.db")
        args = _args(
            level="monthly",
            dry_run=True,
            qdrant_path=qp,
            archive_path=ap,
        )
        rc = cmd_consolidate(args)
        assert rc == 0
        out = capsys.readouterr().out
        assert "DRY RUN" in out
        assert "monthly" in out


class TestCmdConsolidateReal:
    """cmd_consolidate 真跑分支(走 Consolidator.consolidate)。"""

    def test_real_run_on_empty_returns_zero(
        self,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
    ) -> None:
        """空 Qdrant + 真跑 → 0 scanned, 0 promoted, 优雅返回 0。"""
        qp = str(tmp_path / "qdrant")
        ap = str(tmp_path / "archive.db")
        args = _args(
            level="daily",
            dry_run=False,
            qdrant_path=qp,
            archive_path=ap,
        )
        rc = cmd_consolidate(args)
        assert rc == 0
        out = capsys.readouterr().out
        assert "Consolidate done" in out
        assert "scanned" in out
        assert "promoted" in out
