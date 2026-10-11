"""archived SQLite 备份表(Phase 6.3 P1,见 §2.5.1.16)。

设计要点:
1. **物理文件** —— ``data/deleted_archive.db``,跟 Qdrant / short_term 错开
2. **表结构** —— ``archived_memories`` 单表,跟 Qdrant 主体字段对齐
3. **生命周期** —— Qdrant 软删除 7 天后,vacuum 把这些数据从 Qdrant 搬到 SQLite
4. **恢复路径** —— ``restore()`` 从 SQLite 读原文,调 Embedder re-embed,upsert 回 Qdrant
5. **降级** —— SQLite 写失败 → 记 ERROR,不抛(影响小,30 天后才物理删)
6. **不存向量** —— SQLite 只存 payload(vector 在 Qdrant 主体里,搬走时丢;恢复时 re-embed)

表结构::

    CREATE TABLE archived_memories (
        key TEXT PRIMARY KEY,         -- 跟 Qdrant 一致
        content TEXT NOT NULL,        -- 原文
        importance REAL NOT NULL,      -- 0-1
        level TEXT,                   -- 4 层分片
        tags TEXT,                    -- JSON 数组
        created_at TEXT NOT NULL,
        deleted_at TEXT NOT NULL,     -- 软删除时间
        deleted_reason TEXT NOT NULL, -- ForgetReason
        archived_at TEXT NOT NULL,    -- 搬入 archive 的时间
        extra TEXT                    -- JSON:其他 payload 字段
    )
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, UTC
from pathlib import Path
from typing import Any

from smartbutler.utils.logging import get_logger

logger = get_logger(__name__)


@dataclass
class ArchivedMemory:
    """archived 备份表的一条记录。"""

    key: str
    content: str
    importance: float
    level: str = ""
    tags: list[str] = field(default_factory=list)
    created_at: str = ""
    deleted_at: str = ""
    deleted_reason: str = ""
    archived_at: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "content": self.content,
            "importance": self.importance,
            "level": self.level,
            "tags": list(self.tags),
            "created_at": self.created_at,
            "deleted_at": self.deleted_at,
            "deleted_reason": self.deleted_reason,
            "archived_at": self.archived_at,
            "extra": dict(self.extra),
        }


class ArchiveStore:
    """archived 备份表(SQLite,Phase 6.3 P1)。

    典型用法::

        store = ArchiveStore(Path("data/deleted_archive.db"))
        store.add(ArchivedMemory(key="...", content="...", ...))
        items = store.list_all()
        store.remove("...")  # vacuum 物理删时调
        store.close()
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._path))
        self._conn.row_factory = sqlite3.Row
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        """建表(幂等)。"""
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS archived_memories (
                key TEXT PRIMARY KEY,
                content TEXT NOT NULL,
                importance REAL NOT NULL,
                level TEXT,
                tags TEXT,
                created_at TEXT NOT NULL,
                deleted_at TEXT NOT NULL,
                deleted_reason TEXT NOT NULL,
                archived_at TEXT NOT NULL,
                extra TEXT
            )
            """
        )
        self._conn.commit()

    def add(self, item: ArchivedMemory) -> None:
        """加一条 archived 记录(覆盖同 key 的旧条)。"""
        if not item.archived_at:
            item.archived_at = datetime.now(UTC).isoformat()
        try:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO archived_memories (
                    key, content, importance, level, tags,
                    created_at, deleted_at, deleted_reason, archived_at, extra
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item.key,
                    item.content,
                    float(item.importance),
                    item.level,
                    json.dumps(item.tags, ensure_ascii=False),
                    item.created_at,
                    item.deleted_at,
                    item.deleted_reason,
                    item.archived_at,
                    json.dumps(item.extra, ensure_ascii=False),
                ),
            )
            self._conn.commit()
        except sqlite3.Error as e:
            logger.error("archive_store.add_failed key=%s err=%s", item.key, e)
            raise

    def get(self, key: str) -> ArchivedMemory | None:
        """读一条 archived 记录。"""
        cur = self._conn.execute(
            "SELECT * FROM archived_memories WHERE key = ?", (key,)
        )
        row = cur.fetchone()
        if row is None:
            return None
        return self._row_to_item(row)

    def remove(self, key: str) -> bool:
        """删一条 archived 记录(vacuum 物理删时调)。"""
        cur = self._conn.execute(
            "DELETE FROM archived_memories WHERE key = ?", (key,)
        )
        self._conn.commit()
        return cur.rowcount > 0

    def list_all(
        self, *, older_than_days: int = 0, limit: int = 10_000
    ) -> list[ArchivedMemory]:
        """列所有 archived 记录。

        Args:
            older_than_days: 只列 archived_at 早于 now - N 天的(用于 vacuum 二次清理)。
            limit: 上限。
        """
        if older_than_days > 0:
            from datetime import timedelta
            cutoff = (datetime.now(UTC) - timedelta(days=older_than_days)).isoformat()
            cur = self._conn.execute(
                "SELECT * FROM archived_memories "
                "WHERE archived_at < ? ORDER BY archived_at LIMIT ?",
                (cutoff, limit),
            )
        else:
            cur = self._conn.execute(
                "SELECT * FROM archived_memories ORDER BY archived_at LIMIT ?",
                (limit,),
            )
        return [self._row_to_item(row) for row in cur.fetchall()]

    def count(self) -> int:
        """archived 总条数。"""
        cur = self._conn.execute("SELECT COUNT(*) AS c FROM archived_memories")
        row = cur.fetchone()
        return int(row["c"]) if row else 0

    def size_bytes(self) -> int:
        """SQLite 文件大小(字节)。"""
        if not self._path.exists():
            return 0
        return self._path.stat().st_size

    def vacuum(self) -> None:
        """物理清理 SQLite(删 archived_at 超过 retention 的记录后调用)。"""
        try:
            self._conn.execute("VACUUM")
        except sqlite3.Error as e:
            logger.warning("archive_store.vacuum_failed err=%s", e)

    def close(self) -> None:
        """关连接。"""
        try:
            self._conn.close()
        except Exception:  # pragma: no cover
            pass

    @staticmethod
    def _row_to_item(row: sqlite3.Row) -> ArchivedMemory:
        return ArchivedMemory(
            key=row["key"],
            content=row["content"],
            importance=float(row["importance"]),
            level=row["level"] or "",
            tags=json.loads(row["tags"]) if row["tags"] else [],
            created_at=row["created_at"],
            deleted_at=row["deleted_at"],
            deleted_reason=row["deleted_reason"],
            archived_at=row["archived_at"],
            extra=json.loads(row["extra"]) if row["extra"] else {},
        )


__all__ = ["ArchiveStore", "ArchivedMemory"]
