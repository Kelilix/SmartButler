"""🆕 Phase 9:记忆图的数据模型。

设计约束(见 ``docs/PHASE_9_GRAPH_MEMORY_DESIGN.md``):

1. **只有 Person 一种节点** —— 本期不做 Organization/Location/Project。
   task 不是世界实体,只做边的溯源属性(``task_ids``)。
2. **节点 id 不可变** —— 绑定声纹时**只补字段**,绝不换 id。
   边永远指向 ``node_id``;若绑定时改 id,所有已有边都要重连。
3. **边是两层** —— ``relation_type``(归一化谓词,给图算法)+
   ``fact``(自然语言完整句,给 embedding 召回和 LLM 理解)。

节点 id 两种形态:

- ``person:<user_id>``      —— 声纹锚定(已绑定声纹)
- ``person:name:<hash>``    —— 名字锚点(未绑定声纹,后续可升级)
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

#: 唯一节点类型(本期收敛范围)
NODE_TYPE_PERSON = "Person"

#: 边类型白名单 —— 仅 Person↔Person
#:
#: 为什么用受控词表而非开放抽取:
#: - 图算法和过滤需要稳定标签
#: - 中文谓词归一化困难("我妈"/"我母亲"/"母亲大人" 是同一个关系)
#: - 词表外的边降级为 ``RELATES_TO``,保留 ``fact`` 句(见 design §3.3)
EDGE_TYPE_MAP: dict[tuple[str, str], list[str]] = {
    ("Person", "Person"): [
        # 家庭
        "IS_FAMILY_OF",
        "IS_MOTHER_OF",
        "IS_FATHER_OF",
        "IS_PARENT_OF",
        "IS_CHILD_OF",
        "IS_SIBLING_OF",
        "IS_SPOUSE_OF",
        "IS_PARTNER_OF",
        # 社交
        "IS_FRIEND_OF",
        "KNOWS",
        # 职场
        "WORKS_WITH",
        "REPORTS_TO",
        "IS_COLLEAGUE_OF",
        "MANAGES",
    ],
}

#: 词表外谓词的降级目标
DEFAULT_EDGE_TYPE = "RELATES_TO"

#: 对称关系 —— 存一条有向边,查询时双向查(design §3.4)
SYMMETRIC_EDGE_TYPES: frozenset[str] = frozenset({
    "IS_FRIEND_OF",
    "KNOWS",
    "WORKS_WITH",
    "IS_SIBLING_OF",
    "IS_SPOUSE_OF",
    "IS_PARTNER_OF",
    "IS_COLLEAGUE_OF",
    "IS_FAMILY_OF",
})


def all_edge_types() -> set[str]:
    """返回全部合法谓词。"""
    out: set[str] = set()
    for types in EDGE_TYPE_MAP.values():
        out.update(types)
    return out


def is_symmetric(relation_type: str) -> bool:
    """判断谓词是否为对称关系。"""
    return relation_type in SYMMETRIC_EDGE_TYPES


# ---------------------------------------------------------------------------
# 名字归一化
# ---------------------------------------------------------------------------

_BRACKET_RE = re.compile(r"[（(].*?[)）]")
_WHITESPACE_RE = re.compile(r"\s+")


def normalize_person_name(name: str) -> str:
    """归一化人名(轻量、纯字符串、零 LLM)。

    做的处理:
    - 全角转半角
    - 去所有空白
    - 去括号修饰:"李雷(同事)" → "李雷"
    - 小写

    **刻意保守** —— "小李" 与 "李雷" 归一化后仍不同。
    过度激进会把不同人合并(关系错连比名字重复危害大得多)。
    靠别名链 + 声纹绑定来收敛同一人的多种称呼。

    Args:
        name: 原始人名。

    Returns:
        归一化后的名字(可能为空字符串)。
    """
    if not name:
        return ""
    s = name.strip()
    if not s:
        return ""
    # 全角 → 半角
    s = "".join(
        chr(ord(ch) - 0xFEE0) if 0xFF01 <= ord(ch) <= 0xFF5E else ch for ch in s
    )
    s = _BRACKET_RE.sub("", s)
    s = _WHITESPACE_RE.sub("", s)
    return s.lower()


def make_name_node_id(name: str) -> str:
    """生成名字锚定的节点 id。

    ``person:name:<sha1(normalize(name))[:16]>``

    同名 → 同一 id(跨 task 稳定)。
    """
    normalized = normalize_person_name(name)
    digest = hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:16]
    return f"person:name:{digest}"


def make_voice_node_id(user_id: str) -> str:
    """生成声纹锚定的节点 id。"""
    return f"person:{user_id}"


def make_edge_id(
    source_id: str,
    relation_type: str,
    target_id: str,
    fact: str,
) -> str:
    """生成边 id(幂等基石)。

    ``sha256(source_id | relation_type | target_id | normalize(fact))``

    同源同目标同事实 → 同 id → 判重命中时**不新增**,只追加 ``task_ids``。
    """
    normalized_fact = "".join(fact.split()).lower()
    raw = f"{source_id}|{relation_type}|{target_id}|{normalized_fact}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------


@dataclass
class GraphNode:
    """人物节点。

    ``node_id`` 一旦创建**永不改变** —— 绑定声纹只补 ``user_id`` /
    ``voice_bound`` / ``aliases``。
    """

    node_id: str
    name: str
    aliases: list[str] = field(default_factory=list)
    type: str = NODE_TYPE_PERSON
    user_id: str | None = None
    voice_bound: bool = False
    summary: str = ""
    created_at: str = ""
    updated_at: str = ""

    def all_names(self) -> list[str]:
        """主名 + 所有别名(去重,保持顺序)。"""
        out: list[str] = []
        for n in [self.name, *self.aliases]:
            if n and n not in out:
                out.append(n)
        return out

    def to_row(self) -> tuple:
        return (
            self.node_id,
            self.name,
            json.dumps(self.aliases, ensure_ascii=False),
            self.type,
            self.user_id,
            1 if self.voice_bound else 0,
            self.summary,
            self.created_at,
            self.updated_at,
        )


@dataclass
class GraphEdge:
    """人物关系边(Person↔Person)。

    时态四时间戳(design §4.1):
    - ``valid_at``   —— 世界时间:关系在现实中成立的时间(复用 fact 的 valid_at)
    - ``invalid_at`` —— 世界时间:关系在现实中终止的时间(Step 3.5 写)
    - ``created_at`` —— 系统时间:边写入系统的时间
    - ``expired_at`` —— 系统时间:边在系统中被替换的时间(Step 3.5 写)
    """

    edge_id: str
    source_id: str
    target_id: str
    relation_type: str
    fact: str
    speaker: str = ""
    valid_at: str | None = None
    invalid_at: str | None = None
    created_at: str = ""
    expired_at: str | None = None
    task_ids: list[str] = field(default_factory=list)
    embedding: bytes | None = None

    @property
    def is_active(self) -> bool:
        """边是否活跃(未被矛盾消解)。"""
        return not self.invalid_at

    def to_row(self) -> tuple:
        return (
            self.edge_id,
            self.source_id,
            self.target_id,
            self.relation_type,
            self.fact,
            self.speaker,
            self.valid_at,
            self.invalid_at,
            self.created_at,
            self.expired_at,
            json.dumps(self.task_ids, ensure_ascii=False),
            self.embedding,
        )


@dataclass(frozen=True)
class GraphEdgeCandidate:
    """LLM 抽出的边候选(尚未解析成 node_id)。

    ``source`` / ``target`` 是**人名**,由 resolver 映射成 node_id。
    """

    source: str
    relation_type: str
    target: str
    fact: str
    fact_ref: str = ""

    def key(self) -> tuple[str, str, str]:
        """批内消解用的比较键(不含 fact —— 判重时还要比 fact 句)。"""
        return (self.source, self.relation_type, self.target)


__all__ = [
    "DEFAULT_EDGE_TYPE",
    "EDGE_TYPE_MAP",
    "NODE_TYPE_PERSON",
    "SYMMETRIC_EDGE_TYPES",
    "GraphEdge",
    "GraphEdgeCandidate",
    "GraphNode",
    "all_edge_types",
    "is_symmetric",
    "make_edge_id",
    "make_name_node_id",
    "make_voice_node_id",
    "normalize_person_name",
]
