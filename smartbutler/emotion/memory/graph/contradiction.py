"""🆕 Phase 9:矛盾消解 —— 历史关系修正(Step 3.5)。

**最高风险的一步**。Graphiti 把它列为最难的组件(design §5.2):

> "Temporal contradiction detection is one of the hardest problems in GraphRAG.
> A naive approach creates contradictory edges — for example, 'Alice loves Bob'
> and 'Alice hates Bob' both persist as active edges, causing retrieval to
> return both and leaving the LLM to guess which is current."

## 我们的策略:两阶段判重 + LLM 矛盾判定

```
新边落库前
    ↓
快路径:同端点 + 同谓词 + fact 句完全相同?
    ├─ 是 → 已有,只追加 task_ids(幂等)
    └─ 否 → 慢路径
              ↓
         同端点 + 同谓词 + fact 句**语义相近**? (向量)
              ├─ 是 → 同一关系的不同表述 → 合并(不新增边)
              └─ 否 → LLM 判定
                        ↓
                   新边是否**否定**旧边?
                        ├─ 是 → 旧边加 invalid_at(不 DELETE)
                        └─ 否 → 两条边共存(关系演进)
```

## 关键约束:绝不 DELETE

历史事实必须保留。"张女士以前是我朋友吗" 这类问题需要历史边。
失效只加时间戳:
- ``invalid_at`` —— 世界时间:关系在现实中终止的时间
- ``expired_at`` —— 系统时间:边在系统中被替换的时间
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from smartbutler.emotion.memory.graph.models import GraphEdge
from smartbutler.emotion.memory.graph.store import GraphStore
from smartbutler.utils.logging import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# 矛盾判定 prompt
# ---------------------------------------------------------------------------

_CONTRADICT_PROMPT = """你判断两条人物关系是否**互相矛盾**(即新关系取代旧关系)。

## 什么是矛盾

**矛盾(contradiction)**:同一个关系在**同一时间点**不能同时成立。
- 旧:"张女士是我朋友" → 新:"张女士不再是我朋友了,我们掰了" → **矛盾**
- 旧:"张女士是我朋友" → 新:"张女士是我同事" → **不矛盾**(关系演进)

## 什么不是矛盾

- 时间上不重叠的先后关系 → **不是**矛盾(那是演进)
- 同一关系的不同表述 → **不是**矛盾("我朋友张伟" vs "张伟是我朋友")
- 不同的关系类型 → **不是**矛盾

## 输出

严格 JSON,不要解释:

{{"contradicts": true/false, "reason": "简短理由"}}

## 待判断的两条关系

旧关系:{old}

新关系:{new}
"""


@dataclass
class ContradictionResult:
    """矛盾判定结果。"""

    contradicts: bool
    reason: str = ""

    @property
    def invalid_at(self) -> str | None:
        """关系终止的世界时间(矛盾时取新边的 valid_at)。"""
        return None


class ContradictionResolver:
    """矛盾消解器(两阶段判重 + LLM 矛盾判定)。

    用法::

        resolver = ContradictionResolver(store=store, llm=llm)
        outcome = await resolver.resolve(
            new_edge=edge, valid_at="2026-11-09",
        )
    """

    def __init__(
        self,
        store: GraphStore,
        llm: Any | None = None,
        *,
        embedder: Any | None = None,
        dedupe_threshold: float = 0.85,
        timeout_seconds: float = 30.0,
        max_tokens: int = 200,
    ) -> None:
        """Args:
            store: 图存储。
            llm: LLM(矛盾判定用)。None = 不做矛盾判定(只做快路径判重)。
            embedder: 向量编码器(慢路径判重用)。None = 跳过慢路径。
            dedupe_threshold: 慢路径相似度阈值。
            timeout_seconds: LLM 超时。
            max_tokens: LLM 输出上限。
        """
        self._store = store
        self._llm = llm
        self._embedder = embedder
        self._threshold = dedupe_threshold
        self._timeout = timeout_seconds
        self._max_tokens = max_tokens

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------

    async def resolve(
        self,
        *,
        new_edge: GraphEdge,
        valid_at: str | None = None,
    ) -> str:
        """判断新边与已有边的关系,必要时失效旧边。

        Args:
            new_edge: 待落库的新边。
            valid_at: 新边的世界时间(矛盾时用作旧边的 invalid_at)。

        Returns:
            ``"new"`` —— 新增边
            ``"merged"`` —— 与已有边重复(只追加溯源)
            ``"contradicts"`` —— 与旧边矛盾(旧边已失效,新边新增)
        """
        # 只对**同端点**的边做判重/矛盾判断
        existing = self._store.find_edges_by_pair(
            new_edge.source_id, new_edge.target_id, only_active=True
        )
        if not existing:
            return "new"

        # ---- 快路径:fact 句完全相同 → 幂等合并 ----
        norm_new = _normalize_fact(new_edge.fact)
        for e in existing:
            if _normalize_fact(e.fact) == norm_new:
                self._store.append_edge_task(e.edge_id, new_edge.task_ids[0])
                logger.info(
                    "graph_contradiction.fast_path_merged edge_id=%s", e.edge_id
                )
                return "merged"

        # ---- 慢路径:同谓词 + 语义相近 → 合并 ----
        if self._embedder is not None:
            for e in existing:
                if e.relation_type != new_edge.relation_type:
                    continue
                if await self._semantically_same(e, new_edge):
                    self._store.append_edge_task(e.edge_id, new_edge.task_ids[0])
                    logger.info(
                        "graph_contradiction.slow_path_merged edge_id=%s",
                        e.edge_id,
                    )
                    return "merged"

        # ---- LLM 判定矛盾 ----
        if self._llm is None:
            return "new"

        for e in existing:
            if e.relation_type != new_edge.relation_type:
                continue
            verdict = await self._judge(e, new_edge)
            if verdict.contradicts:
                self._invalidate(e, new_edge, valid_at)
                logger.info(
                    "graph_contradiction.invalidated old=%s new=%s reason=%s",
                    e.edge_id, new_edge.edge_id, verdict.reason,
                )
                return "contradicts"

        return "new"

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    async def _semantically_same(
        self,
        a: GraphEdge,
        b: GraphEdge,
    ) -> bool:
        """慢路径:向量相似度判重。

        Args:
            a: 已有边。
            b: 新边。

        Returns:
            True = 语义上是同一关系的不同表述。
        """
        try:
            emb_a = a.embedding
            emb_b = b.embedding
            if emb_a is None:
                emb_a = await self._embed(a.fact)
            if emb_b is None:
                emb_b = await self._embed(b.fact)
            if emb_a is None or emb_b is None:
                return False
            sim = _cosine(emb_a, emb_b)
            return sim >= self._threshold
        except Exception as e:  # noqa: BLE001
            logger.warning("graph_contradiction.slow_path_failed err=%s", e)
            return False

    async def _embed(self, text: str) -> bytes | None:
        """编码文本(embedder 接口兼容)。"""
        if self._embedder is None:
            return None
        try:
            vec = await self._embedder.embed(text)
        except Exception as e:  # noqa: BLE001
            logger.warning("graph_contradiction.embed_failed err=%s", e)
            return None
        if not vec:
            return None
        try:
            import struct

            return struct.pack(f"{len(vec)}f", *vec)
        except (TypeError, ValueError):
            return None

    async def _judge(self, old: GraphEdge, new: GraphEdge) -> ContradictionResult:
        """LLM 判定两条关系是否矛盾。"""
        import asyncio

        prompt = (
            _CONTRADICT_PROMPT
            .replace("{old}", f"{old.relation_type}: {old.fact}")
            .replace("{new}", f"{new.relation_type}: {new.fact}")
        )
        try:
            from smartbutler.capabilities.llm.types import Message

            resp = await asyncio.wait_for(
                self._llm.chat(
                    [
                        Message(
                            role="system",
                            content="你只做关系矛盾判定,输出严格 JSON。",
                        ),
                        Message(role="user", content=prompt),
                    ],
                    temperature=0.0,
                    max_tokens=self._max_tokens,
                ),
                timeout=self._timeout,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("graph_contradiction.llm_failed err=%s", e)
            return ContradictionResult(contradicts=False, reason="llm_error")

        raw = (getattr(resp, "content", "") or "").strip()
        parsed = _parse_json(raw)
        if parsed is None:
            return ContradictionResult(contradicts=False, reason="bad_json")
        return ContradictionResult(
            contradicts=bool(parsed.get("contradicts", False)),
            reason=str(parsed.get("reason", ""))[:100],
        )

    def _invalidate(
        self,
        old: GraphEdge,
        new: GraphEdge,
        valid_at: str | None,
    ) -> None:
        """把旧边标记为失效(不 DELETE)。"""
        from datetime import UTC, datetime

        expired_at = datetime.now(UTC).isoformat()
        # invalid_at 优先用新边的世界时间;没有就用系统时间
        invalid_at = valid_at or expired_at
        self._store.invalidate_edge(
            old.edge_id,
            invalid_at=invalid_at,
            expired_at=expired_at,
        )


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def _normalize_fact(fact: str) -> str:
    """fact 句归一化(去空白 + 小写)。"""
    return "".join((fact or "").split()).lower()


def _cosine(a: bytes, b: bytes) -> float:
    """两个 float32 blob 的余弦相似度。"""
    import struct

    try:
        va = struct.unpack(f"{len(a) // 4}f", a)
        vb = struct.unpack(f"{len(b) // 4}f", b)
    except struct.error:
        return 0.0
    if len(va) != len(vb) or not va:
        return 0.0
    dot = sum(x * y for x, y in zip(va, vb, strict=False))
    na = sum(x * x for x in va) ** 0.5
    nb = sum(y * y for y in vb) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _parse_json(raw: str) -> dict[str, Any] | None:
    """抠出 JSON 对象(容错围栏)。"""
    if not raw:
        return None
    text = raw.strip()
    text = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", text)
    text = re.sub(r"\s*```$", "", text).strip()
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except (TypeError, ValueError):
        pass
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    data = json.loads(text[start : i + 1])
                    return data if isinstance(data, dict) else None
                except (TypeError, ValueError):
                    return None
    return None


__all__ = ["ContradictionResolver", "ContradictionResult"]
