"""🆕 Phase 9:记忆图 SQLite 路径解析(单一事实来源)。

⚠️ 为什么要单独抽一个模块:

``graph_db_path`` 配置是**相对路径**时,相对于哪个目录,曾经
在 ``wiring.py``(运行时)和 CLI 里各写一份,结果不一致 —— 运行时落盘
在 ``data/memory/data/graph.db``(多了一层 ``data/``),CLI 却去找
``data/graph.db``,清图命令找不到文件。

现在两处都走 :func:`resolve_graph_db_path`,不会再分叉。

路径解析规则(按优先级):
1. 绝对路径 → 原样使用
2. 相对路径且**首段就是 ``data``** → 相对 ``PROJECT_ROOT``
   (兼容 ``data/graph.db`` 这种写法)
3. 其余相对路径 → 相对 ``data_dir``(运行时默认 ``PROJECT_ROOT/data/memory``)
"""

from __future__ import annotations

from pathlib import Path

# 运行时数据目录(与 wiring.py 的默认 data_dir 一致)
DEFAULT_DATA_DIR_NAME = "memory"


def resolve_graph_db_path(
    raw: str | Path | None,
    *,
    data_dir: Path | None = None,
    project_root: Path | None = None,
) -> Path:
    """解析记忆图 SQLite 的最终路径。

    Args:
        raw: 配置里的原始路径(``None`` → 用默认 ``graph.db``)。
        data_dir: 运行时数据目录(``None`` → ``PROJECT_ROOT/data/memory``)。
        project_root: 项目根目录(``None`` → 从 config.base 取)。

    Returns:
        绝对路径。
    """
    if project_root is None:
        from smartbutler.config.base import PROJECT_ROOT

        project_root = PROJECT_ROOT

    if data_dir is None:
        data_dir = project_root / "data" / DEFAULT_DATA_DIR_NAME

    raw_str = str(raw).strip() if raw else ""
    if not raw_str:
        raw_str = "graph.db"

    path = Path(raw_str)
    if path.is_absolute():
        return path

    # 首段是 "data" → 相对项目根(兼容 "data/graph.db" 写法)
    parts = path.parts
    if parts and parts[0] == "data":
        return project_root / path

    return data_dir / path


__all__ = ["resolve_graph_db_path", "DEFAULT_DATA_DIR_NAME"]
