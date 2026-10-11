"""5W1H 提炼 + LLM 摘要(Phase 6.3 P1)。

设计要点:
1. **5W1H 适配版**(2026-10-10 决策,不全照搬):
   - what / when / how(what_changed) 必填
   - where / who 可省(无信息就不写,不要写"unknown")
   - why **必须用户原话引述** —— LLM 不准编"为什么"
2. **LLM 摘要降级** —— 调 LLM 失败时,用启发式摘要(取前 200 字符 + 标签拼接)。
3. **why 严格校验** —— LLM 输出含"用户可能..."/"他大概..."等推断语 → 丢弃 why 字段。
4. **本地启发式** —— 不调 LLM,直接 ``"用户提到: " + content[:200]``。

不变量:
- 缺必填字段时,该 fact 不进 consolidation,降级为 raw 短期记忆
- why 永远是字符串(可能为空),不返回 None
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from smartbutler.utils.logging import get_logger

logger = get_logger(__name__)


# LLM 编"为什么" 的典型推断语(用于降级校验)
_INFERENCE_PATTERNS = re.compile(
    r"(用户可能|他大概|可能是因为|也许是因为|看来是|应该是|似乎是|估计是|或许是)"
)


@dataclass
class FiveW1H:
    """5W1H 提炼结果(2026-10-10 决策适配版)。

    - what: 必填
    - when: 必填(可空字符串表示"未指明时间")
    - how / what_changed: 必填
    - where: 可省
    - who: 可省
    - why: 用户原话引述,仅在用户明确说了时填
    """

    what: str
    when: str
    what_changed: str
    where: str = ""
    who: str = ""
    why: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "what": self.what,
            "when": self.when,
            "where": self.where,
            "who": self.who,
            "what_changed": self.what_changed,
            "why": self.why,
        }

    def is_valid(self) -> bool:
        """必填字段非空 = 有效。"""
        return bool(self.what and self.when and self.what_changed)

    def render(self) -> str:
        """渲染成 markdown 摘要(供 recall 输出)。"""
        lines = [
            f"**事件**:{self.what}",
            f"**时间**:{self.when}",
        ]
        if self.where:
            lines.append(f"**地点**:{self.where}")
        if self.who:
            lines.append(f"**人物**:{self.who}")
        if self.what_changed:
            lines.append(f"**变化**:{self.what_changed}")
        if self.why:
            lines.append(f"**原话**:`{self.why}`")
        return "\n".join(lines)


@dataclass
class SummarizedFact:
    """提炼 + 摘要的最终结果,供 consolidation 写入长期存储。"""

    content: str                # 原始 content(原文)
    summary: str                # 提炼后的 markdown 摘要
    importance: float           # 重要性 0-1
    tags: list[str] = field(default_factory=list)
    five_w1h: FiveW1H | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "content": self.content,
            "summary": self.summary,
            "importance": self.importance,
            "tags": list(self.tags),
            "five_w1h": self.five_w1h.to_dict() if self.five_w1h else None,
        }


class FiveW1HExtractor:
    """5W1H 提炼器。

    设计:核心逻辑是启发式从原文抽 6 字段(正则 + 长度截断)。
    LLM 增强版留给 consolidation(LLMSummarizer)调用,本类不做 LLM 调用,纯本地。
    """

    # 时间模式:具体日期 / 时间 / 相对时间
    _RE_TIME = re.compile(
        r"(\d{1,2}月\d{1,2}日|\d{4}-\d{1,2}-\d{1,2}|\d{1,2}/\d{1,2}|"
        r"\d{1,2}:\d{2}|"
        r"(今天|明天|后天|明早|明晚|今晚|昨晚|今早|今晚上午|下午|晚上)|"
        r"(下周|下个月|下月|今年|明年))"
    )
    # 地点关键词
    _RE_WHERE = re.compile(r"在([\u4e00-\u9fa5A-Za-z]{2,10})(开会|住|上班|出差|旅行|旅游|吃饭)")
    # 人物关键词("Alice 说的"/"Bob 喜欢")
    _RE_WHO = re.compile(r"([A-Z][a-zA-Z]+|[\u4e00-\u9fa5]{2,3})(说|告诉|跟|喜欢|想|要)")
    # 用户原话引述
    _RE_QUOTE = re.compile(r"[\"「『'\"'](.+?)[\"」』'\"']")

    def extract(self, content: str) -> FiveW1H:
        """启发式从原文抽 5W1H。

        Args:
            content: 原文。

        Returns:
            :class:`FiveW1H`(必填字段可能为空,调用方用 ``is_valid()`` 判断)。
        """
        if not content or not content.strip():
            return FiveW1H(what="", when="", what_changed="")

        # what: 整句(L1 daily 场景下,整句比截断到 100 字更有用)
        what = content.strip()

        # when: 抽时间模式
        m = self._RE_TIME.search(content)
        when = m.group(0) if m else ""

        # where: 抽地点
        m = self._RE_WHERE.search(content)
        where = m.group(1) if m else ""

        # who: 抽人物
        m = self._RE_WHO.search(content)
        who = m.group(1) if m else ""

        # what_changed: 启发式 = 整句(L1 daily 阶段 what == what_changed)
        what_changed = what

        # why: 抽原话引述
        m = self._RE_QUOTE.search(content)
        why = m.group(1) if m else ""

        return FiveW1H(
            what=what,
            when=when,
            where=where,
            who=who,
            what_changed=what_changed,
            why=why,
        )


class LLMScorer:
    """LLM 评分器 —— 对 consolidation 候选 fact 批评 0-1 分(2026-10-10 设计)。

    设计:
    - P0 阶段不调 LLM,只走 ``HeuristicScorer``(在 ``importance.py``)
    - 6.3 的 LLM 评分是 consolidation 触发时,**只对候选 promote 的 fact** 调
    - 评分维度:
      - 0.0-0.2: 日常琐事(开灯/调温度等无后续影响)
      - 0.2-0.5: 有时间/数字的事实(8 点烧洗澡水)
      - 0.5-0.8: 任务/约定(下周出差/周末聚餐)
      - 0.8-1.0: 持久偏好/关系/约定(用户长期习惯/家庭成员信息)
    - **降级**: LLM 失败 → 返回 None,调用方走启发式兜底

    本类只定义接口 + 降级,不直接 import LLM client(留给 thinking 层或后续实现)。
    """

    async def score_batch(
        self, facts: list[dict[str, Any]]
    ) -> list[dict[str, Any]] | None:
        """对一批 fact 评分。

        Args:
            facts: ``[{"key": ..., "content": ..., "current_importance": ...}, ...]``

        Returns:
            ``[{"key": ..., "importance": 0.X, "reason": "..."}, ...]`` 顺序对齐输入。
            失败/降级时返回 None(调用方走启发式)。
        """
        # P0 阶段不接 LLM,直接返 None 让 calling 走启发式
        # 6.3 实施时这里接 capabilities.llm.BaseLLM(可选)
        logger.debug("LLMScorer.score_batch not implemented yet, returning None")
        return None


class LLMSummarizer:
    """LLM 摘要器 —— 调 LLM 把一批 fact 提炼为 weekly/monthly 摘要(2026-10-10 设计)。

    设计:
    - 输入:一批 fact(原文 + 5W1H)
    - 输出:提炼后的 markdown 摘要(为什么必须原话引述,见 _INFERENCE_PATTERNS)
    - 降级: LLM 失败 → 走启发式摘要(只拼接)
    - why 严格校验: 检出 LLM 编造的"用户可能..." → 丢弃 why 字段
    """

    async def summarize_5w1h(
        self, facts: list[FiveW1H]
    ) -> list[FiveW1H] | None:
        """调 LLM 提炼一批 fact 的 5W1H。

        Args:
            facts: 已用 :class:`FiveW1HExtractor` 抽出的 5W1H 列表(启发式版)。

        Returns:
            LLM 提炼后的 5W1H 列表,顺序对齐输入。失败/降级时返 None。
        """
        logger.debug("LLMSummarizer.summarize_5w1h not implemented yet, returning None")
        return None

    @staticmethod
    def validate_why(why: str) -> str:
        """校验 why 字段:含 LLM 推断语 → 丢弃(返空字符串)。"""
        if not why:
            return ""
        if _INFERENCE_PATTERNS.search(why):
            logger.warning("LLM_inferred_why_dropped content=%s", why[:50])
            return ""
        return why


class HeuristicSummarizer:
    """本地启发式摘要 —— 不调 LLM,直接拼接(降级兜底)。"""

    def summarize(self, facts: list[FiveW1H]) -> list[FiveW1H]:
        """返回原样(5W1H 已经是结构化,不需要再摘要)。

        Args:
            facts: 5W1H 列表。

        Returns:
            同一份 facts(原样返回,L1 daily 写入时直接用)。
        """
        return list(facts)

    def summarize_text(self, contents: list[str]) -> str:
        """纯文本启发式摘要:多 fact 拼接 + 去重。"""
        seen: set[str] = set()
        lines: list[str] = []
        for c in contents:
            snippet = c.strip()
            if not snippet or snippet in seen:
                continue
            seen.add(snippet)
            if len(snippet) > 200:
                snippet = snippet[:197] + "..."
            lines.append(f"- {snippet}")
        return "\n".join(lines)


# 公开 API
__all__ = [
    "FiveW1H",
    "SummarizedFact",
    "FiveW1HExtractor",
    "LLMScorer",
    "LLMSummarizer",
    "HeuristicSummarizer",
]
