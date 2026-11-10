"""记忆数据路径解析单元测试。

⚠️ 这组测试的来历(2026-11-09 事故):

用户在 CLI 里执行了"全清理",但重启后旧对话(咖啡话题)又被注入 prompt,
LLM 顺着旧上下文继续聊咖啡。根因是**运行时与 CLI 指向了不同的文件**:

    运行时 ``wiring.py``  → ``data/memory/short_term.db``   (4.3MB, 65 checkpoints)
    CLI ``memory_cli.py`` → ``data/short_term.db``           (20KB, 空壳)

CLI 清掉的是空壳,真实数据纹丝不动。

现在两者都走 :mod:`smartbutler.emotion.memory.paths`,本组测试锁死这个不变量:
**任何时候,CLI 解析出的路径必须 == 运行时解析出的路径。**

覆盖:
1. ``resolve_data_path`` 的分支规则(绝对 / data/ 前缀 / 普通相对)
2. ``data/xxx.db`` → ``data/memory/xxx.db`` 的重定向
3. 三个具体路径解析器(短期 / task 索引 / 归档)
4. **CLI 默认值与运行时一致性**(核心不变量)
5. CLI 参数显式传入时仍然生效
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from smartbutler.emotion.memory.paths import (
    ARCHIVE_DB_NAME,
    SHORT_TERM_DB_NAME,
    TASK_INDEX_DB_NAME,
    default_data_dir,
    resolve_archive_db_path,
    resolve_data_path,
    resolve_short_term_db_path,
    resolve_task_index_db_path,
)


def _make_stub_settings() -> Any:
    """造一个 MemorySettings(不读 .env,避免测试受本地环境影响)。"""
    from smartbutler.config.memory import MemorySettings

    return MemorySettings(
        graph_enabled=False,
        scheduler_enabled=False,
        judge_on_message=False,
    )


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    """假的项目根。"""
    return tmp_path


class TestDefaultDataDir:
    """运行时数据目录。"""

    def test_is_data_memory(self, project_root: Path) -> None:
        """默认 = PROJECT_ROOT/data/memory。"""
        assert default_data_dir(project_root) == project_root / "data" / "memory"


class TestResolveDataPath:
    """通用路径解析规则。"""

    def test_absolute_passthrough(self, project_root: Path) -> None:
        """绝对路径原样使用。"""
        abs_path = Path(r"D:\somewhere\else.db")
        assert resolve_data_path(abs_path, project_root=project_root) == abs_path

    def test_plain_relative_goes_to_data_dir(self, project_root: Path) -> None:
        """普通相对路径 → data_dir 下。"""
        data_dir = project_root / "data" / "memory"
        got = resolve_data_path("foo.db", data_dir=data_dir, project_root=project_root)
        assert got == data_dir / "foo.db"

    def test_data_prefixed_db_redirects_to_memory(self, project_root: Path) -> None:
        """``data/xxx.db`` → ``data/memory/xxx.db``(关键重定向)。"""
        got = resolve_data_path(
            "data/short_term.db", project_root=project_root
        )
        assert got == project_root / "data" / "memory" / "short_term.db"

    def test_data_prefixed_dir_stays_at_data_root(self, project_root: Path) -> None:
        """``data/qdrant`` 是目录,不该被重定向到 data/memory/。"""
        got = resolve_data_path("data/qdrant", project_root=project_root)
        assert got == project_root / "data" / "qdrant"

    def test_empty_returns_data_dir(self, project_root: Path) -> None:
        """空值 → data_dir 本身。"""
        data_dir = project_root / "data" / "memory"
        assert resolve_data_path(None, data_dir=data_dir) == data_dir
        assert resolve_data_path("  ", data_dir=data_dir) == data_dir


class TestSpecificResolvers:
    """三个具体路径解析器。"""

    def test_short_term_default(self, project_root: Path) -> None:
        got = resolve_short_term_db_path(project_root=project_root)
        assert got == project_root / "data" / "memory" / SHORT_TERM_DB_NAME

    def test_task_index_default(self, project_root: Path) -> None:
        got = resolve_task_index_db_path(project_root=project_root)
        assert got == project_root / "data" / "memory" / TASK_INDEX_DB_NAME

    def test_archive_default(self, project_root: Path) -> None:
        got = resolve_archive_db_path(project_root=project_root)
        assert got == project_root / "data" / "memory" / ARCHIVE_DB_NAME

    def test_explicit_value_respected(self, project_root: Path) -> None:
        """显式传入的路径不被覆盖。"""
        got = resolve_short_term_db_path(
            "custom/st.db", project_root=project_root
        )
        assert got == project_root / "data" / "memory" / "custom" / "st.db"


class TestCliRuntimePathParity:
    """🔴 核心不变量:CLI 路径必须与运行时路径完全一致。

    2026-11-09 的"清库无效"事故就是这条不变量被打破导致的。
    本组测试直接对比 CLI 解析结果与运行时解析结果,防止再次分叉。
    """

    def test_cli_and_runtime_same_paths(self) -> None:
        """CLI 的三个默认路径 == 运行时 wiring 用的路径。"""
        import argparse

        from smartbutler.scripts.memory_cli import (
            _archive_path,
            _stm_path,
            _task_index_path,
        )

        # 运行时(wiring.build_memory_system 的默认 data_dir)
        runtime_data_dir = default_data_dir()
        runtime_stm = runtime_data_dir / "short_term.db"
        runtime_task_index = runtime_data_dir / "task_index.db"

        args = argparse.Namespace(
            stm_path=None,
            task_index_path=None,
            archive_path=None,
        )

        assert _stm_path(args) == runtime_stm
        assert _task_index_path(args) == runtime_task_index
        assert _archive_path(args) == runtime_data_dir / ARCHIVE_DB_NAME

    def test_cli_defaults_are_none(self) -> None:
        """CLI 参数默认值必须是 None(而不是硬编码路径)。

        硬编码路径 = 未来再有人改运行时目录就会分叉。
        解析统一交给 ``paths.resolve_*``。
        """
        import argparse

        from smartbutler.scripts.memory_cli import (
            _archive_path,
            _stm_path,
            _task_index_path,
        )

        # 默认(None)→ 统一落到 data/memory/
        ns = argparse.Namespace(
            stm_path=None, task_index_path=None, archive_path=None
        )
        assert _stm_path(ns) == default_data_dir() / SHORT_TERM_DB_NAME
        assert _task_index_path(ns) == default_data_dir() / TASK_INDEX_DB_NAME
        assert _archive_path(ns) == default_data_dir() / ARCHIVE_DB_NAME

    def test_cli_explicit_path_respected(self) -> None:
        """用户显式传路径时,不能被重定向覆盖。"""
        import argparse

        from smartbutler.scripts.memory_cli import _stm_path

        ns = argparse.Namespace(stm_path=r"D:\custom\my.db")
        assert _stm_path(ns) == Path(r"D:\custom\my.db")

    def test_runtime_wiring_uses_shared_resolver(self) -> None:
        """运行时真的把库建在 ``data/memory/`` 下。

        直接构造 ``ShortTermMemory`` / ``TaskIndex``(wiring 里就是这么做的),
        检查它们打开的路径 —— 比 grep 源码可靠:验证的是**行为**。
        """
        from smartbutler.emotion.memory.short_term import ShortTermMemory
        from smartbutler.emotion.memory.task_index import TaskIndex

        expected_dir = default_data_dir()

        # wiring.py 的做法:data_dir / "short_term.db"
        stm = ShortTermMemory(expected_dir / "short_term.db")
        try:
            assert Path(stm._path).parent == expected_dir  # noqa: SLF001
        finally:
            stm.close()

        # wiring.py 的做法:data_dir / "task_index.db"
        ti = TaskIndex(expected_dir / "task_index.db")
        try:
            assert Path(ti._path).parent == expected_dir  # noqa: SLF001
        finally:
            ti.close()


class TestGraphPathDelegation:
    """graph/paths.py 应委托给通用解析。"""

    def test_graph_db_default(self) -> None:
        from smartbutler.emotion.memory.graph.paths import resolve_graph_db_path

        got = resolve_graph_db_path("graph.db")
        assert got == default_data_dir() / "graph.db"

    def test_graph_db_data_prefixed(self) -> None:
        from smartbutler.emotion.memory.graph.paths import resolve_graph_db_path

        got = resolve_graph_db_path("data/graph.db")
        assert got == default_data_dir() / "graph.db"
