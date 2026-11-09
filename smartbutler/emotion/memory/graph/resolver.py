"""🆕 Phase 9:人物节点解析(替代通用实体消解)。

Graphiti 需要三级漏斗(归一化精确 → 向量近邻 → LLM 判重)是因为它面对
**任意类型**的实体,只有名字可用。

我们的节点**只有 Person**,而 Person 有两条更强的路径:

- **路径 A(声纹档案命中)** —— ``registry.get_by_display_name(name)``
  → ``person:<user_id>``。**硬锚定,零消解成本**。
- **路径 B(图内名字匹配)** —— 查 ``name`` + ``aliases``
  → 复用已有节点。
- **路径 C(名字归一化哈希)** —— ``person:name:<hash>``
  → 新建节点。

**路径 A 是我们相对 Graphiti 的结构性优势** —— Graphiti 最贵的一环在我们
这里对 Person 完全不需要(见 design §7)。
"""

from __future__ import annotations

from typing import Any

from smartbutler.emotion.memory.graph.models import (
    NODE_TYPE_PERSON,
    GraphNode,
    make_name_node_id,
    make_voice_node_id,
    normalize_person_name,
)
from smartbutler.emotion.memory.graph.store import GraphStore
from smartbutler.utils.logging import get_logger

logger = get_logger(__name__)


class PersonResolver:
    """把人名解析成稳定的 ``node_id``。

    用法::

        resolver = PersonResolver(store=store, registry=registry)
        node = await resolver.resolve("张女士")
        node.node_id  # "person:spk_a3f9" 或 "person:name:<hash>"

    设计约束:
    - **解析不建节点** —— ``resolve`` 只查不写;建节点由调用方决定
      (避免"查一下"这种只读操作产生副作用)。
    - **同名字的人不合并** —— 归一化只做字符串级处理,同名交给消歧流程
      (见 ``binding.py``)。
    """

    def __init__(
        self,
        store: GraphStore,
        registry: Any | None = None,
    ) -> None:
        """Args:
            store: 图存储(路径 B/C 用)。
            registry: 声纹注册表(路径 A 用)。None = 跳过路径 A。
        """
        self._store = store
        self._registry = registry

    # ------------------------------------------------------------------
    # 解析
    # ------------------------------------------------------------------

    async def resolve(self, name: str) -> GraphNode | None:
        """解析人名 → 节点(只查不建)。

        Args:
            name: 人名。

        Returns:
            命中的节点;三种路径都没命中时返回 None。
        """
        if not name or not name.strip():
            return None

        # 路径 A:声纹档案命中(硬锚点)
        node = await self.resolve_by_voice(name)
        if node is not None:
            return node

        # 路径 B:图内名字匹配
        candidates = self._store.find_nodes_by_name(name)
        if candidates:
            # 多个同名 → 返回第一个,消歧由上层处理
            # (优先已绑声纹的,其次边多的)
            return self._pick_primary(candidates)

        # 路径 C:未命中 —— 返回一个"待建"的节点(不写库)
        return GraphNode(
            node_id=make_name_node_id(name),
            name=name.strip(),
            type=NODE_TYPE_PERSON,
        )

    async def resolve_id(self, name: str) -> str | None:
        """解析人名 → node_id(便捷方法)。"""
        node = await self.resolve(name)
        return node.node_id if node else None

    def find_candidates(self, name: str) -> list[GraphNode]:
        """按名查所有候选节点(同名消歧用)。

        ⚠️ **不查声纹档案** —— 这里只关心"图里已经有哪些叫这个名字的人"。
        声纹档案的命中在 ``resolve_by_voice`` 里单独处理。

        Args:
            name: 人名。

        Returns:
            候选节点列表(可能为空)。
        """
        if not name or not name.strip():
            return []
        return self._store.find_nodes_by_name(name)

    async def resolve_by_voice(self, name: str) -> GraphNode | None:
        """路径 A:按名字查声纹档案 → 节点。

        命中时**优先返回图里已有的节点**(它可能已经有边和别名),
        这样绑定声纹时能保住已有关系。

        Args:
            name: 人名。

        Returns:
            命中的节点;未命中返回 None。
        """
        if not name or not name.strip():
            return None
        if self._registry is None:
            return None

        try:
            profile = await self._registry.get_by_display_name(name.strip())
        except Exception as e:  # noqa: BLE001
            logger.warning("graph_resolver.voice_lookup_failed name=%s err=%s", name, e)
            return None

        if profile is None:
            return None

        user_id = getattr(profile, "user_id", None)
        if not user_id:
            return None

        # 图里已有这个 user_id 的节点?→ 用它(保住已有边)
        existing = self._store.find_node_by_user_id(user_id)
        if existing is not None:
            return existing

        # 图里没有 → 声纹锚定的"待建"节点
        display_name = getattr(profile, "display_name", "") or name.strip()
        return GraphNode(
            node_id=make_voice_node_id(user_id),
            name=display_name,
            type=NODE_TYPE_PERSON,
            user_id=user_id,
            voice_bound=True,
        )

    # ------------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------------

    @staticmethod
    def _pick_primary(candidates: list[GraphNode]) -> GraphNode:
        """多个同名候选里挑一个"最可能"的主节点。

        规则(与 ``binding.py`` 的主节点选择一致):
        1. 优先 ``voice_bound=1``(有声纹锚点的更可靠)
        2. 其次建得早的(更早建立 = 更可能是原始节点)
        """
        return sorted(
            candidates,
            key=lambda n: (not n.voice_bound, n.created_at or ""),
        )[0]

    @staticmethod
    def make_distinct_node_id(
        name: str,
        existing_ids: list[str],
        *,
        force_suffix: bool = False,
    ) -> str:
        """为同名的人生成一个区分用的节点 id。

        ``person:name:<hash>`` → ``person:name:<hash>#2`` → ``#3`` ...

        **node_id 带后缀,但 ``name`` 保持原样** —— 可读性靠 name,
        唯一性靠 node_id。

        Args:
            name: 人名。
            existing_ids: 已存在的节点 id 列表(含**所有同名**节点,
                不论 id 形态 —— 声纹锚点 ``person:spk_x`` 也算)。
            force_suffix: 强制带后缀。区分场景用 —— 此时图里已经有同名的人,
                新节点必须显式标明"这是另一个"。

        Returns:
            新的节点 id。
        """
        base = make_name_node_id(name)
        if not force_suffix and base not in existing_ids:
            return base
        n = 2
        while f"{base}#{n}" in existing_ids:
            n += 1
        return f"{base}#{n}"

    @staticmethod
    def is_same_person(a: GraphNode, b: GraphNode) -> bool:
        """判断两个节点是否可能是同一人(保守)。

        判据(任一命中即认为可能):
        - ``user_id`` 相同且非空
        - 归一化后 ``name`` 或 ``aliases`` 有交集

        **这是"可能",不是"确定"** —— 真正的同名判定要问用户。
        """
        if a.user_id and b.user_id and a.user_id == b.user_id:
            return True
        a_names = {normalize_person_name(n) for n in a.all_names()}
        b_names = {normalize_person_name(n) for n in b.all_names()}
        a_names.discard("")
        b_names.discard("")
        return bool(a_names & b_names)


__all__ = ["PersonResolver"]
