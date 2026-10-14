"""SQLite 后端（按文档 §4.3 + README Phase 6.1）。

实现 ``BaseStorage`` 协议，用 aiosqlite 异步包装。用于：
- 短期对话上下文（README Phase 6.1 短期记忆的 3 层存储之一）
- 长期情景记忆（Phase 6.4 之后，SQLite JSON 存对话轨迹）
- 性格状态、Agent 配置索引等 KV 数据

设计要点：
1. **全异步**：BaseStorage 接口是 async，SQLite 同步 API 走 aiosqlite 桥接。
2. **KV 模型**：value 用 BLOB 存 bytes，由调用方负责序列化（json/pickle 自由）。
3. **TTL**：用 expires_at 字段实现；读取时过滤；后台清理由调用方触发（避免守护线程）。
4. **目录自建**：首次启动若父目录不存在则 mkdir(parents=True)。
5. **连接单例**：一个 SQLiteStorage 实例持有一个 aiosqlite 连接；多协程并发安全
   （aiosqlite 内部串行化），够用。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import aiosqlite

from smartbutler.storage.base import BaseStorage, StorageError

_SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (
    key         TEXT PRIMARY KEY,
    value       BLOB NOT NULL,
    expires_at  REAL
);
CREATE INDEX IF NOT EXISTS ix_kv_expires ON kv(expires_at) WHERE expires_at IS NOT NULL;
"""


class SQLiteStorage(BaseStorage):
    """SQLite KV 存储后端。

    典型用法::

        async with SQLiteStorage(Path("data/smartbutler.db")) as store:
            await store.set("user:alice", b'{"name":"Alice"}')
            data = await store.get("user:alice")
    """

    def __init__(self, path: Path) -> None:
        """初始化 SQLite 后端。

        Args:
            path: 数据库文件路径。父目录若不存在会自动创建。
        """
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: aiosqlite.Connection | None = None

    async def _ensure_conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            conn = await aiosqlite.connect(str(self._path))
            await conn.execute("PRAGMA journal_mode=WAL")
            await conn.execute("PRAGMA synchronous=NORMAL")
            await conn.executescript(_SCHEMA)
            await conn.commit()
            self._conn = conn
        return self._conn

    async def get(self, key: str) -> bytes | None:
        """取值，过期返回 None。"""
        conn = await self._ensure_conn()
        async with conn.execute(
            "SELECT value, expires_at FROM kv WHERE key = ?", (key,)
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        value, expires_at = row
        if expires_at is not None and expires_at < time.time():
            # 惰性删除：读到过期 key 顺手清掉，不等后台线程
            await self.delete(key)
            return None
        return value

    async def set(self, key: str, value: bytes, *, ttl: int | None = None) -> None:
        """写入，ttl 秒后过期（None 持久化）。"""
        if not isinstance(value, bytes):
            raise StorageError(f"value must be bytes, got {type(value).__name__}")
        conn = await self._ensure_conn()
        expires_at = (time.time() + ttl) if ttl is not None else None
        await conn.execute(
            """
            INSERT INTO kv(key, value, expires_at) VALUES(?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, expires_at=excluded.expires_at
            """,
            (key, value, expires_at),
        )
        await conn.commit()

    async def delete(self, key: str) -> bool:
        """删除 key，返回是否真删了一条。"""
        conn = await self._ensure_conn()
        async with conn.execute("DELETE FROM kv WHERE key = ?", (key,)) as cur:
            await conn.commit()
            return cur.rowcount > 0

    async def exists(self, key: str) -> bool:
        """判断 key 是否存在且未过期。"""
        return await self.get(key) is not None

    async def list_keys(self, prefix: str = "") -> list[str]:
        """列 key，过期的不返回。"""
        conn = await self._ensure_conn()
        like = f"{prefix}%" if prefix else "%"
        now = time.time()
        async with conn.execute(
            "SELECT key, expires_at FROM kv WHERE key LIKE ?", (like,)
        ) as cur:
            rows = await cur.fetchall()
        result: list[str] = []
        for key, expires_at in rows:
            if expires_at is not None and expires_at < now:
                continue
            result.append(key)
        return result

    async def close(self) -> None:
        """关闭连接。重复调用幂等。"""
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def __aenter__(self) -> SQLiteStorage:
        await self._ensure_conn()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()


__all__ = ["SQLiteStorage"]
