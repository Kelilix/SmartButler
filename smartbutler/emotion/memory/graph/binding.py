"""🆕 Phase 9:声纹绑定 + 同名消歧。

**核心机制**(见 ``docs/PHASE_9_GRAPH_MEMORY_DESIGN.md`` §8):

```
声纹采集完成 → display_name="张伟", user_id="spk_b2c8"
    ↓
查图:按名字查候选节点
    ├─ 未命中 → 直接建节点 person:spk_b2c8(声纹锚定)✅
    └─ 命中   → ⚠️ **立即询问用户**(同名消歧)
                 "是不是这个人:张伟,我的同事,在XX公司工作?"
                 ├─ 答"是"   → 绑定到候选(node_id 不变,边不动)
                 └─ 答"不是" → 建新节点 person:name:<hash>#2 区分
```

**为什么命中 1 个也要问**:同名场景下"只找到 1 个"恰恰最危险 ——
把张伟B 的声音认成张伟A,后果比名字重复严重得多。

**为什么交给用户判断**:用户是这个世界上最权威的"同一人判定器"。
把"同名不同人"这个技术难题转成一次对话交互,比任何向量算法都可靠。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from smartbutler.emotion.memory.graph.models import (
    GraphNode,
    make_voice_node_id,
)
from smartbutler.emotion.memory.graph.resolver import PersonResolver
from smartbutler.emotion.memory.graph.store import GraphStore
from smartbutler.utils.logging import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# 结果模型
# ---------------------------------------------------------------------------


@dataclass
class CandidateNode:
    """候选节点(给用户看的人选)。"""

    node_id: str
    name: str
    summary: str = ""
    voice_bound: bool = False

    def to_dict(self) -> dict[str, Any]:
        """转成 tool 返回结构。"""
        return {
            "node_id": self.node_id,
            "name": self.name,
            "summary": self.summary,
            "voice_bound": self.voice_bound,
        }


@dataclass
class BindOutcome:
    """绑定结果。

    ``status``:
    - ``"bound"``    —— 已绑定到已有节点
    - ``"created"``  —— 新建节点(未命中候选)
    - ``"distinct"`` —— 用户确认是另一个人 → 新建区分节点
    - ``"needs_confirmation"`` —— 有候选,等用户回答
    - ``"skipped"``  —— 用户不在 / 询问失败,放弃本次绑定
    """

    status: str
    node_id: str = ""
    candidates: list[CandidateNode] = field(default_factory=list)
    reason: str = ""

    @property
    def is_pending(self) -> bool:
        """是否在等用户确认。"""
        return self.status == "needs_confirmation"


# ---------------------------------------------------------------------------
# 绑定器
# ---------------------------------------------------------------------------


class VoiceBinding:
    """把声纹绑定到图节点 + 同名消歧。

    用法::

        binder = VoiceBinding(store=store, resolver=resolver)
        outcome = await binder.bind(user_id="spk_b2c8", display_name="张伟")
        if outcome.is_pending:
            # 向用户确认后
            await binder.confirm(outcome.candidates[0].node_id, user_id="spk_b2c8",
                                display_name="张伟")
    """

    def __init__(
        self,
        store: GraphStore,
        resolver: PersonResolver,
    ) -> None:
        self._store = store
        self._resolver = resolver

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------

    async def bind(
        self,
        *,
        user_id: str,
        display_name: str,
        confirm: bool = True,
    ) -> BindOutcome:
        """绑定声纹到图节点。

        Args:
            user_id: 声纹 user_id。
            display_name: 用户自报的名字。
            confirm: 是否允许返回"待确认"状态(True = 需要消歧时返回
                ``needs_confirmation``;False = 直接跳过同名候选,建新节点)。

        Returns:
            :class:`BindOutcome`。
        """
        if not user_id:
            return BindOutcome(status="skipped", reason="缺少 user_id")
        if not display_name or not display_name.strip():
            return BindOutcome(status="skipped", reason="缺少 display_name")

        display_name = display_name.strip()

        # 幂等:这个 user_id 已经绑过节点了
        existing = self._store.find_node_by_user_id(user_id)
        if existing is not None:
            logger.info(
                "graph_binding.already_bound user_id=%s node_id=%s",
                user_id, existing.node_id,
            )
            return BindOutcome(status="bound", node_id=existing.node_id)

        # 查候选(图里已有叫这个名字的人)
        candidates = self._resolver.find_candidates(display_name)

        if not candidates:
            # 未命中 → 直接建声纹锚定节点
            node = GraphNode(
                node_id=make_voice_node_id(user_id),
                name=display_name,
                user_id=user_id,
                voice_bound=True,
            )
            self._store.upsert_node(node)
            logger.info(
                "graph_binding.created user_id=%s node_id=%s name=%s",
                user_id, node.node_id, display_name,
            )
            return BindOutcome(status="created", node_id=node.node_id)

        # 命中 → 同名消歧
        if not confirm:
            # 不允许询问 → 建区分节点(走 distinct 逻辑)
            return self._create_distinct(
                user_id=user_id, display_name=display_name, reason="no_confirm"
            )

        payload = [self._to_candidate(n) for n in candidates]
        logger.info(
            "graph_binding.needs_confirmation user_id=%s name=%s candidates=%s",
            user_id, display_name, len(payload),
        )
        return BindOutcome(status="needs_confirmation", candidates=payload)

    async def confirm(
        self,
        *,
        user_id: str,
        display_name: str,
        candidate_node_id: str,
    ) -> BindOutcome:
        """用户确认"是" → 绑定到该候选节点。

        ⚠️ **node_id 不变** —— 已有边无需重连(design §2.3 技术基石)。

        Args:
            user_id: 声纹 user_id。
            display_name: 名字(作为别名补进节点)。
            candidate_node_id: 用户选定的候选节点 id。

        Returns:
            :class:`BindOutcome`。
        """
        if not user_id or not candidate_node_id:
            return BindOutcome(status="skipped", reason="参数缺失")

        node = self._store.get_node(candidate_node_id)
        if node is None:
            logger.warning(
                "graph_binding.confirm_node_missing node_id=%s", candidate_node_id
            )
            return BindOutcome(status="skipped", reason="候选节点不存在")

        ok = self._store.bind_voice(candidate_node_id, user_id)
        if not ok:
            return BindOutcome(status="skipped", reason="绑定失败")

        # 名字作为别名补进节点(用户自报的名字可能与图里的主名不同)
        if display_name and display_name not in node.all_names():
            self._store.add_alias(candidate_node_id, display_name)

        logger.info(
            "graph_binding.confirmed user_id=%s node_id=%s name=%s",
            user_id, candidate_node_id, node.name,
        )
        return BindOutcome(status="bound", node_id=candidate_node_id)

    async def distinguish(
        self,
        *,
        user_id: str,
        display_name: str,
    ) -> BindOutcome:
        """用户确认"不是,是另一个人" → 建区分节点。

        新节点 id = ``person:name:<hash>#N``,``name`` 保持原样。

        Args:
            user_id: 声纹 user_id。
            display_name: 名字。

        Returns:
            :class:`BindOutcome`。
        """
        return self._create_distinct(
            user_id=user_id,
            display_name=display_name,
            reason="user_said_distinct",
        )

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _create_distinct(
        self,
        *,
        user_id: str,
        display_name: str,
        reason: str,
    ) -> BindOutcome:
        """建区分节点(同名不同人)。

        ⚠️ **必须避开所有已有的同名节点 id** —— 不只是名字锚点形态,
        ``person:spk_xxx``(声纹锚点)也是"张伟"。

        为什么要"强制后缀":base id ``person:name:<hash>`` 只在**这个节点
        自己**存在时才是空闲的。区分场景下,图里已经有"张伟"(别的 id 形态),
        新节点必须显式带后缀,否则读者无法从 id 看出它是"另一个张伟"。
        """
        same_name_nodes = self._store.find_nodes_by_name(display_name)
        existing_ids = [n.node_id for n in same_name_nodes]
        node_id = self._resolver.make_distinct_node_id(
            display_name, existing_ids, force_suffix=True
        )

        node = GraphNode(
            node_id=node_id,
            name=display_name,
            user_id=user_id,
            voice_bound=True,
            summary=f"(与另一位{display_name}区分)",
        )
        self._store.upsert_node(node)
        logger.info(
            "graph_binding.distinct user_id=%s node_id=%s name=%s reason=%s",
            user_id, node_id, display_name, reason,
        )
        return BindOutcome(status="distinct", node_id=node_id)

    @staticmethod
    def _to_candidate(node: GraphNode) -> CandidateNode:
        """节点 → 候选(带可展示的描述)。"""
        return CandidateNode(
            node_id=node.node_id,
            name=node.name,
            summary=node.summary,
            voice_bound=node.voice_bound,
        )

    # ------------------------------------------------------------------
    # 描述生成
    # ------------------------------------------------------------------

    @staticmethod
    def describe_candidate(candidate: CandidateNode) -> str:
        """生成给用户看的描述(一句话)。

        ``"张伟,我的同事,在XX公司工作"`` / ``"张伟"``
        """
        if candidate.summary:
            return f"{candidate.name},{candidate.summary}"
        return candidate.name


__all__ = ["BindOutcome", "CandidateNode", "VoiceBinding"]
