"""langmem 抽象层工厂(Phase 6.2 P0)。

优先级:
1. ``Langmem0030Adapter``(真能初始化的话)
2. ``RegexFactExtractor``(langmem 装不上 / 0.0.30 抽不出时)

调用方拿到的总是 ``BaseLangmemAdapter`` 实例,无感知。
"""

from __future__ import annotations

from smartbutler.capabilities.langmem.base import (
    BaseLangmemAdapter,
    LangmemError,
)
from smartbutler.capabilities.langmem.fallback import RegexFactExtractor
from smartbutler.capabilities.langmem.v0030 import Langmem0030Adapter
from smartbutler.utils.logging import get_logger

logger = get_logger(__name__)


def build_default_adapter() -> BaseLangmemAdapter:
    """按可用性选实现。

    0.0.30 暂时**永远**走 fallback(因为 0.0.30 没有"只抽事实不存"的稳定 API,
    强行用会抛 LangmemError),后续 0.1.x 稳定后改这里优先 0.1.x。
    """
    try:
        adapter: BaseLangmemAdapter = Langmem0030Adapter()
        # 试探一下能否抽事实(0.0.30 当前会抛 LangmemError,自然走 fallback)
        # 实际抽是 async,这里不能 await;改成"导入即验"
        logger.info("langmem.factory.v0_0_30_available")
        # 但已知 0.0.30 抽不出事 → 直接走 fallback,避免上层第一次调用才失败
        return RegexFactExtractor()
    except LangmemError as e:
        logger.warning(
            "langmem.factory.v0_0_30_unavailable,fallback to RegexFactExtractor",
            error=str(e),
        )
        return RegexFactExtractor()


__all__ = ["build_default_adapter"]
