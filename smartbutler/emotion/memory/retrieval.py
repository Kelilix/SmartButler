"""MemoryRetriever(Phase 6.2 P0)。

组合 ``Embedder`` + ``LongTermStore`` + ``ImportanceScorer`` + 时间衰减。
thinking 层不直接 import 这个,只通过 ``MemoryFacade`` 调用。
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass
from datetime import datetime, UTC
from typing import Any

from smartbutler.capabilities.embedding.base import Embedder, EmbedderError
from smartbutler.emotion.memory.importance import ImportanceScorer
from smartbutler.emotion.memory.long_term import LongTermStore
from smartbutler.utils.logging import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class RetrievalHit:
    """召回结果的一条。"""

    key: str
    content: str
    raw_score: float          # Qdrant 返回的相似度(0-1,越大越相关)
    importance: float
    tags: list[str]
    created_at: datetime
    final_score: float        # 重排序后(重要性 + 时间衰减)的最终分

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "content": self.content,
            "score": self.final_score,
            "raw_score": self.raw_score,
            "importance": self.importance,
            "tags": list(self.tags),
            "created_at": self.created_at.isoformat(),
        }


class MemoryRetriever:
    """``remember`` + ``recall`` + 衰减重排序。"""

    def __init__(
        self,
        store: LongTermStore,
        embedder: Embedder,
        scorer: ImportanceScorer | None = None,
        *,
        decay_half_life_days: float = 30.0,
    ) -> None:
        if decay_half_life_days <= 0:
            raise ValueError(
                f"decay_half_life_days 必须 > 0,得到 {decay_half_life_days}"
            )
        self._store = store
        self._embedder = embedder
        self._scorer = scorer or ImportanceScorer()
        self._decay_half_life_days = decay_half_life_days

    async def remember(
        self,
        content: str,
        *,
        tags: list[str] | None = None,
        source: str = "",
        importance: float | None = None,
    ) -> str:
        """存一条长期记忆。返回 key。

        Args:
            content: 原文文本。
            tags: 标签列表(可选)。
            source: 说话人/来源,会自动追加到 tags 方便 recall 过滤。
            importance: 显式指定重要性,None 则自动启发式评分。
        """
        if not content or not content.strip():
            raise ValueError("content 不能为空")
        # embed
        try:
            emb = await self._embedder.embed(content)
        except EmbedderError as e:
            logger.warning("retriever.embed_failed,无法存入", error=str(e))
            raise
        # score
        score = importance if importance is not None else await self._scorer.score(content)
        # tags: 把 source 也写进去(后续 recall 用 source tag 过滤)
        all_tags = list(tags or [])
        if source:
            all_tags.append(f"source:{source}")
        key = self._make_key(source)
        # 写存储
        self._store.store_memory(
            key, emb, content, importance=score, tags=all_tags,
        )
        logger.debug("retriever.remember key=%s score=%.2f", key, score)
        return key

    async def recall(
        self,
        query: str,
        *,
        k: int = 5,
        min_importance: float = 0.0,
        tag: str | None = None,
        now: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """召回相关记忆,按 ``重要性 + 时间衰减`` 重排序。

        流程:
        1. embed query
        2. 走 Qdrant 服务端 importance 过滤 + tag 过滤,取 ``2k`` 候选
        3. 客户端按 ``score * exp(-Δdays / half_life * ln 2)`` 重排
        4. 取 top k,转成 ``dict`` 返回
        """
        if not query or not query.strip():
            return []
        try:
            emb = await self._embedder.embed(query)
        except EmbedderError as e:
            logger.warning("retriever.embed_failed,recall 返回空", error=str(e))
            return []
        # 2 倍候选,客户端按时间衰减重排
        hits = self._store.search_memory(
            emb, k=k * 2, min_importance=min_importance, tag=tag,
        )
        if not hits:
            return []
        now = now or datetime.now(UTC)
        # 客户端重排:用 importance + 时间衰减 + 原始相似度
        ranked: list[RetrievalHit] = []
        for h in hits:
            created_str = h.get("created_at", "")
            try:
                created_at = datetime.fromisoformat(created_str) if created_str else now
            except ValueError:
                created_at = now
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=UTC)
            # 时间衰减
            delta_days = max(0.0, (now - created_at).total_seconds() / 86400.0)
            decay = math.exp(-delta_days / self._decay_half_life_days * math.log(2))
            # raw_normalized: Qdrant cosine score 范围 [-1, 1],映射到 [0, 1]
            raw = float(h.get("score", 0.0))
            raw_norm = max(0.0, min(1.0, (raw + 1.0) / 2.0))
            importance = float(h.get("importance", 0.5))
            # 综合:similarity(0-1) * time_decay(0-1) * importance_weight(0.5-1.0)
            final = raw_norm * decay * (0.5 + importance / 2.0)
            ranked.append(RetrievalHit(
                key=h["key"],
                content=h.get("content", ""),
                raw_score=float(h.get("score", 0.0)),
                importance=importance,
                tags=list(h.get("tags", [])),
                created_at=created_at,
                final_score=final,
            ))
        ranked.sort(key=lambda x: x.final_score, reverse=True)
        return [h.to_dict() for h in ranked[:k]]

    @staticmethod
    def _make_key(source: str) -> str:
        """生成唯一 key。"""
        if source:
            return f"mem:{source}:{uuid.uuid4().hex[:12]}"
        return f"mem:{uuid.uuid4().hex[:16]}"


__all__ = ["MemoryRetriever", "RetrievalHit"]
