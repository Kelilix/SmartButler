"""🆕 Phase 9:记忆图(graph memory)。

包结构(遵循现有 ``emotion/memory`` 的组织方式):

- ``models.py``   —— 数据模型 + 谓词词表 + 名字归一化
- ``store.py``    —— SQLite 存储层
- ``resolver.py`` —— 人物节点解析(三路径)
- ``extractor.py``—— LLM 抽取(节点+边一次出)
- ``binding.py``  —— 声纹绑定 + 同名消歧
- ``hooks.py``    —— 抽取钩子(挂 ``ON_TASK_COMPLETE``)

设计文档: ``docs/PHASE_9_GRAPH_MEMORY_DESIGN.md``
路线图:   ``docs/PHASE_9_GRAPH_MEMORY_ROADMAP.md``
"""

from smartbutler.emotion.memory.graph.models import (
    DEFAULT_EDGE_TYPE,
    EDGE_TYPE_MAP,
    NODE_TYPE_PERSON,
    SYMMETRIC_EDGE_TYPES,
    GraphEdge,
    GraphEdgeCandidate,
    GraphNode,
    all_edge_types,
    is_symmetric,
    make_edge_id,
    make_name_node_id,
    make_voice_node_id,
    normalize_person_name,
)
from smartbutler.emotion.memory.graph.store import GraphStore

__all__ = [
    "DEFAULT_EDGE_TYPE",
    "EDGE_TYPE_MAP",
    "NODE_TYPE_PERSON",
    "SYMMETRIC_EDGE_TYPES",
    "GraphEdge",
    "GraphEdgeCandidate",
    "GraphNode",
    "GraphStore",
    "all_edge_types",
    "is_symmetric",
    "make_edge_id",
    "make_name_node_id",
    "make_voice_node_id",
    "normalize_person_name",
]
