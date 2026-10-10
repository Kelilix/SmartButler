"""正则抽事实 fallback(Phase 6.2 P0)。

langmem 装不上 / 0.0.30 API 不顺手时,本类接管。
质量低(只能匹配触发词句式),但 0 依赖、不崩。

**这是纯基础能力**(只 import 标准库 + 业务无关),所以放在
``capabilities/langmem/`` 而非 ``emotion/memory/``。
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from smartbutler.capabilities.langmem.base import (
    BaseLangmemAdapter,
    ExtractedFact,
)


# 触发词模式:(fact_type, 正则) — 模式只匹配"中文"场景
_TRIGGER_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "name",
        re.compile(
            r"(我(?:叫|是|的名字叫|的全名是)\s*([^\s,.。!！?？,]{1,30}))"
        ),
    ),
    (
        "location",
        re.compile(
            r"(我(?:住在|在|的家在|现在在)\s*([^\s,.。!！?？,]{1,50}))"
        ),
    ),
    (
        "preference",
        re.compile(
            r"(我(?:喜欢|爱|常喝|常吃|习惯|常用|讨厌|不喜欢)\s*"
            r"([^\s,.。!！?？,]{1,30}))"
        ),
    ),
    (
        "family",
        re.compile(
            r"(我(?:老婆|老公|爱人|女朋友|男朋友|孩子|儿子|女儿|爸|妈|父亲|母亲)"
            r"(?:叫|是)?\s*([^\s,.。!！?？,]{0,30}))"
        ),
    ),
    (
        "date",
        re.compile(
            r"((?:明天|后天|大后天|下周[一二三四五六日天]?|[0-9]{1,2}月[0-9]{1,2}日?)"
            r"(?:[^,.。!！?？\n]{0,30}))"
        ),
    ),
)


class RegexFactExtractor(BaseLangmemAdapter):
    """正则抽事实 fallback。"""

    async def extract_facts(
        self, conversation: Sequence[dict[str, str]]
    ) -> list[ExtractedFact]:
        if not conversation:
            return []
        # 只看 user 消息(assistant 内容一般不作为"事实")
        user_texts = [
            (m.get("content") or "")
            for m in conversation
            if m.get("role") == "user"
        ]
        facts: list[ExtractedFact] = []
        for text in user_texts:
            if not text:
                continue
            for fact_type, pattern in _TRIGGER_PATTERNS:
                for m in pattern.finditer(text):
                    # 取整句触发,不是只 group(2);部分触发词无 group(2)
                    full = m.group(0).strip()
                    if full and full not in {f.content for f in facts}:
                        facts.append(
                            ExtractedFact(content=full, fact_type=fact_type)
                        )
        return facts


__all__ = ["RegexFactExtractor"]
