"""langmem 抽象层(Phase 6.2 P0)。

``BaseLangmemAdapter`` 是业务层唯一依赖,具体实现藏在 ``_v0_0_30.py`` 或 ``_fallback.py``。
后续 langmem 升级只新增 ``_v0_1_x.py`` + 改工厂,业务层零改。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, field


class LangmemError(Exception):
    """langmem 子能力任何错误都包成这个。"""


@dataclass(frozen=True)
class ExtractedFact:
    """从对话中抽出的一条事实。

    Attributes:
        content: 事实文本(如"用户叫 Alice"、"用户喜欢喝咖啡")。
        fact_type: 类别(name / preference / event / other),可空字符串。
        confidence: 0-1,提取可信度(可选,降级路径用 1.0)。
    """

    content: str
    fact_type: str = ""
    confidence: float = 1.0


class BaseLangmemAdapter(ABC):
    """langmem 抽象层(业务层只 import 这个)。"""

    @abstractmethod
    async def extract_facts(
        self, conversation: Sequence[dict[str, str]]
    ) -> list[ExtractedFact]:
        """从一段对话中抽取事实。

        Args:
            conversation: ``[{"role": "user", "content": "..."}, ...]`` 格式。

        Returns:
            事实列表。空对话或抽取失败返回空 list。
        """
        ...

    async def aclose(self) -> None:
        """可选:释放资源。默认 no-op。"""
        return None


__all__ = ["BaseLangmemAdapter", "ExtractedFact", "LangmemError"]
