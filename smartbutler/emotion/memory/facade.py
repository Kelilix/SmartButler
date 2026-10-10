"""MemoryFacade —— thinking 层唯一入口(Phase 6.2 P0 核心)。

设计原则:
1. **thinking 只 import facade** —— 不感知底层 Embedder / Retriever / Hooks
2. **所有方法非阻塞 async** —— 任何环节失败快速降级,管家不崩
3. **内部统一调 MemoryHooks** —— 扩展能力的接入点
4. **source 隔离(问题 10)** —— remember 自动写 ``source:xxx`` tag,
   recall 自动按 source tag 过滤(后续 6.3 加 ``user_id`` payload 做强隔离)

关键接口:
- ``MemoryContext``: ``user_id``(可空) + ``source``(说话人) + ``session_id``(跨 thread 标识)
- ``remember()``: 存长期记忆
- ``recall()``: 召回相关记忆
- ``format_for_prompt()``: 召回 + 格式化成可注入 system prompt 的 markdown 片段
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from smartbutler.emotion.memory.hooks import MemoryEventType, MemoryHooks
from smartbutler.emotion.memory.retrieval import MemoryRetriever
from smartbutler.emotion.memory.long_term import LongTermStore
from smartbutler.capabilities.embedding.base import Embedder
from smartbutler.utils.logging import get_logger

logger = get_logger(__name__)


@dataclass
class MemoryContext:
    """调用方提供的上下文(全部可空,空 = 单用户模式)。"""

    user_id: str | None = None
    source: str = ""
    session_id: str | None = None


class MemoryFacade:
    """Memory 高级 API —— thinking 层唯一入口。"""

    def __init__(
        self,
        retriever: MemoryRetriever,
        hooks: MemoryHooks,
        long_term: LongTermStore,
        embedder: Embedder,
    ) -> None:
        self._retriever = retriever
        self._hooks = hooks
        self._long_term = long_term
        self._embedder = embedder

    @property
    def embedder(self) -> Embedder:
        """暴露 embedder,供 thinking 直接 embed(预留,P0 阶段不调)。"""
        return self._embedder

    @property
    def retriever(self) -> MemoryRetriever:
        return self._retriever

    async def remember(
        self,
        content: str,
        *,
        ctx: MemoryContext | None = None,
        tags: list[str] | None = None,
        importance: float | None = None,
    ) -> str:
        """存一条长期记忆。返回 key。

        Args:
            content: 原文文本(必填)。
            ctx: 调用方上下文,可空(空 = 单用户模式)。
            tags: 额外标签列表。
            importance: 显式指定重要性,None = 启发式评分。

        Returns:
            存入的 key 字符串。
        """
        if not content or not content.strip():
            raise ValueError("content 不能为空")
        source = ctx.source if ctx else ""
        key = await self._retriever.remember(
            content,
            tags=list(tags) if tags else None,
            source=source,
            importance=importance,
        )
        self._hooks.trigger(MemoryEventType.ON_STORE, key=key, content=content)
        logger.debug("facade.remember", key=key, source=source)
        return key

    async def recall(
        self,
        query: str,
        *,
        ctx: MemoryContext | None = None,
        k: int = 5,
        min_importance: float = 0.3,
    ) -> list[dict[str, Any]]:
        """召回相关记忆。语义检索 + 重要性过滤 + 时间衰减。

        Args:
            query: 查询文本。
            ctx: 调用方上下文,可空。
            k: 最多返回条数。
            min_importance: 重要性下限。

        Returns:
            ``[{"key": ..., "content": ..., "score": ..., ...}, ...]``
        """
        if not query or not query.strip():
            return []
        # source 作 tag 过滤(多用户场景下不同 source 不串)
        tag: str | None = None
        if ctx and ctx.source:
            tag = f"source:{ctx.source}"
        hits = await self._retriever.recall(
            query,
            k=k,
            min_importance=min_importance,
            tag=tag,
        )
        for h in hits:
            self._hooks.trigger(
                MemoryEventType.ON_RECALL,
                key=h["key"],
                score=h.get("score", 0.0),
            )
        logger.debug("facade.recall", query_preview=query[:50], hit_count=len(hits))
        return hits

    async def format_for_prompt(
        self,
        query: str,
        *,
        ctx: MemoryContext | None = None,
        k: int = 5,
        max_chars: int = 2000,
    ) -> str:
        """召回 + 格式化成可注入 system prompt 的 markdown 片段。

        返回示例::

            ### 相关历史记忆(共 3 条)
            - 用户之前说:10.8 要去迪士尼
            - 用户提到:喜欢喝美式咖啡
            - 用户提到:住在浦东

        空召回返回空字符串。

        Args:
            query: 查询文本。
            ctx: 调用方上下文,可空。
            k: 最多召回条数。
            max_chars: 截断上限。

        Returns:
            markdown 格式的记忆片段,或空字符串。
        """
        hits = await self.recall(query, ctx=ctx, k=k)
        if not hits:
            return ""
        lines: list[str] = [f"### 相关历史记忆(共 {len(hits)} 条)"]
        for h in hits:
            content = h.get("content", "")
            if len(content) > 200:
                content = content[:197] + "..."
            lines.append(f"- {content}")
        block = "\n".join(lines)
        if len(block) > max_chars:
            block = block[: max_chars - 3] + "..."
        return block


__all__ = ["MemoryFacade", "MemoryContext"]
