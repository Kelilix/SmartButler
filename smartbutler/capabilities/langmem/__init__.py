"""langmem 抽象层(Phase 6.2 P0)。

对外暴露:
- ``BaseLangmemAdapter``: ABC,业务层只 import 这个
- ``Langmem0030Adapter``: 具体实现(软依赖 ``langmem>=0.0.30,<0.1``)
- ``RegexFactExtractor``: langmem 装不上时的降级(正则抽事实)
- ``build_default_adapter``: 工厂,自动选实现

设计要点:
1. **业务层零感知**: emotion/memory 只 import ``BaseLangmemAdapter``
2. **软依赖**: ``langmem`` 装不上时 try-except,自动切 ``_fallback``
3. **API 稳定**: langmem 升级只改 ``_v0_0_30.py`` → 后续 ``_v0_1_x.py``
"""

from __future__ import annotations

from smartbutler.capabilities.langmem.base import (
    BaseLangmemAdapter,
    ExtractedFact,
    LangmemError,
)
from smartbutler.capabilities.langmem.factory import build_default_adapter
from smartbutler.capabilities.langmem.fallback import RegexFactExtractor
from smartbutler.capabilities.langmem.v0030 import Langmem0030Adapter

__all__ = [
    "BaseLangmemAdapter",
    "ExtractedFact",
    "LangmemError",
    "Langmem0030Adapter",
    "RegexFactExtractor",
    "build_default_adapter",
]
