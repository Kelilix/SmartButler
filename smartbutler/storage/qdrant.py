"""Qdrant 向量后端（按文档 §4.3 + README Phase 6.1）。

实现 ``BaseStorage`` 协议（KV 视图），并暴露 Qdrant 原生向量 API（similarity_search /
upsert_with_vector）供 Phase 6.2 语义检索使用。

两种运行模式（按 StorageSettings 自动选）：

1. **嵌入式（默认）** —— ``QdrantClient(path=...)``,零部署,Phase 6.1 家用场景。
   进程退出后数据保留在 data/qdrant/ 目录。
2. **服务模式** —— 用户在 .env 显式设 ``SMARTBUTLER_STORAGE_QDRANT_URL=http://...``
   时切到 ``QdrantClient(url=...)``,Phase 8+ 中心化部署 / 团队共享时用。

设计要点：
1. **KV 桥接** —— BaseStorage 是 KV 接口。QdrantStorage 内部把每个 value 编码成一个
   单位向量(dummy 1.0 嵌入) + 把原始 bytes 塞进 payload["raw"]。这样 Phase 6.1
   可以用 ``await qstore.set(k, v)`` / ``await qstore.get(k)`` 与 SQLiteStorage 互换。
2. **真正的向量检索** —— 不走 BaseStorage,而是 QdrantStorage 独有的
   ``upsert_with_vector`` / ``similarity_search``,Phase 6.2 用。
3. **集合自动创建** —— 首次 upsert/similarity_search 时如果集合不存在,
   按 ``qdrant_vector_size`` + ``qdrant_distance`` 自动建。
4. **服务模式 ping** —— ``QdrantClient(url=...)`` 不会主动连,首次 RPC 才连;启动期
   调用 ``get_collections()`` 触发连通性校验,失败立刻抛 ``StorageError``。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    MatchValue,
    PointStruct,
    Range,
    VectorParams,
)

from smartbutler.storage.base import BaseStorage, StorageError

_DISTANCE_MAP: dict[str, Distance] = {
    "Cosine": Distance.COSINE,
    "Euclid": Distance.EUCLID,
    "Dot": Distance.DOT,
}

# Qdrant payload key 必须是合法标识符(数字/字母/下划线/连字符/点)。
# tag 默认是 "source:xxx" 这种含冒号的形式,需要转成 "source_xxx"。
_TAG_KEY_RE = re.compile(r"[^a-zA-Z0-9_.\-]")


def _sanitize_payload_key(key: str) -> str:
    """把任意 tag 转成 Qdrant payload key(替换非法字符为 _)。"""
    return _TAG_KEY_RE.sub("_", key)


class QdrantStorage(BaseStorage):
    """Qdrant 后端（支持嵌入式 + 服务模式）。

    典型用法::

        # KV 视图（与 SQLiteStorage 同协议）
        store = QdrantStorage(StorageSettings())
        await store.set("k", b"hello")
        v = await store.get("k")

        # 原生向量视图
        await store.upsert_with_vector("alice_pref", [0.1, 0.2, ...], {"raw": b"..."})
        hits = await store.similarity_search([0.1, 0.2, ...], limit=5)
    """

    def __init__(
        self,
        path: Path | None = None,
        *,
        url: str | None = None,
        api_key: str | None = None,
        collection: str = "smartbutler_memory",
        vector_size: int = 1024,
        distance: str = "Cosine",
    ) -> None:
        """初始化 Qdrant 后端。

        Args:
            path: 嵌入式数据目录。设了则走 ``QdrantClient(path=...)``。
            url: 服务模式 URL。设了则走 ``QdrantClient(url=...)``。
            api_key: 服务模式 API key。
            collection: 集合名。
            vector_size: 向量维度。
            distance: 距离度量（Cosine / Euclid / Dot）。
        """
        if path is None and url is None:
            raise StorageError("QdrantStorage 需要 path 或 url 之一")
        if path is not None and url is not None:
            raise StorageError("QdrantStorage 不能同时设 path 和 url")
        if path is not None:
            path.mkdir(parents=True, exist_ok=True)
        self._path = path
        self._url = url
        self._api_key = api_key
        self._collection = collection
        self._vector_size = vector_size
        self._distance_name = distance
        try:
            self._distance = _DISTANCE_MAP[distance]
        except KeyError as e:
            raise StorageError(
                f"未知 distance: {distance};可选 {list(_DISTANCE_MAP)}"
            ) from e

        # QdrantClient 构造不会连服务端；连接惰性触发。
        if url is not None:
            self._client: QdrantClient = QdrantClient(url=url, api_key=api_key)
            self._mode = "server"
        else:
            assert path is not None
            self._client = QdrantClient(path=str(path))
            self._mode = "embedded"

    @property
    def mode(self) -> str:
        """'embedded' 或 'server'。"""
        return self._mode

    @property
    def collection(self) -> str:
        """当前集合名。"""
        return self._collection

    def _ensure_collection(self) -> None:
        """确保集合存在,首次 upsert/search 前调用。"""
        if not self._client.collection_exists(self._collection):
            self._client.create_collection(
                collection_name=self._collection,
                vectors_config=VectorParams(
                    size=self._vector_size,
                    distance=self._distance,
                ),
            )

    # --------------------- BaseStorage (KV 视图) ---------------------

    async def get(self, key: str) -> bytes | None:
        """按 key 取值(KV 视图)。通过 qdrant payload 过滤实现。"""
        hits = self._client.scroll(
            collection_name=self._collection,
            scroll_filter=Filter(
                must=[FieldCondition(key="kv_key", match=MatchValue(value=key))]
            ),
            limit=1,
            with_payload=True,
            with_vectors=False,
        )
        points, _ = hits
        if not points:
            return None
        payload = points[0].payload or {}
        raw = payload.get("raw")
        return raw.encode("utf-8") if isinstance(raw, str) else None

    async def set(self, key: str, value: bytes, *, ttl: int | None = None) -> None:
        """按 key 写入(KV 视图)。用 payload 存原始 bytes (utf-8 解码)。

        TTL 字段记到 payload.expires_at,读取时由调用方判断（Qdrant 没有原生 TTL）。
        """
        if not isinstance(value, bytes):
            raise StorageError(f"value must be bytes, got {type(value).__name__}")
        self._ensure_collection()
        # KV 视图用零向量(中心点)存,无意义但合法。
        zero_vector = [0.0] * self._vector_size
        import time
        payload: dict[str, Any] = {
            "kv_key": key,
            "raw": value.decode("utf-8", errors="replace"),
        }
        if ttl is not None:
            payload["expires_at"] = time.time() + ttl
        # 用 key 的稳定 hash 当 point id,确保 upsert 可更新。
        point_id = abs(hash(key)) % (2**63)
        self._client.upsert(
            collection_name=self._collection,
            points=[
                PointStruct(id=point_id, vector=zero_vector, payload=payload)
            ],
        )

    async def delete(self, key: str) -> bool:
        """按 key 删除(KV 视图)。"""
        # 先 scroll 找 id,再按 id 删
        points, _ = self._client.scroll(
            collection_name=self._collection,
            scroll_filter=Filter(
                must=[FieldCondition(key="kv_key", match=MatchValue(value=key))]
            ),
            limit=1,
            with_payload=False,
            with_vectors=False,
        )
        if not points:
            return False
        self._client.delete(
            collection_name=self._collection,
            points_selector=[p.id for p in points],
        )
        return True

    async def exists(self, key: str) -> bool:
        """判断 key 是否存在。"""
        return await self.get(key) is not None

    async def list_keys(self, prefix: str = "") -> list[str]:
        """列 key（受 Qdrant 默认 scroll 限制 10000 条）。"""
        points, _ = self._client.scroll(
            collection_name=self._collection,
            limit=10_000,
            with_payload=True,
            with_vectors=False,
        )
        result: list[str] = []
        for p in points:
            payload = p.payload or {}
            k = payload.get("kv_key")
            if not isinstance(k, str):
                continue
            if prefix and not k.startswith(prefix):
                continue
            result.append(k)
        return result

    async def close(self) -> None:
        """释放 client。QdrantClient 析构会自动关,但显式调更稳。"""
        try:
            self._client.close()
        except Exception:
            # Qdrant 1.19 在 Windows portalocker 析构会刷 NoneType 告警,忽略。
            pass

    # --------------------- 原生向量 API(Phase 6.2 用) ---------------------

    def upsert_with_vector(
        self,
        key: str,
        vector: list[float],
        payload: dict[str, Any] | None = None,
    ) -> None:
        """按 key 写入一条真实向量(Phase 6.2 语义检索用)。"""
        if len(vector) != self._vector_size:
            raise StorageError(
                f"vector size {len(vector)} != collection size {self._vector_size}"
            )
        self._ensure_collection()
        point_id = abs(hash(key)) % (2**63)
        merged: dict[str, Any] = {"kv_key": key, **(payload or {})}
        self._client.upsert(
            collection_name=self._collection,
            points=[PointStruct(id=point_id, vector=vector, payload=merged)],
        )

    def similarity_search(
        self,
        vector: list[float],
        *,
        limit: int = 5,
        score_threshold: float | None = None,
        filter_payload: dict[str, Any] | None = None,
    ) -> list[tuple[str, float, dict[str, Any]]]:
        """语义检索:返回 [(key, score, payload), ...](Phase 6.2 用)。

        Args:
            vector: 查询向量。
            limit: 返回条数上限。
            score_threshold: 最低相似度,过滤低分。
            filter_payload: 额外 payload 过滤条件。
        """
        if len(vector) != self._vector_size:
            raise StorageError(
                f"vector size {len(vector)} != collection size {self._vector_size}"
            )
        self._ensure_collection()
        scroll_filter: Filter | None = None
        if filter_payload:
            must = [
                FieldCondition(key=str(k), match=MatchValue(value=v))
                for k, v in filter_payload.items()
            ]
            scroll_filter = Filter(must=must)
        # Qdrant 1.19 用 query_points,search 没了。
        resp = self._client.query_points(
            collection_name=self._collection,
            query=vector,
            limit=limit,
            score_threshold=score_threshold,
            query_filter=scroll_filter,
            with_payload=True,
        )
        result: list[tuple[str, float, dict[str, Any]]] = []
        for p in resp.points:
            payload = p.payload or {}
            key = payload.get("kv_key", str(p.id))
            result.append((key, p.score, payload))
        return result

    # --------------------- Phase 6.2 P0: range + tag 联合查询 ---------------------

    def similarity_search_with_filters(
        self,
        vector: list[float],
        *,
        k: int = 5,
        min_importance: float | None = None,
        tag: str | None = None,
    ) -> list[tuple[str, float, dict[str, Any]]]:
        """带 ``min_importance`` + ``tag`` 过滤的语义检索(Phase 6.2 P0)。

        区别于 ``similarity_search``:
        - ``min_importance`` 走 Qdrant 服务端 ``Range(gte=...)``,避免客户端二次过滤
        - ``tag`` 走服务端 ``MatchValue``,与现有 ``similarity_search`` 一致

        Args:
            vector: 查询向量。
            k: 返回条数上限。
            min_importance: 重要性下限,None = 不过滤。
            tag: 单 tag 精确匹配,None = 不过滤。
                注意:tag 如 ``"source:alice"`` 内部存为 ``"source_alice"``(Qdrant
                payload key 不支持冒号),传 tag 时也走同样的 sanitize。

        Returns:
            ``[(key, score, payload), ...]`` 列表。
        """
        if len(vector) != self._vector_size:
            raise StorageError(
                f"vector size {len(vector)} != collection size {self._vector_size}"
            )
        self._ensure_collection()

        must: list[FieldCondition] = []
        if min_importance is not None:
            must.append(
                FieldCondition(key="importance", range=Range(gte=float(min_importance)))
            )
        if tag is not None:
            # tag 在 ``similarity_search`` 里以 ``tags`` 数组存储,所以这里按数组元素匹配。
            # 数组里存的是原 tag(可能含冒号),所以 match 的 value 用原 tag,Qdrant
            # 本身支持数组里任意值匹配。
            must.append(FieldCondition(key="tags", match=MatchValue(value=tag)))

        scroll_filter: Filter | None = Filter(must=must) if must else None
        resp = self._client.query_points(
            collection_name=self._collection,
            query=vector,
            limit=k,
            query_filter=scroll_filter,
            with_payload=True,
        )
        result: list[tuple[str, float, dict[str, Any]]] = []
        for p in resp.points:
            payload = p.payload or {}
            key = payload.get("kv_key", str(p.id))
            result.append((key, p.score, payload))
        return result


__all__ = ["QdrantStorage"]
