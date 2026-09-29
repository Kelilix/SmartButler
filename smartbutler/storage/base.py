"""存储后端统一协议。

所有具体后端（SQLite / Qdrant / Redis / YAML 等）必须实现 BaseStorage，
保证上层模块（emotion/memory 等）只依赖接口，不耦合具体实现。

设计要点：
1. 全异步接口，与项目 asyncio-first 原则一致（按文档 §4.6）。
2. KV 模型：key 是 str，value 是 bytes——具体序列化由调用方决定。
   这样记忆、性格、配置等不同数据形态都共用同一套接口。
3. 复杂场景（如向量检索）需要在子类扩展，不在此通用接口中暴露。
"""

from __future__ import annotations

from abc import abstractmethod
from typing import Protocol, runtime_checkable


class StorageError(Exception):
    """存储层错误基类。"""


@runtime_checkable
class BaseStorage(Protocol):
    """存储后端统一接口（Protocol）。

    任何具体后端类只要实现了以下方法，即被认作 BaseStorage；
    不需要显式继承——这是 duck typing + 静态检查的友好折中。
    """

    @abstractmethod
    async def get(self, key: str) -> bytes | None:
        """根据 key 取值；不存在返回 None。"""

    @abstractmethod
    async def set(self, key: str, value: bytes, *, ttl: int | None = None) -> None:
        """写入键值对；ttl（秒）为 None 时持久化。"""

    @abstractmethod
    async def delete(self, key: str) -> bool:
        """删除 key；返回是否真的删除了一条记录。"""

    @abstractmethod
    async def exists(self, key: str) -> bool:
        """判断 key 是否存在。"""

    @abstractmethod
    async def list_keys(self, prefix: str = "") -> list[str]:
        """列出所有以 prefix 开头的 key。"""

    @abstractmethod
    async def close(self) -> None:
        """释放底层资源（连接、文件句柄等）。"""
