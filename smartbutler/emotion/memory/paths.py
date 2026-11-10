"""记忆子系统数据路径解析(单一事实来源)。

⚠️ 为什么要单独抽一个模块:

历史上有两处各写一份路径,结果**运行时和 CLI 指向不同的文件** ——
用户在 CLI 里执行了"全清理",运行时却完全没被清掉。

具体事故(2026-11-09):
    运行时 ``wiring.py``  → ``PROJECT_ROOT/data/memory/short_term.db``
    CLI ``memory_cli.py`` → ``data/short_term.db``          ← 差一层目录!
    运行时 ``task_index.db`` → ``data/memory/task_index.db``
    CLI                   → ``data/task_index.db``          ← 同样错位

于是用户执行 ``clear-all --include-task-index`` 清掉的是两个**空壳库**,
真实数据(4.3MB / 65 checkpoints)纹丝不动,重启后旧对话又被注入 prompt。

现在**运行时和 CLI 都走本模块**,不会再分叉。

路径解析规则(按优先级):
1. 绝对路径 → 原样使用
2. 相对路径且**首段就是 ``data``** → 相对 ``PROJECT_ROOT``
   (兼容 ``data/short_term.db`` 这种老写法,但会正确落到 ``data/memory/``)
3. 其余相对路径 → 相对 ``data_dir``
   (运行时默认 ``PROJECT_ROOT/data/memory``)

设计要点:
- **不硬编码绝对路径** —— 全部基于 ``PROJECT_ROOT`` 推导,项目可以整体搬走
- **CLI 与运行时共用** —— 见 ``wiring.py`` 与 ``scripts/memory_cli.py``
- **保留向后兼容** —— 老的 ``data/xxx.db`` 写法会被重定向到 ``data/memory/xxx.db``
"""

from __future__ import annotations

from pathlib import Path

#: 运行时数据目录名(相对 PROJECT_ROOT)。
#: 与 ``wiring.py`` 的默认 ``data_dir`` 保持一致。
DEFAULT_DATA_DIR_NAME = "memory"

#: 项目根下的数据目录名。
DATA_DIR_NAME = "data"

#: 短期记忆(checkpoints / writes)SQLite 文件名。
SHORT_TERM_DB_NAME = "short_term.db"

#: Task 切分索引 SQLite 文件名。
TASK_INDEX_DB_NAME = "task_index.db"

#: 长期记忆 archived 备份 SQLite 文件名。
ARCHIVE_DB_NAME = "deleted_archive.db"


def default_data_dir(project_root: Path | None = None) -> Path:
    """运行时数据目录(``PROJECT_ROOT/data/memory``)。

    Args:
        project_root: 项目根目录(None → 从 ``config.base`` 取)。

    Returns:
        绝对路径(不保证目录已存在)。
    """
    if project_root is None:
        from smartbutler.config.base import PROJECT_ROOT

        project_root = PROJECT_ROOT
    return project_root / DATA_DIR_NAME / DEFAULT_DATA_DIR_NAME


def resolve_data_path(
    raw: str | Path | None,
    *,
    data_dir: Path | None = None,
    project_root: Path | None = None,
) -> Path:
    """解析任意记忆子路径的最终绝对路径。

    Args:
        raw: 配置/参数里的原始路径。
            - ``None`` / 空串 → 返回 ``data_dir`` 本身
            - 绝对路径 → 原样使用
            - 相对路径且首段是 ``data`` → ``PROJECT_ROOT / raw``
              **但会把 ``data/xxx.db`` 重定向到 ``data/memory/xxx.db``**
              (见下方"向后兼容"说明)
            - 其余相对路径 → ``data_dir / raw``
        data_dir: 运行时数据目录(None → ``PROJECT_ROOT/data/memory``)。
        project_root: 项目根目录(None → 从 ``config.base`` 取)。

    Returns:
        绝对路径。

    向后兼容说明:
        老代码里到处写 ``data/short_term.db``,本意都是"运行时那个库",
        而运行时实际在 ``data/memory/short_term.db``。为了让这两处统一到
        真实位置,``data/`` 开头的**文件**路径会被自动重定向到 ``data/memory/``。

        只有当用户**明确**想指向 ``data/`` 根目录时(比如 ``data/qdrant``
        这种目录),才应该用绝对路径绕过。
    """
    if project_root is None:
        from smartbutler.config.base import PROJECT_ROOT

        project_root = PROJECT_ROOT

    if data_dir is None:
        data_dir = default_data_dir(project_root)

    raw_str = str(raw).strip() if raw else ""
    if not raw_str:
        return data_dir

    path = Path(raw_str)
    if path.is_absolute():
        return path

    parts = path.parts
    # 首段是 "data" → 相对项目根
    if parts and parts[0] == DATA_DIR_NAME:
        rest = Path(*parts[1:]) if len(parts) > 1 else Path()
        # data/xxx.db → data/memory/xxx.db(重定向到真实运行时位置)
        if rest and rest.suffix in {".db", ".sqlite", ".sqlite3"}:
            return data_dir / rest
        # data/<目录>(如 data/qdrant)→ 原样保留在 data/ 下
        return project_root / path

    return data_dir / path


def resolve_short_term_db_path(
    raw: str | Path | None = None,
    *,
    data_dir: Path | None = None,
    project_root: Path | None = None,
) -> Path:
    """短期记忆 SQLite 路径(默认 ``data/memory/short_term.db``)。"""
    if raw is None or not str(raw).strip():
        if data_dir is None and project_root is None:
            return default_data_dir() / SHORT_TERM_DB_NAME
        return (
            data_dir if data_dir is not None else default_data_dir(project_root)
        ) / SHORT_TERM_DB_NAME
    return resolve_data_path(raw, data_dir=data_dir, project_root=project_root)


def resolve_task_index_db_path(
    raw: str | Path | None = None,
    *,
    data_dir: Path | None = None,
    project_root: Path | None = None,
) -> Path:
    """Task 切分索引 SQLite 路径(默认 ``data/memory/task_index.db``)。"""
    if raw is None or not str(raw).strip():
        return (
            data_dir if data_dir is not None else default_data_dir(project_root)
        ) / TASK_INDEX_DB_NAME
    return resolve_data_path(raw, data_dir=data_dir, project_root=project_root)


def resolve_archive_db_path(
    raw: str | Path | None = None,
    *,
    data_dir: Path | None = None,
    project_root: Path | None = None,
) -> Path:
    """长期记忆 archived 备份 SQLite 路径(默认 ``data/memory/deleted_archive.db``)。"""
    if raw is None or not str(raw).strip():
        return (
            data_dir if data_dir is not None else default_data_dir(project_root)
        ) / ARCHIVE_DB_NAME
    return resolve_data_path(raw, data_dir=data_dir, project_root=project_root)


__all__ = [
    "DEFAULT_DATA_DIR_NAME",
    "DATA_DIR_NAME",
    "SHORT_TERM_DB_NAME",
    "TASK_INDEX_DB_NAME",
    "ARCHIVE_DB_NAME",
    "default_data_dir",
    "resolve_data_path",
    "resolve_short_term_db_path",
    "resolve_task_index_db_path",
    "resolve_archive_db_path",
]
