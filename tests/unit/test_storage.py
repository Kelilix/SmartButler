"""storage/base 单元测试。

验证：
- BaseStorage 是 Protocol，能被具体类实现。
- StorageError 能正常抛出与捕获。
- 不依赖任何外部服务（内存 Fake 即可）。
"""

from __future__ import annotations

import pytest

from smartbutler.storage import BaseStorage, StorageError


class InMemoryStorage:
    """BaseStorage 的最小实现（仅用于测试与开发期快速验证）。"""

    def __init__(self) -> None:
        self._data: dict[str, bytes] = {}

    async def get(self, key: str) -> bytes | None:
        return self._data.get(key)

    async def set(self, key: str, value: bytes, *, ttl: int | None = None) -> None:
        self._data[key] = value

    async def delete(self, key: str) -> bool:
        return self._data.pop(key, None) is not None

    async def exists(self, key: str) -> bool:
        return key in self._data

    async def list_keys(self, prefix: str = "") -> list[str]:
        return [k for k in self._data if k.startswith(prefix)]

    async def close(self) -> None:
        self._data.clear()


async def test_in_memory_storage_implements_protocol() -> None:
    """InMemoryStorage 必须被识别为 BaseStorage。"""
    storage: BaseStorage = InMemoryStorage()
    assert isinstance(storage, BaseStorage)


async def test_in_memory_storage_roundtrip() -> None:
    """set / get / exists / delete 完整流程。"""
    storage: BaseStorage = InMemoryStorage()

    assert await storage.get("missing") is None
    assert await storage.exists("k") is False

    await storage.set("k", b"value")
    assert await storage.get("k") == b"value"
    assert await storage.exists("k") is True

    deleted = await storage.delete("k")
    assert deleted is True
    assert await storage.get("k") is None


async def test_in_memory_storage_list_keys_with_prefix() -> None:
    """list_keys 必须支持 prefix 过滤。"""
    storage: BaseStorage = InMemoryStorage()
    await storage.set("memory:user:1", b"a")
    await storage.set("memory:user:2", b"b")
    await storage.set("config:app", b"c")

    keys = await storage.list_keys(prefix="memory:")
    assert set(keys) == {"memory:user:1", "memory:user:2"}


async def test_in_memory_storage_delete_returns_false_when_missing() -> None:
    """删除不存在的 key 必须返回 False，而不是抛错。"""
    storage: BaseStorage = InMemoryStorage()
    assert await storage.delete("nope") is False


def test_storage_error_is_exception() -> None:
    """StorageError 必须是 Exception 子类，可正常抛出与捕获。"""
    with pytest.raises(StorageError):
        raise StorageError("boom")
