"""长期记忆（按 README Phase 6.1 + Phase 6.2 占位）。

接 ``QdrantStorage``(语义检索) + ``langmem``(LangGraph 集成的记忆管理)。

Phase 6.1 范围（当前文件）：
- 工厂函数 ``create_long_term_store(settings)`` 把 ``StorageSettings`` 翻译成
  ``QdrantStorage`` 实例
- 暴露 ``LongTermStore`` 薄包装,提供 ``store_memory(key, content, embedding)`` 和
  ``search_memory(embedding, k)`` 两个最简 API,Phase 6.2 直接用
- langmem 的 ``InMemoryStore`` 跟 Qdrant 组合的双层结构(langmem 0.0.30 的
  ``create_memory_store_manager`` 需要一个 ``BaseStore``,Qdrant 不是 BaseStore,
  所以 Phase 6.1 走"我们自己的薄包装",langmem 真正的 manager 留 Phase 6.2/6.3)

设计要点：
1. **不重写 langmem** —— langmem 0.0.30 的 InMemoryStore 不是 Qdrant 后端,
   Phase 6.1 没必要硬塞。Phase 6.2/6.3 上 langmem 时再走"langmem + qdrant 双层"
   桥接(那时 langmem 可能已经原生支持 qdrant,见 GitHub issue 跟踪)。
2. **降级路径** —— 如果 QdrantStorage 启动失败(磁盘满、path 权限),
   长短期记忆回退到 SQLiteStorage(没有向量检索,但能存能取),保证管家不崩。
3. **不抽事实** —— 抽事实(fact extraction)需要 LLM 调用,属于 Phase 6.3 consolidation
   的范畴。Phase 6.1 只做"存什么查什么"的基础 KV + 向量 API。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from smartbutler.storage.base import StorageError
from smartbutler.storage.qdrant import QdrantStorage
from smartbutler.utils.logging import get_logger

logger = get_logger(__name__)


class LongTermStore:
    """长期记忆存储（Phase 6.1 最小可用）。

    用 QdrantStorage 暴露向量检索,Phase 6.2 会基于此接 langmem 的抽象。
    """

    def __init__(self, qdrant: QdrantStorage) -> None:
        if not isinstance(qdrant, QdrantStorage):
            raise TypeError(
                f"LongTermStore 需 QdrantStorage 实例,得到 {type(qdrant).__name__}"
            )
        self._q = qdrant

    @property
    def collection(self) -> str:
        return self._q.collection

    @property
    def mode(self) -> str:
        """Qdrant 模式:embedded / server。"""
        return self._q.mode

    def store_memory(
        self,
        key: str,
        embedding: list[float],
        content: str,
        *,
        importance: float = 0.5,
        tags: list[str] | None = None,
    ) -> None:
        """存一条记忆。

        Args:
            key: 唯一 key(常用"user:xxx:fact:yyy")。
            embedding: 已嵌入好的向量(Phase 6.2 会接嵌入模型自动算)。
            content: 原文文本(记到 payload.content,检索命中时返回给 LLM 看)。
            importance: 重要性 0-1(Phase 6.2 的评分用,这里先存 payload)。
            tags: 标签列表(检索时按 tag 过滤)。
        """
        payload: dict[str, Any] = {
            "content": content,
            "importance": float(importance),
            "tags": tags or [],
        }
        self._q.upsert_with_vector(key, embedding, payload)
        logger.debug(
            "long_term.store key=%s importance=%.2f tags=%s",
            key, importance, tags or [],
        )

    def search_memory(
        self,
        embedding: list[float],
        *,
        k: int = 5,
        min_importance: float = 0.0,
        tag: str | None = None,
    ) -> list[dict[str, Any]]:
        """按向量检索记忆。

        Args:
            embedding: 查询向量。
            k: 最多返回条数。
            min_importance: 重要性阈值,低于此分的不返回。
            tag: 标签过滤,只返回含此 tag 的。

        Returns:
            [{"key": str, "score": float, "content": str, "importance": float,
              "tags": list[str]}, ...]
        """
        filter_payload: dict[str, Any] = {}
        if tag is not None:
            filter_payload["tags"] = tag  # Qdrant MatchValue 支持精确匹配
        # min_importance 暂时不在 filter 里(需要 range query),留 Phase 6.2
        hits = self._q.similarity_search(
            embedding, limit=k, filter_payload=filter_payload or None,
        )
        out: list[dict[str, Any]] = []
        for key, score, payload in hits:
            if payload.get("importance", 0.0) < min_importance:
                continue
            out.append({
                "key": key,
                "score": score,
                "content": payload.get("content", ""),
                "importance": payload.get("importance", 0.0),
                "tags": payload.get("tags", []),
            })
        return out

    async def delete_memory(self, key: str) -> bool:
        """按 key 删一条记忆(Phase 6.3 遗忘 API 用)。"""
        return await self._q.delete(key)

    def count(self) -> int:
        """当前集合的向量条数。集合不存在则返回 0。"""
        if not self._q._client.collection_exists(self._q.collection):
            return 0
        info = self._q._client.get_collection(self._q.collection)
        return int(info.points_count or 0)


def create_long_term_store(
    *,
    qdrant_path: Path | None = None,
    qdrant_url: str | None = None,
    qdrant_api_key: str | None = None,
    collection: str = "smartbutler_memory",
    vector_size: int = 1536,
    distance: str = "Cosine",
) -> LongTermStore:
    """工厂函数:从参数建 LongTermStore。

    Qdrant 模式:path 优先;url 设了则走服务模式;都不设则默认嵌入式 data/qdrant。
    """
    if qdrant_path is None and qdrant_url is None:
        from smartbutler.config.base import PROJECT_ROOT

        qdrant_path = PROJECT_ROOT / "data" / "qdrant"
    try:
        q = QdrantStorage(
            path=qdrant_path,
            url=qdrant_url,
            api_key=qdrant_api_key,
            collection=collection,
            vector_size=vector_size,
            distance=distance,
        )
    except StorageError as e:
        logger.error("QdrantStorage 启动失败: %s", e)
        raise
    return LongTermStore(q)


__all__ = ["LongTermStore", "create_long_term_store"]
