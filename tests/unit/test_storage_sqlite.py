"""SQLiteStorage 单元测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from smartbutler.storage import SQLiteStorage, StorageError


@pytest.fixture
async def store(tmp_path: Path) -> SQLiteStorage:
    s = SQLiteStorage(tmp_path / "test.db")
    yield s
    await s.close()


async def test_set_and_get(store: SQLiteStorage) -> None:
    await store.set("k", b"hello")
    assert await store.get("k") == b"hello"


async def test_get_missing_returns_none(store: SQLiteStorage) -> None:
    assert await store.get("nope") is None


async def test_exists(store: SQLiteStorage) -> None:
    await store.set("k", b"v")
    assert await store.exists("k") is True
    assert await store.exists("nope") is False


async def test_delete(store: SQLiteStorage) -> None:
    await store.set("k", b"v")
    assert await store.delete("k") is True
    assert await store.delete("k") is False  # 已删


async def test_list_keys_with_prefix(store: SQLiteStorage) -> None:
    await store.set("user:1", b"a")
    await store.set("user:2", b"b")
    await store.set("config:x", b"c")
    assert set(await store.list_keys(prefix="user:")) == {"user:1", "user:2"}


async def test_ttl_expires(store: SQLiteStorage) -> None:
    """ttl=1 秒的 key 在 1.1 秒后应返回 None。"""
    import asyncio

    await store.set("k", b"v", ttl=1)
    assert await store.get("k") == b"v"
    await asyncio.sleep(1.1)
    assert await store.get("k") is None


async def test_value_must_be_bytes(store: SQLiteStorage) -> None:
    with pytest.raises(StorageError):
        await store.set("k", "string not bytes")  # type: ignore[arg-type]


async def test_overwrite(store: SQLiteStorage) -> None:
    """同 key 二次 set 覆盖。"""
    await store.set("k", b"v1")
    await store.set("k", b"v2")
    assert await store.get("k") == b"v2"


async def test_async_context_manager(tmp_path: Path) -> None:
    """async with 用法。"""
    async with SQLiteStorage(tmp_path / "ctx.db") as s:
        await s.set("k", b"v")
        assert await s.get("k") == b"v"
    # 出 async with 后连接已关,再 get 应重建
    async with SQLiteStorage(tmp_path / "ctx.db") as s:
        assert await s.get("k") == b"v"


async def test_persistence_across_instances(tmp_path: Path) -> None:
    """两个 SQLiteStorage 实例连同一文件,数据应共享。"""
    p = tmp_path / "shared.db"
    async with SQLiteStorage(p) as s1:
        await s1.set("k", b"persistent")
    async with SQLiteStorage(p) as s2:
        assert await s2.get("k") == b"persistent"
