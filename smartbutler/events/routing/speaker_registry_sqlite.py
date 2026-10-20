"""SQLiteSpeakerRegistry — 基于 SQLite 持久化的声纹注册表。

依赖 ``EmbeddingSpeakerRegistry`` 做内存缓存 + 余弦相似度匹配。

存储设计:
- SQLite 表 ``speaker_profiles`` 存声纹档案，应用重启后不丢失。
- embedding 用 pickle 序列化存 BLOB（list[float] → bytes）。
- 启动时从 SQLite 加载全部档案到内存 dict，后续读写走内存，
  变更时同步写 SQLite（write-through）。

Schema::

    CREATE TABLE speaker_profiles (
        user_id      TEXT PRIMARY KEY,
        display_name TEXT NOT NULL,
        embedding    BLOB NOT NULL,       -- pickle(list[float])
        enrolled_at  TEXT NOT NULL,       -- ISO 8601
        sample_count INTEGER DEFAULT 0,
        metadata     TEXT                 -- JSON
    );

Author: SmartButler Team
Date: 2026-10-16
"""

from __future__ import annotations

import json
import pickle
from datetime import datetime
from pathlib import Path
from typing import Any

import aiosqlite

from smartbutler.events.routing.speaker_registry_embedding import (
    EmbeddingSpeakerRegistry,
)
from smartbutler.events.routing.speaker_registry import SpeakerProfile


_SCHEMA = """
CREATE TABLE IF NOT EXISTS speaker_profiles (
    user_id      TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    embedding    BLOB NOT NULL,
    enrolled_at  TEXT NOT NULL,
    sample_count INTEGER DEFAULT 0,
    metadata     TEXT
);
"""


class SQLiteSpeakerRegistry(EmbeddingSpeakerRegistry):
    """SQLite 持久化的声纹注册表。

    Usage::

        from smartbutler.voice.speaker.ecapa import EcapaTdnnEmbedder
        from smartbutler.events.routing.speaker_registry_sqlite import SQLiteSpeakerRegistry

        embedder = EcapaTdnnEmbedder()
        registry = SQLiteSpeakerRegistry(
            embedder=embedder,
            db_path=Path("data/speakers.db"),
        )
        await registry.start()   # 启动:加载 SQLite → 内存 dict

        # 注册家庭成员
        await registry.register(SpeakerProfile(
            user_id="mommy",
            display_name="妈妈",
            embedding=[...],       # 512 维 list[float]
        ))

        # 识别说话人
        match = await registry.identify(embedding)

        await registry.stop()     # 关闭 SQLite 连接

    不变量(继承自 EmbeddingSpeakerRegistry):
    - user_id 全局唯一
    - identify() 永远不抛异常
    """

    def __init__(
        self,
        embedder: Any,
        db_path: Path | str = Path("data/speakers.db"),
        *,
        threshold: float = 0.70,
    ) -> None:
        """
        Args:
            embedder: 声纹 embedding 生成器(EcapaTdnnEmbedder / ResemblyzerEmbedder)。
            db_path: SQLite 数据库路径，父目录不存在会自动创建。
            threshold: 余弦相似度阈值。
        """
        db_path = Path(db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)

        self._db_path = db_path
        self._conn: aiosqlite.Connection | None = None

        # 先初始化内存 dict（不调用 super().__init__，避免加载）
        self._embedder = embedder
        self._threshold = threshold
        self._store: dict[str, SpeakerProfile] = {}

    async def start(self) -> None:
        """启动:初始化 SQLite 连接，从数据库加载已有档案到内存。

        必须在使用前调用。重复调用幂等。
        """
        if self._conn is not None:
            return

        conn = await aiosqlite.connect(str(self._db_path))
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA synchronous=NORMAL")
        await conn.executescript(_SCHEMA)
        await conn.commit()
        self._conn = conn

        # 从 SQLite 加载到内存 dict
        await self._load_all()

    async def stop(self) -> None:
        """关闭 SQLite 连接。重复调用幂等。"""
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def _ensure_conn(self) -> aiosqlite.Connection:
        """确保连接已初始化。"""
        if self._conn is None:
            await self.start()
        assert self._conn is not None
        return self._conn

    async def _load_all(self) -> None:
        """从 SQLite 加载所有档案到内存 dict。"""
        conn = await self._ensure_conn()
        self._store.clear()
        async with conn.execute(
            "SELECT user_id, display_name, embedding, enrolled_at, sample_count, metadata "
            "FROM speaker_profiles"
        ) as cur:
            rows = await cur.fetchall()

        for row in rows:
            user_id, display_name, embedding_blob, enrolled_at, sample_count, metadata_json = row
            embedding = pickle.loads(embedding_blob)  # bytes → list[float]
            metadata = json.loads(metadata_json) if metadata_json else None
            enrolled_at_dt = datetime.fromisoformat(enrolled_at)

            profile = SpeakerProfile(
                user_id=user_id,
                display_name=display_name,
                embedding=embedding,
                enrolled_at=enrolled_at_dt,
                sample_count=sample_count,
                metadata=metadata,
            )
            self._store[user_id] = profile

    async def _save_to_db(self, profile: SpeakerProfile) -> None:
        """把单个档案持久化到 SQLite。"""
        conn = await self._ensure_conn()
        embedding_bytes = pickle.dumps(profile.embedding)
        enrolled_at_str = (
            profile.enrolled_at.isoformat()
            if profile.enrolled_at
            else datetime.now().isoformat()
        )
        metadata_json = json.dumps(profile.metadata) if profile.metadata else None

        await conn.execute(
            """
            INSERT INTO speaker_profiles (user_id, display_name, embedding, enrolled_at, sample_count, metadata)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                display_name=excluded.display_name,
                embedding=excluded.embedding,
                enrolled_at=excluded.enrolled_at,
                sample_count=excluded.sample_count,
                metadata=excluded.metadata
            """,
            (
                profile.user_id,
                profile.display_name,
                embedding_bytes,
                enrolled_at_str,
                profile.sample_count,
                metadata_json,
            ),
        )
        await conn.commit()

    async def _delete_from_db(self, user_id: str) -> None:
        """从 SQLite 删除指定档案。"""
        conn = await self._ensure_conn()
        await conn.execute(
            "DELETE FROM speaker_profiles WHERE user_id = ?", (user_id,)
        )
        await conn.commit()

    # ---- 覆写父类方法，添加 SQLite 持久化 ----

    async def register(self, profile: SpeakerProfile) -> None:
        """注册家庭成员，写入内存 + SQLite。"""
        await super().register(profile)
        await self._save_to_db(profile)

    async def unregister(self, user_id: str) -> bool:
        """注销家庭成员，从内存 + SQLite 删除。"""
        deleted = await super().unregister(user_id)
        if deleted:
            await self._delete_from_db(user_id)
        return deleted

    # ---- 上下文管理器（方便 async with） ----

    async def __aenter__(self) -> "SQLiteSpeakerRegistry":
        await self.start()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.stop()


__all__ = ["SQLiteSpeakerRegistry"]
