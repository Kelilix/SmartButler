"""重要性评分(Phase 6.2 P0)。

策略:
- **P0 阶段不调 LLM**(6.2 阶段 recall 路径全本地,避免增加 LLM 调用成本)
- 只用 ``HeuristicScorer``(本地启发式)
- 6.3 接 consolidation 时,这里加 LLM 评分逻辑(只对候选 promote 的 fact 评)

启发式规则:
- 含数字 / 日期 / 姓名: +0.3
- 长度 > 100: +0.2
- 含情绪词(喜欢/讨厌/想/要): +0.2
- 上限 1.0,下限 0.0
"""

from __future__ import annotations

import re

from smartbutler.utils.logging import get_logger

logger = get_logger(__name__)

# 数字 / 日期 / 姓名: 简化,数字 = 任何阿拉伯数字,日期 = X月X日/X-X,姓名 = 2-4 字中文
_RE_HAS_NUMBER = re.compile(r"\d")
_RE_HAS_DATE = re.compile(r"(\d{1,2}月\d{1,2}日|\d{4}-\d{1,2}-\d{1,2}|\d{1,2}/\d{1,2})")
_RE_HAS_NAME = re.compile(r"[\u4e00-\u9fa5]{2,4}")
# 情绪词
_RE_HAS_EMOTION = re.compile(
    r"(喜欢|讨厌|爱|不爱|想|不想|要|不要|希望|希望是|期待|害怕|担心|高兴|开心|难过|生气)"
)


class HeuristicScorer:
    """本地启发式评分,0-1。"""

    def score(self, content: str) -> float:
        if not content or not content.strip():
            return 0.5  # 空内容给中位,跟"默认 0.5"一致
        score = 0.5
        if _RE_HAS_NUMBER.search(content) or _RE_HAS_DATE.search(content):
            score += 0.3
        if _RE_HAS_NAME.search(content):
            score += 0.1
        if len(content) > 100:
            score += 0.2
        if _RE_HAS_EMOTION.search(content):
            score += 0.2
        # 限幅
        return max(0.0, min(1.0, score))


class ImportanceScorer:
    """重要性评分门面。P0 只走 HeuristicScorer。"""

    def __init__(self, llm: object | None = None) -> None:  # noqa: ARG002
        # llm 参数保留接口,6.3 接 consolidation 时用
        self._heuristic = HeuristicScorer()

    async def score(self, content: str) -> float:
        """评 0-1 分。P0 阶段不调 LLM,只用启发式。"""
        s = self._heuristic.score(content)
        return s


__all__ = ["HeuristicScorer", "ImportanceScorer"]
