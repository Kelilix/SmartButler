"""🆕 Phase 9:图的落库 —— 批内消解 + 单事务批写 + 幂等。

**批内消解为什么必须自己做**(design §5.4):

DeepWiki 对 Graphiti bulk 模式的诚实评价:

    Key constraint: bulk processing trades completeness of cross-episode
    temporal invalidation for throughput. Edge invalidation and deduplication
    happen per episode within the batch, but episodes within the same batch
    **do not see each other's newly added edges** during resolution.

我们的规避:task 内的边在**落库前**先在内存做一轮判重,再统一写库。

附注:Graphiti 至今有未修的 bug(#1872)—— bulk 路径下完全相同的 fact
会被写成两条边。说明这套机制本身不完美,批内消解要自己写对。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

from smartbutler.emotion.memory.graph.contradiction import ContradictionResolver
from smartbutler.emotion.memory.graph.extractor import ExtractionResult
from smartbutler.emotion.memory.graph.models import (
    GraphEdge,
    GraphEdgeCandidate,
    GraphNode,
    make_edge_id,
)
from smartbutler.emotion.memory.graph.resolver import PersonResolver
from smartbutler.emotion.memory.graph.store import GraphStore
from smartbutler.utils.logging import get_logger

logger = get_logger(__name__)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


class GraphWriter:
    """把抽取结果落库。

    职责:
    1. **批内消解** —— task 内的边先在内存判重(同端点 + 谓词 + fact)
    2. **节点解析** —— 人名 → node_id
    3. **矛盾消解** —— 与已有边判重/判矛盾(可选,注入 resolver)
    4. **幂等落库** —— 同 edge_id 只追加 task_ids
    5. **summary 聚合** —— 节点 summary = 活跃边 fact 句聚合(供消歧时描述)
    """

    def __init__(
        self,
        store: GraphStore,
        resolver: PersonResolver,
        contradiction: ContradictionResolver | None = None,
    ) -> None:
        self._store = store
        self._resolver = resolver
        self._contradiction = contradiction

    async def write_task(
        self,
        *,
        task_id: str,
        result: ExtractionResult,
        speaker: str = "",
        valid_at: str | None = None,
    ) -> tuple[int, int]:
        """落库一个 task 的抽取结果。

        Args:
            task_id: task id(溯源 + 幂等键)。
            result: 抽取结果。
            speaker: 说话人 user_id(边的 speaker 字段)。
            valid_at: 关系在现实中成立的时间(复用 fact 的 valid_at,决策④)。

        Returns:
            ``(节点数, 边数)`` —— 实际新增/复用的节点数与新增边数。
        """
        if not task_id:
            return (0, 0)

        # 幂等:同一 task 不重复抽
        if self._store.has_task(task_id):
            logger.info("graph_writer.skip_duplicate task_id=%s", task_id)
            return (0, 0)

        try:
            return await self._write_task_inner(
                task_id=task_id,
                result=result,
                speaker=speaker,
                valid_at=valid_at,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("graph_writer.write_failed task_id=%s err=%s", task_id, e)
            self._store.log_task(task_id, status="failed")
            return (0, 0)

    async def _write_task_inner(
        self,
        *,
        task_id: str,
        result: ExtractionResult,
        speaker: str,
        valid_at: str | None,
    ) -> tuple[int, int]:
        # 1) 批内消解(内存)
        candidates = self._dedupe_in_batch(result.edges)

        # 2) 节点解析 + 收集要建的节点
        node_map: dict[str, GraphNode] = {}   # 人名 → 节点
        resolved_edges: list[tuple[GraphEdgeCandidate, str, str]] = []

        for cand in candidates:
            src_node = await self._resolve_or_create(cand.source, node_map)
            tgt_node = await self._resolve_or_create(cand.target, node_map)
            if src_node is None or tgt_node is None:
                continue
            if src_node.node_id == tgt_node.node_id:
                continue
            resolved_edges.append((cand, src_node.node_id, tgt_node.node_id))

        # 3) 落节点
        for node in node_map.values():
            self._store.upsert_node(node)

        # 4) 落边(先过矛盾消解)
        new_edges = 0
        for cand, src_id, tgt_id in resolved_edges:
            edge_id = make_edge_id(src_id, cand.relation_type, tgt_id, cand.fact)
            edge = GraphEdge(
                edge_id=edge_id,
                source_id=src_id,
                target_id=tgt_id,
                relation_type=cand.relation_type,
                fact=cand.fact,
                speaker=speaker,
                valid_at=valid_at,
                created_at=_now_iso(),
                task_ids=[task_id],
            )

            # 矛盾消解:重复则合并,矛盾则先失效旧边
            if self._contradiction is not None:
                try:
                    outcome = await self._contradiction.resolve(
                        new_edge=edge, valid_at=valid_at
                    )
                    if outcome == "merged":
                        continue
                except Exception as e:  # noqa: BLE001
                    logger.warning(
                        "graph_writer.contradiction_failed task_id=%s err=%s",
                        task_id, e,
                    )

            if self._store.upsert_edge(edge):
                new_edges += 1

        # 5) 更新受影响节点的 summary
        touched = {src_id for _, src_id, _ in resolved_edges}
        touched |= {tgt_id for _, _, tgt_id in resolved_edges}
        for node_id in touched:
            self._refresh_summary(node_id)

        # 6) 幂等日志
        self._store.log_task(
            task_id,
            node_count=len(node_map),
            edge_count=new_edges,
            status="ok",
        )

        logger.info(
            "graph_writer.written task_id=%s nodes=%s edges=%s dropped_endpoint=%s "
            "dropped_predicate=%s",
            task_id, len(node_map), new_edges,
            result.dropped_invalid_endpoint, result.dropped_unknown_predicate,
        )
        return (len(node_map), new_edges)

    # ------------------------------------------------------------------
    # 批内消解
    # ------------------------------------------------------------------

    @staticmethod
    def _dedupe_in_batch(
        edges: Sequence[GraphEdgeCandidate],
    ) -> list[GraphEdgeCandidate]:
        """task 内的边在内存判重。

        判据:**同 (source, relation_type, target) 且 fact 归一化后相同** →
        视为同一条边,只留第一条。

        同端点但 fact 不同 → **保留**(可能是关系的演进,交给矛盾消解处理)。

        这是对 Graphiti bulk 缺陷(#1872)的规避 —— 那里完全相同的 fact
        会被写成两条边。
        """
        seen: set[tuple[str, str, str, str]] = set()
        out: list[GraphEdgeCandidate] = []
        for cand in edges:
            normalized_fact = "".join(cand.fact.split()).lower()
            key = (cand.source, cand.relation_type, cand.target, normalized_fact)
            if key in seen:
                continue
            seen.add(key)
            out.append(cand)
        return out

    # ------------------------------------------------------------------
    # 节点解析
    # ------------------------------------------------------------------

    async def _resolve_or_create(
        self,
        name: str,
        node_map: dict[str, GraphNode],
    ) -> GraphNode | None:
        """解析人名 → 节点(带本批缓存)。

        缓存的作用:同一个 task 里反复提到同一个人时,解析结果一致,
        不会出现"前半批用 node_id A、后半批用 node_id B"的分裂。
        """
        if name in node_map:
            return node_map[name]

        node = await self._resolver.resolve(name)
        if node is None:
            return None
        node_map[name] = node
        return node

    # ------------------------------------------------------------------
    # summary 聚合
    # ------------------------------------------------------------------

    def _refresh_summary(self, node_id: str) -> None:
        """重算节点 summary = 活跃边 fact 句聚合。

        summary 用于**向用户描述这个人**(同名消歧时问"是不是这个人:
        张伟,我的同事,在XX公司工作?")—— 所以必须来自图里的真实关系,
        不能编造。

        Args:
            node_id: 节点 id。
        """
        edges = self._store.find_edges_for_node(node_id, only_active=True, limit=20)
        facts: list[str] = []
        for e in edges:
            fact = e.fact.strip()
            if fact and fact not in facts:
                facts.append(fact)
        summary = "; ".join(facts[:5])
        node = self._store.get_node(node_id)
        if node is None:
            return
        if node.summary == summary:
            return
        node.summary = summary
        self._store.upsert_node(node)


__all__ = ["GraphWriter"]
