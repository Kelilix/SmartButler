"""长期记忆(按 README Phase 6.1 + Phase 6.2 + Phase 6.3 扩展)。

接 ``QdrantStorage``(语义检索) + ``langmem``(LangGraph 集成的记忆管理)。

Phase 6.1 范围:
- 工厂函数 ``create_long_term_store(settings)`` 把 ``StorageSettings`` 翻译成
  ``QdrantStorage`` 实例
- 暴露 ``LongTermStore`` 薄包装,提供 ``store_memory(key, content, embedding)`` 和
  ``search_memory(embedding, k)`` 两个最简 API,Phase 6.2 直接用

Phase 6.3 扩展(本文件新增):
- payload 字段扩展:``level``(4 层分片)/ ``deleted``(软删除)/ ``created_at``(时间戳)/
  ``source_keys``(升格溯源)
- 软删除 + 恢复 API:``soft_delete`` / ``restore_deleted`` / ``get_payload`` / ``list_keys_by_level``
- 软删除过滤自动加到 search(``must_not deleted=true``)

设计要点:
1. 不重写 langmem —— langmem 0.0.30 的 InMemoryStore 不是 Qdrant 后端,
   Phase 6.3 也没必要硬塞。langmem 0.1+ 等官方 qdrant adapter 再说。
2. 降级路径 —— 如果 QdrantStorage 启动失败(磁盘满、path 权限),
   长短期记忆回退到 SQLiteStorage(没有向量检索,但能存能取),保证管家不崩。
3. payload 字段全部可选 —— P0 已有调用方(retriever.remember)不传新字段,行为零变化。
"""

from __future__ import annotations

from datetime import datetime, UTC
from pathlib import Path
from typing import Any

from smartbutler.storage.base import StorageError
from smartbutler.storage.qdrant import QdrantStorage
from smartbutler.utils.logging import get_logger

logger = get_logger(__name__)


# 6.3 payload 字段常量
PAYLOAD_KEY = "kv_key"          # 已有
PAYLOAD_CONTENT = "content"     # 已有
PAYLOAD_IMPORTANCE = "importance"  # 已有
PAYLOAD_TAGS = "tags"           # 已有(数组)
# 6.3 新增
PAYLOAD_LEVEL = "level"                    # 单值:MemoryLevel.value
PAYLOAD_DELETED = "deleted"                # bool
PAYLOAD_DELETED_AT = "deleted_at"          # iso 字符串
PAYLOAD_DELETED_REASON = "deleted_reason"  # ForgetReason.value
PAYLOAD_CREATED_AT = "created_at"          # iso 字符串
PAYLOAD_SOURCE_KEYS = "source_keys"        # 升格溯源,list[str]
# 🆕 7.4 多模态扩展位(7.9 启动时由 EmbeddingGemma2Embedder 补 image_embedding)
PAYLOAD_IMAGE_REF = "image_ref"            # 7.8 写入(可选),7.9 多模态 embedding 用
PAYLOAD_AUDIO_REF = "audio_ref"            # 7.6 写入(可选),7.9 多模态 embedding 用
PAYLOAD_IMAGE_EMBEDDING = "image_embedding"  # list[float] 序列化(7.9 启动时补,7.4 留位)
PAYLOAD_EMBEDDING_MODEL = "embedding_model"  # 记录哪个模型生成的 embedding(如 "embeddinggemma-2")
# 7.4 兼容性:旧 payload 没有这 4 字段也 OK,业务层读不到就当 None


class LongTermStore:
    """长期记忆存储(Phase 6.1 最小可用 + 6.3 扩展)。"""

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

    @property
    def qdrant(self) -> QdrantStorage:
        """暴露底层 QdrantStorage(供 6.3 vacuum 物理删除用)。"""
        return self._q

    def store_memory(
        self,
        key: str,
        embedding: list[float],
        content: str,
        *,
        importance: float = 0.5,
        tags: list[str] | None = None,
        level: str | None = None,
        source_keys: list[str] | None = None,
        created_at: datetime | None = None,
        # 🆕 7.4 多模态扩展位(Qdrant payload schema-less,默认 None,7.9 启动时由
        # EmbeddingGemma2Embedder 补 image_embedding + embedding_model)。
        # - image_ref / audio_ref:7.6/7.8 写入原始文件路径(可选,不强约束)
        # - image_embedding:bytes 或 list[float](7.4 留位,7.9 启动时补)
        # - embedding_model:str,记录哪个模型生成的 image_embedding
        image_ref: str | None = None,
        audio_ref: str | None = None,
        image_embedding: bytes | list[float] | None = None,
        embedding_model: str | None = None,
    ) -> None:
        """存一条记忆(P0 + 6.3 字段 + 7.4 多模态扩展位)。

        Args:
            key: 唯一 key(常用"user:xxx:fact:yyy")。
            embedding: 已嵌入好的向量(Phase 6.2 会接嵌入模型自动算)。
            content: 原文文本(记到 payload.content,检索命中时返回给 LLM 看)。
            importance: 重要性 0-1(Phase 6.2 的评分用,这里先存 payload)。
            tags: 标签列表(检索时按 tag 过滤)。
            level: 6.3 新增,4 层分片之一(daily/weekly/monthly/permanent),None = 不打 level tag。
            source_keys: 6.3 新增,升格溯源(本 fact 由哪些源 fact 升格而来)。
            created_at: 6.3 新增,创建时间(默认 = now UTC)。
            image_ref: 🆕 7.4 原始图片文件路径(7.8 CameraAdapter 传入,7.9 多模态 embedding 用)。
            audio_ref: 🆕 7.4 原始音频文件路径(7.6 VoiceAdapter 传入,7.9 多模态 embedding 用)。
            image_embedding: 🆕 7.4 多模态向量(7.9 启动时由 EmbeddingGemma2Embedder 补;
                7.4-7.8 传 None,Qdrant payload 不存这个字段)。
            embedding_model: 🆕 7.4 embedding 模型名(如 "embeddinggemma-2",7.9 启动时写)。
        """
        ts = (created_at or datetime.now(UTC)).isoformat()
        payload: dict[str, Any] = {
            "content": content,
            "importance": float(importance),
            "tags": list(tags or []),
            "created_at": ts,
        }
        if level is not None:
            payload[PAYLOAD_LEVEL] = level
        if source_keys is not None:
            payload[PAYLOAD_SOURCE_KEYS] = list(source_keys)
        # 🆕 7.4 多模态字段(Qdrant payload schema-less,None = 不写字段)
        if image_ref is not None:
            payload[PAYLOAD_IMAGE_REF] = str(image_ref)
        if audio_ref is not None:
            payload[PAYLOAD_AUDIO_REF] = str(audio_ref)
        if image_embedding is not None:
            # list[float] 序列化(list → JSON,Qdrant 会自动处理)
            payload[PAYLOAD_IMAGE_EMBEDDING] = list(image_embedding)
        if embedding_model is not None:
            payload[PAYLOAD_EMBEDDING_MODEL] = str(embedding_model)
        self._q.upsert_with_vector(key, embedding, payload)
        logger.debug(
            "long_term.store key=%s importance=%.2f level=%s tags=%s"
            " image_ref=%s audio_ref=%s",
            key, importance, level, tags or [], image_ref, audio_ref,
        )

    def search_memory(
        self,
        embedding: list[float],
        *,
        k: int = 5,
        min_importance: float = 0.0,
        tag: str | None = None,
        include_deleted: bool = False,
    ) -> list[dict[str, Any]]:
        """按向量检索记忆(P0 + 6.3 软删除过滤)。

        Args:
            embedding: 查询向量。
            k: 最多返回条数。
            min_importance: 重要性阈值,低于此分的不返回。
            tag: 标签过滤,只返回含此 tag 的。
            include_deleted: 6.3 新增,True = 含软删除条目(默认 False)。

        Returns:
            [{"key": str, "score": float, "content": str, "importance": float,
              "tags": list[str], "level": str?, "deleted": bool?,
              "created_at": str?, "source_keys": list[str]?}, ...]
        """
        if include_deleted:
            hits = self._q.similarity_search_with_filters(
                embedding,
                k=k,
                min_importance=min_importance,
                tag=tag,
            )
        else:
            # 6.3 软删除:用 Qdrant 端 must_not 过滤 deleted=true
            hits = self._q.similarity_search_with_filters_and_soft_delete(
                embedding,
                k=k,
                min_importance=min_importance,
                tag=tag,
            )
        out: list[dict[str, Any]] = []
        for key, score, payload in hits:
            row: dict[str, Any] = {
                "key": key,
                "score": score,
                "content": payload.get("content", ""),
                "importance": payload.get("importance", 0.0),
                "tags": payload.get("tags", []),
                "created_at": payload.get("created_at", ""),
            }
            if PAYLOAD_LEVEL in payload:
                row["level"] = payload[PAYLOAD_LEVEL]
            if PAYLOAD_DELETED in payload:
                row["deleted"] = bool(payload[PAYLOAD_DELETED])
            if PAYLOAD_DELETED_AT in payload:
                row["deleted_at"] = payload[PAYLOAD_DELETED_AT]
            if PAYLOAD_DELETED_REASON in payload:
                row["deleted_reason"] = payload[PAYLOAD_DELETED_REASON]
            if PAYLOAD_SOURCE_KEYS in payload:
                row["source_keys"] = payload[PAYLOAD_SOURCE_KEYS]
            # 🆕 7.4 多模态字段透传(Qdrant payload schema-less,没有这些字段时
            # 不在 row 里出现,业务层 .get() 兜底 → None)
            if PAYLOAD_IMAGE_REF in payload:
                row["image_ref"] = payload[PAYLOAD_IMAGE_REF]
            if PAYLOAD_AUDIO_REF in payload:
                row["audio_ref"] = payload[PAYLOAD_AUDIO_REF]
            if PAYLOAD_IMAGE_EMBEDDING in payload:
                row["image_embedding"] = payload[PAYLOAD_IMAGE_EMBEDDING]
            if PAYLOAD_EMBEDDING_MODEL in payload:
                row["embedding_model"] = payload[PAYLOAD_EMBEDDING_MODEL]
            out.append(row)
        return out

    async def delete_memory(self, key: str) -> bool:
        """按 key 物理删一条记忆(6.3 vacuum 用)。"""
        return await self._q.delete(key)

    async def soft_delete(
        self,
        key: str,
        reason: str,
        *,
        deleted_at: datetime | None = None,
    ) -> bool:
        """软删除一条记忆(6.3 5 类遗忘的底层 API)。

        行为:
        - 读现有 payload(用 Qdrant scroll)
        - 写回 payload,加 deleted=true + deleted_at + deleted_reason
        - 不删向量,recall 通过 must_not 过滤
        - 找不到 key 返回 False

        Args:
            key: 记忆 key。
            reason: 遗忘原因(ForgetReason.value)。
            deleted_at: 软删除时间(默认 now UTC)。

        Returns:
            True = 成功软删除,False = key 不存在。
        """
        ts = (deleted_at or datetime.now(UTC)).isoformat()
        existing = self.get_payload(key)
        if existing is None:
            return False
        if existing.get(PAYLOAD_DELETED):
            logger.debug("long_term.soft_delete already_deleted key=%s", key)
            return True
        vector = self._get_vector_by_key(key)
        if vector is None:
            return False
        existing[PAYLOAD_DELETED] = True
        existing[PAYLOAD_DELETED_AT] = ts
        existing[PAYLOAD_DELETED_REASON] = reason
        self._q.upsert_with_vector(key, vector, existing)
        logger.info("long_term.soft_delete key=%s reason=%s", key, reason)
        return True

    async def restore_deleted(self, key: str) -> bool:
        """恢复软删除的记忆(6.3 restore API)。

        Args:
            key: 记忆 key。

        Returns:
            True = 成功恢复,False = key 不存在或未软删。
        """
        existing = self.get_payload(key)
        if existing is None:
            return False
        if not existing.get(PAYLOAD_DELETED):
            return False
        vector = self._get_vector_by_key(key)
        if vector is None:
            return False
        existing.pop(PAYLOAD_DELETED, None)
        existing.pop(PAYLOAD_DELETED_AT, None)
        existing.pop(PAYLOAD_DELETED_REASON, None)
        self._q.upsert_with_vector(key, vector, existing)
        logger.info("long_term.restore_deleted key=%s", key)
        return True

    def restore_deleted_sync(self, key: str) -> bool:
        """恢复软删除的同步版本(6.3 vacuum 在已 running event loop 里用)。

        行为同 :meth:`restore_deleted`,但用 sync helper 跑异步。

        Args:
            key: 记忆 key。

        Returns:
            True = 成功恢复,False = key 不存在或未软删。
        """
        import asyncio
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # 同步路径在已 running loop 里跑不了 → 用 _run_in_executor 走 thread
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                    future = ex.submit(asyncio.run, self.restore_deleted(key))
                    return future.result(timeout=10)
            return loop.run_until_complete(self.restore_deleted(key))
        except RuntimeError:
            return asyncio.run(self.restore_deleted(key))

    def qdrant_size_bytes(self) -> int:
        """Qdrant 数据大小(字节,6.3 vacuum_stats 用)。"""
        return self._q.size_bytes()

    def get_payload(self, key: str) -> dict[str, Any] | None:
        """读 key 对应的 payload(不返向量)。key 不存在返 None。

        兜底:collection 尚未创建(嵌入式 Qdrant,无任何 store_memory 调用过)→ 返 None。
        """
        if not self._q._client.collection_exists(self._q.collection):
            return None
        points, _ = self._q._client.scroll(
            collection_name=self._q.collection,
            scroll_filter=self._q._payload_filter_for_key(key),
            limit=1,
            with_payload=True,
            with_vectors=False,
        )
        if not points:
            return None
        return dict(points[0].payload or {})

    def _get_vector_by_key(self, key: str) -> list[float] | None:
        """读 key 对应的 vector(内部用,供 soft_delete / restore 走 upsert 拿原向量)。"""
        if not self._q._client.collection_exists(self._q.collection):
            return None
        points, _ = self._q._client.scroll(
            collection_name=self._q.collection,
            scroll_filter=self._q._payload_filter_for_key(key),
            limit=1,
            with_payload=False,
            with_vectors=True,
        )
        if not points:
            return None
        return list(points[0].vector) if points[0].vector is not None else None

    def list_keys_by_level(
        self,
        level: str | None = None,
        *,
        include_deleted: bool = False,
        limit: int = 10_000,
    ) -> list[dict[str, Any]]:
        """列某 level 的所有记忆(运维/审计用)。

        Args:
            level: "daily"/"weekly"/"monthly"/"permanent",None = 全部。
            include_deleted: True = 含软删除条目(配合 list_deleted 用)。
            limit: 最多返多少条(Qdrant scroll 默认 10000)。

        Returns:
            [{"key": ..., "created_at": ..., "deleted": ..., "importance": ..., "level": ..., "content": ...}, ...]
        """
        from smartbutler.storage.qdrant import FieldCondition, Filter, MatchValue
        if not self._q._client.collection_exists(self._q.collection):
            return []
        must: list[FieldCondition] = []
        if level is not None:
            must.append(FieldCondition(key=PAYLOAD_LEVEL, match=MatchValue(value=level)))
        scroll_filter = Filter(must=must) if must else None
        points, _ = self._q._client.scroll(
            collection_name=self._q.collection,
            scroll_filter=scroll_filter,
            limit=limit,
            with_payload=True,
            with_vectors=False,
        )
        out: list[dict[str, Any]] = []
        for p in points:
            pl = p.payload or {}
            if not include_deleted and pl.get(PAYLOAD_DELETED):
                continue
            out.append({
                "key": pl.get(PAYLOAD_KEY, str(p.id)),
                "importance": pl.get(PAYLOAD_IMPORTANCE, 0.0),
                "created_at": pl.get(PAYLOAD_CREATED_AT, ""),
                "deleted": bool(pl.get(PAYLOAD_DELETED, False)),
                "level": pl.get(PAYLOAD_LEVEL, ""),
                "content": pl.get(PAYLOAD_CONTENT, ""),
            })
        return out

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
    vector_size: int = 1024,
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


__all__ = [
    "LongTermStore",
    "create_long_term_store",
    # 6.3 payload 字段常量(供 forgetting / consolidation 复用)
    "PAYLOAD_LEVEL",
    "PAYLOAD_DELETED",
    "PAYLOAD_DELETED_AT",
    "PAYLOAD_DELETED_REASON",
    "PAYLOAD_CREATED_AT",
    "PAYLOAD_SOURCE_KEYS",
    # 🆕 7.4 多模态扩展位(7.9 启动时由 EmbeddingGemma2Embedder 复用)
    "PAYLOAD_IMAGE_REF",
    "PAYLOAD_AUDIO_REF",
    "PAYLOAD_IMAGE_EMBEDDING",
    "PAYLOAD_EMBEDDING_MODEL",
]
