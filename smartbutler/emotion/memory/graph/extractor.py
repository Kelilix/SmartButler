"""🆕 Phase 9:LLM 抽取 —— 一次调用产出人物节点 + 人物关系边。

对齐 Graphiti 的 ``extract_nodes_and_edges`` combined prompt(节点+边合并成
**1 次 LLM 调用**,省一次往返)。

**代码 vs LLM 的分工**(design §3.2):

> **LLM 提议,代码裁决并落库。**

代码硬约束三件事(LLM 不可绕过):

1. **端点必须存在** —— 边的 source/target 必须命中本次抽出的人物列表,
   否则丢弃(防 LLM 编造端点)
2. **谓词必须过白名单** —— 不在词表 → 降级为 ``RELATES_TO``,保留 fact 句
3. **端点必须是 Person** —— 本期唯一类型,任何非 Person 端点 → **整条边丢弃**

降级原则:图是**增强层**,任何环节失败都不能影响主对话流程。
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from typing import Any

from smartbutler.emotion.memory.graph.models import (
    DEFAULT_EDGE_TYPE,
    NODE_TYPE_PERSON,
    GraphEdgeCandidate,
    all_edge_types,
)
from smartbutler.utils.logging import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

_EXTRACT_PROMPT = """你是人物关系抽取器。从对话中抽取**人**和**人际关系**。

## 硬规则(必须严格遵守)

1. ALWAYS extract the speaker as a Person entity.
2. **只抽人**。不要抽公司、地点、物品、事件、概念作为实体。
3. 亲属称谓(妈妈/爸爸/奶奶/外公/婆婆等)也是 Person 实体,
   亲属关系表达为**边**,不要把"我妈"当成一个抽象标签。
4. DO NOT create nodes for relationships or actions.
   (不要为"是朋友"这类关系本身建节点)
5. DO NOT create nodes for temporal information.
   (不要为"明天""去年"这类时间建节点)
6. Each fact must relate **two DISTINCT** Person nodes.
   (不能自己跟自己有关系)
7. ``relation_type`` 必须从下面的词表里选,SCREAMING_SNAKE_CASE 格式:
{predicate_list}
8. 边的两个端点必须**都出现在** nodes 列表里。
9. 每条边标注 ``fact_ref``:它依据的是哪一条输入事实(填事实的序号,1-based)。
10. 涉及非人实体的关系**不要建边**。
    例:"我妈明天去上海" → 只建 `我 IS_MOTHER_OF 妈妈`,
    不要建 `妈妈 TRAVELED_TO 上海`。
11. ``fact`` 必须是**自然语言完整句**,保留原始细节
    (如"张女士是我最好的朋友,认识十年了"而不是"张女士是我朋友")。

## 输出格式

严格 JSON,不要任何解释文字:

{{
  "nodes": [
    {{"name": "我", "type": "Person"}},
    {{"name": "张女士", "type": "Person"}}
  ],
  "edges": [
    {{
      "source": "我",
      "relation_type": "IS_FRIEND_OF",
      "target": "张女士",
      "fact": "张女士是我最好的朋友,认识十年了",
      "fact_ref": "1"
    }}
  ]
}}

## 待抽取内容

{facts_block}
"""


def _build_prompt(facts_block: str) -> str:
    """构造 prompt。

    ⚠️ **不能用 ``.format()``** —— prompt 里有 JSON 示例(含花括号),
    ``.format()`` 会把 ``{{`` 当占位符。用显式 ``.replace()``。
    """
    predicates = "\n".join(f"  - {t}" for t in sorted(all_edge_types()))
    prompt = _EXTRACT_PROMPT.replace("{predicate_list}", predicates)
    return prompt.replace("{facts_block}", facts_block)


# ---------------------------------------------------------------------------
# 结果模型
# ---------------------------------------------------------------------------


@dataclass
class ExtractionResult:
    """一次抽取的结果。"""

    nodes: list[str] = field(default_factory=list)
    edges: list[GraphEdgeCandidate] = field(default_factory=list)
    dropped_invalid_endpoint: int = 0
    dropped_unknown_predicate: int = 0
    dropped_non_person: int = 0

    @property
    def is_empty(self) -> bool:
        return not self.edges and not self.nodes


@dataclass
class GraphExtractor:
    """LLM 人物关系抽取器。

    用法::

        ex = GraphExtractor(llm)
        result = await ex.extract(facts=[("我", "张女士是我朋友")])
    """

    llm: Any | None = None
    max_chars: int = 4000
    timeout_seconds: float = 30.0
    max_tokens: int = 800
    max_edges: int = 20

    @property
    def enabled(self) -> bool:
        """是否启用了 LLM。"""
        return self.llm is not None

    async def extract(
        self,
        facts: list[tuple[str, str]],
        *,
        messages: str = "",
    ) -> ExtractionResult:
        """抽取人物节点 + 关系边。

        Args:
            facts: ``[(speaker, fact_text), ...]`` —— 骨架(谁说了什么)。
            messages: 原始消息文本(细节)。两者拼接后送 LLM(决策① A3)。
            max_edges 已通过构造参数设定。

        Returns:
            :class:`ExtractionResult`。失败时返回空结果(不抛异常)。
        """
        if self.llm is None or not facts:
            return ExtractionResult()

        facts_block = self._build_facts_block(facts, messages)
        if not facts_block.strip():
            return ExtractionResult()

        prompt = _build_prompt(facts_block)
        raw = await self._call_llm(prompt)
        if raw is None:
            return ExtractionResult()

        parsed = _parse_json(raw)
        if parsed is None:
            logger.warning("graph_extractor.bad_response raw=%s", raw[:200])
            return ExtractionResult()

        return self._validate(parsed)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _build_facts_block(
        self,
        facts: list[tuple[str, str]],
        messages: str,
    ) -> str:
        """拼输入:fact 骨架 + 原始消息细节。"""
        lines: list[str] = []
        for i, (speaker, text) in enumerate(facts, start=1):
            who = speaker or "unknown"
            lines.append(f"[事实 {i}] 说话人={who}: {text}")
        block = "\n".join(lines)
        if messages:
            block = f"{block}\n\n[原始对话]\n{messages}"
        return block[: self.max_chars]

    async def _call_llm(self, prompt: str) -> str | None:
        """调 LLM,失败返回 None。"""
        try:
            from smartbutler.capabilities.llm.types import Message

            resp = await asyncio.wait_for(
                self.llm.chat(
                    [
                        Message(
                            role="system",
                            content=(
                                "你只做人物关系抽取,输出严格 JSON,不要任何解释文字。"
                            ),
                        ),
                        Message(role="user", content=prompt),
                    ],
                    temperature=0.0,
                    max_tokens=self.max_tokens,
                ),
                timeout=self.timeout_seconds,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("graph_extractor.llm_failed err=%s", e)
            return None
        return getattr(resp, "content", "") or ""

    def _validate(self, parsed: dict[str, Any]) -> ExtractionResult:
        """代码硬校验(LLM 提议 → 代码裁决)。"""
        result = ExtractionResult()

        # ---- 节点 ----
        raw_nodes = parsed.get("nodes")
        node_names: set[str] = set()
        if isinstance(raw_nodes, list):
            for item in raw_nodes:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name", "")).strip()
                if not name:
                    continue
                # 类型校验:非 Person 的节点直接丢弃(本期只有 Person)
                node_type = str(item.get("type", NODE_TYPE_PERSON)).strip().upper()
                if node_type and node_type != NODE_TYPE_PERSON.upper():
                    logger.info(
                        "graph_extractor.drop_non_person_node name=%s type=%s",
                        name, node_type,
                    )
                    continue
                if name not in node_names:
                    node_names.add(name)
                    result.nodes.append(name)

        # ---- 边 ----
        raw_edges = parsed.get("edges")
        if not isinstance(raw_edges, list):
            return result

        valid_types = all_edge_types()
        seen: set[tuple[str, str, str]] = set()

        for item in raw_edges:
            if not isinstance(item, dict):
                continue
            source = str(item.get("source", "")).strip()
            target = str(item.get("target", "")).strip()
            fact = str(item.get("fact", "")).strip()
            relation = str(item.get("relation_type", "")).strip().upper()

            # 端点必须在本次抽出的人物列表里(防 LLM 编造端点)
            if source not in node_names or target not in node_names:
                result.dropped_invalid_endpoint += 1
                logger.info(
                    "graph_extractor.drop_endpoint src=%s tgt=%s (不在人物列表)",
                    source, target,
                )
                continue

            # 不能自己跟自己有关系
            if source == target:
                result.dropped_invalid_endpoint += 1
                continue

            # 谓词白名单 —— 不在词表降级为 RELATES_TO(保留 fact 句)
            if relation not in valid_types:
                result.dropped_unknown_predicate += 1
                logger.info(
                    "graph_extractor.degrade_predicate %s → %s", relation,
                    DEFAULT_EDGE_TYPE,
                )
                relation = DEFAULT_EDGE_TYPE

            if not fact:
                fact = f"{source} {relation} {target}"

            key = (source, relation, target)
            if key in seen:
                continue
            seen.add(key)

            result.edges.append(
                GraphEdgeCandidate(
                    source=source,
                    relation_type=relation,
                    target=target,
                    fact=fact,
                    fact_ref=str(item.get("fact_ref", "")).strip(),
                )
            )

            if len(result.edges) >= self.max_edges:
                logger.warning(
                    "graph_extractor.truncated max_edges=%s", self.max_edges,
                )
                break

        return result


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------


def _parse_json(raw: str) -> dict[str, Any] | None:
    """从 LLM 输出里抠出 JSON 对象(容错围栏 / 前后解释文字)。"""
    if not raw:
        return None
    text = raw.strip()
    # 剥 markdown 围栏
    text = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", text)
    text = re.sub(r"\s*```$", "", text).strip()

    # 直接解析
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except (TypeError, ValueError):
        pass

    # 抠出第一个 {...} 平衡括号块
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


__all__ = ["ExtractionResult", "GraphExtractor"]
