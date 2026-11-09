"""🆕 Phase 9:``resolve_person`` tool —— 人物登记与关系建立。

## 工具的三态返回

| status | 含义 | LLM 该做什么 |
|--------|------|-------------|
| ``ok`` | 已建/复用,完成 | 直接回复用户 |
| ``needs_confirmation`` | 图里已有同名的人 | **先向用户确认身份**,拿到答案再调一次 |
| ``error`` | 失败 | 降级,不阻塞对话 |

## 两步调用协议

```
第一轮:resolve_person(display_name="张伟", relation_type="IS_COLLEAGUE_OF",
                      speaker=<用户user_id>)
       → {"status": "needs_confirmation",
          "candidates": [{"node_id": "...", "name": "张伟", "summary": "..."}]}
       → LLM 问用户:"是不是这个人:张伟,我的同事,在XX公司?"
                ↓
第二轮(用户答"是"):
       resolve_person(..., candidate_node_id="person:xxx")
       → {"status": "ok", ...}

第二轮(用户答"不是" / "同名"):
       resolve_person(...)            ← **不传 candidate_node_id**
       → 自动建新节点区分
```

## 为什么确认由 LLM 触发而不是代码

**分工**:代码提供"查询/登记 + 建边"的能力;LLM 决定**何时**问用户。

- 澄清话术由 LLM 生成,贴合上下文(代码写不出自然的话)
- 代码不需要写状态机追踪"待澄清的指代"
- 与既有 tool 机制(``enroll_speaker`` / ``decline_speaker_enrollment``)一致

> 参考:``enroll_speaker`` 的 ``confirm`` 硬门槛是**代码**强制(涉及生物特征
> 隐私必须显式同意);这里只是**关系登记**,不涉及隐私,所以由 LLM 判断时机。

Author: SmartButler Team
Date: 2026-11-09
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from smartbutler.capabilities.tools.decorator import register_tool
from smartbutler.capabilities.tools.types import ToolScope
from smartbutler.utils.logging import get_logger

if TYPE_CHECKING:
    from smartbutler.emotion.memory.graph.store import GraphStore

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# 依赖注入(模块级全局,由 wiring 注入)
# ---------------------------------------------------------------------------

_graph_store: GraphStore | None = None
_resolver: Any | None = None
_writer: Any | None = None


def set_graph_dependencies(
    *,
    store: GraphStore,
    resolver: Any,
    writer: Any,
) -> None:
    """由 wiring 层注入图相关依赖。

    Args:
        store: 图存储。
        resolver: 人物节点解析器。
        writer: 图落库器。
    """
    global _graph_store, _resolver, _writer
    _graph_store = store
    _resolver = resolver
    _writer = writer
    logger.info("resolve_person.deps_injected store=%s", store.path)


def clear_graph_dependencies() -> None:
    """清空依赖(测试用)。"""
    global _graph_store, _resolver, _writer
    _graph_store = None
    _resolver = None
    _writer = None


# ---------------------------------------------------------------------------
# Tool 实现
# ---------------------------------------------------------------------------


@register_tool(
    name="resolve_person",
    description=(
        "登记或查询一个人,并建立人与人之间的关系。\n"
        "场景:用户提到某个人(如'我妈''我同事李雷'),"
        "当需要建立人际关系、或系统不知道这个人是谁时调用。\n"
        "参数:\n"
        "  display_name      —— 对方的名字(必填)\n"
        "  relation_type     —— 关系类型(必填),如 IS_MOTHER_OF / IS_FRIEND_OF / "
        "WORKS_WITH / IS_FATHER_OF / IS_COLLEAGUE_OF / KNOWS\n"
        "  speaker           —— 说话人的 user_id(从上下文取,必填)\n"
        "  candidate_node_id —— 可选。**第二轮确认时传**:用户已确认是哪一个候选人\n"
        "返回值 status=needs_confirmation 表示图里已有同名的人,"
        "请把 candidates 里的描述转述给用户听(如'是不是这个人:张伟,我的同事?'),"
        "等用户回答后再调用一次。\n"
        "  - 用户说'是' → 带上 candidate_node_id 再调一次\n"
        "  - 用户说'不是'/'同名' → **不要**带 candidate_node_id,直接再调一次"
        "(系统会自动建新节点区分这两个人)"
    ),
    scope=ToolScope.GLOBAL,
    timeout_seconds=10.0,
    idempotent=True,
)
async def resolve_person(
    display_name: str,
    relation_type: str,
    speaker: str,
    candidate_node_id: str | None = None,
) -> dict[str, Any]:
    """登记人物 + 建立人际关系(三态)。

    Args:
        display_name: 对方的名字。
        relation_type: 关系类型(大写下划线)。
        speaker: 说话人的 user_id。
        candidate_node_id: 第二轮确认时传 —— 用户选定的候选节点 id。

    Returns:
        见模块 docstring 的三态表。
    """
    if _graph_store is None or _resolver is None or _writer is None:
        logger.warning("resolve_person.no_deps")
        return {
            "status": "error",
            "message": "记忆图功能未启用",
        }

    name = (display_name or "").strip()
    if not name:
        return {"status": "error", "message": "display_name 不能为空"}

    rel = (relation_type or "").strip().upper()
    if not rel:
        return {"status": "error", "message": "relation_type 不能为空"}

    if not speaker:
        return {"status": "error", "message": "speaker 不能为空"}

    try:
        # ---- 第二轮:用户已确认是哪个候选人 ----
        if candidate_node_id:
            node = _graph_store.get_node(candidate_node_id)
            if node is None:
                return {
                    "status": "error",
                    "message": f"候选节点不存在: {candidate_node_id}",
                }
            target_id = candidate_node_id
            known = True
        else:
            # ---- 第一轮:解析 / 消歧 ----
            #
            # ⚠️ **同名候选 ≠ 一定要问** —— 要区分两种情况:
            #
            # 1. 候选**已经和 speaker 有边**(或关系类型一致)→ 很可能就是同一个人
            #    (用户之前就提过"我同事张伟",现在又说"张伟帮我改个方案")
            #    → 直接复用,不再打扰用户
            # 2. 候选**和 speaker 没有边** → 可能是另一个人(同名)
            #    → 询问用户
            #
            # 不区分的话,用户每提一次"张伟"就要被问一次"是不是这个人",
            # 第二次以后就是纯噪音。
            all_candidates = _resolver.find_candidates(name)
            reusable = _filter_reusable(all_candidates, speaker, rel)
            if reusable:
                # 命中"同一个人" → 直接复用,不问
                target_id = reusable[0].node_id
                known = True
            elif all_candidates:
                # ⚠️ 同名但不是同一个人 → 需要向用户确认(不建边)
                payload = [
                    {
                        "node_id": c.node_id,
                        "name": c.name,
                        "summary": c.summary,
                        "voice_bound": c.voice_bound,
                    }
                    for c in all_candidates
                ]
                logger.info(
                    "resolve_person.needs_confirmation name=%s candidates=%s",
                    name, len(payload),
                )
                return {
                    "status": "needs_confirmation",
                    "display_name": name,
                    "candidates": payload,
                    "message": (
                        f"图里已有名为'{name}'的人,请先向用户确认是哪一个。"
                    ),
                }
            else:
                node = await _resolver.resolve(name)
                if node is None:
                    return {"status": "error", "message": "无法解析该人物"}
                target_id = node.node_id
                known = False

        # ---- 建节点 + 建边 ----
        edge_created = await _build_edge(
            speaker=speaker,
            target_id=target_id,
            relation_type=rel,
            display_name=name,
        )

        target = _graph_store.get_node(target_id)
        return {
            "status": "ok",
            "person": {
                "node_id": target_id,
                "name": target.name if target else name,
                "voice_bound": bool(target.voice_bound) if target else False,
            },
            "edge_created": edge_created,
            "known": known,
        }

    except Exception as e:  # noqa: BLE001
        logger.warning("resolve_person.failed name=%s err=%s", name, e)
        return {"status": "error", "message": f"操作失败: {e}"}


def _filter_reusable(
    candidates: list[Any],
    speaker: str,
    relation_type: str,
) -> list[Any]:
    """从同名候选里挑出"很可能就是同一个人"的。

    判据(任一命中即认为可复用):
    1. 候选节点**已经和 speaker 有边** → 用户之前提过这个人
    2. 该边的关系类型**与本次相同** → 明显是同一个人

    为什么需要这个区分:
    - "我同事张伟" → "张伟帮我改方案" → 应该是同一个人,**复用**
    - "我同事张伟" → "客户张伟" → 可能是另一个人,**要问**

    不区分的话,用户每提一次"张伟"就要被问一次"是不是这个人",
    第二次以后就是纯噪音 —— 用户会直接忽略,机制就废了。

    Args:
        candidates: 同名候选节点列表。
        speaker: 说话人 user_id。
        relation_type: 本次要建立的关系类型。

    Returns:
        可复用的候选子集(空列表 = 都需要问用户)。
    """
    if _graph_store is None:
        return []
    reusable: list[Any] = []
    for cand in candidates:
        edges = _graph_store.find_edges_for_node(cand.node_id, only_active=True)
        for e in edges:
            if e.source_id == f"person:{speaker}" or e.target_id == f"person:{speaker}":
                # 已有 speaker 的边 —— 是同一个人
                reusable.append(cand)
                break
            if e.relation_type == relation_type:
                # 关系类型一致 —— 很可能是同一个人
                reusable.append(cand)
                break
    return reusable


async def _build_edge(    *,
    speaker: str,
    target_id: str,
    relation_type: str,
    display_name: str,
) -> bool:
    """建边(幂等)。

    Args:
        speaker: 说话人 user_id。
        target_id: 目标节点 id。
        relation_type: 关系类型。
        display_name: 目标名字(生成 fact 句用)。

    Returns:
        True = 新建了边;False = 边已存在(幂等)。
    """
    from smartbutler.emotion.memory.graph.models import GraphEdge, GraphNode, make_edge_id

    # 源节点:说话人对应的节点(找不到就按 user_id 建一个)
    source = _graph_store.find_node_by_user_id(speaker)
    if source is None:
        source_id = f"person:{speaker}"
        _graph_store.upsert_node(
            GraphNode(node_id=source_id, name="我", user_id=speaker, voice_bound=True)
        )
    else:
        source_id = source.node_id

    # ⚠️ 目标节点必须先落库 —— 否则"边"指向一个不存在的节点,
    # 下一轮 find_candidates 就找不到它,同一个人会被当成陌生人重新建。
    if _graph_store.get_node(target_id) is None:
        _graph_store.upsert_node(GraphNode(node_id=target_id, name=display_name))

    fact = f"{display_name}和我的关系是{relation_type}"
    edge_id = make_edge_id(source_id, relation_type, target_id, fact)
    edge = GraphEdge(
        edge_id=edge_id,
        source_id=source_id,
        target_id=target_id,
        relation_type=relation_type,
        fact=fact,
        speaker=speaker,
        created_at=_now_iso(),
        task_ids=[],
    )
    return _graph_store.upsert_edge(edge)


def _now_iso() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()


__all__ = [
    "clear_graph_dependencies",
    "resolve_person",
    "set_graph_dependencies",
]
