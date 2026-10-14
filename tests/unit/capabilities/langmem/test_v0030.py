"""Langmem0030Adapter 单测(Phase 6.2 P0)。"""

from __future__ import annotations

import pytest

from smartbutler.capabilities.langmem import Langmem0030Adapter, LangmemError


def test_v0_0_30_can_be_constructed_if_langmem_installed() -> None:
    """langmem 装了的话,能构造;不装的话抛 LangmemError。"""
    try:
        import langmem  # noqa: F401

        Langmem0030Adapter()
    except ImportError:
        # 没装的话工厂已经降级,这里直接跳过
        pytest.skip("langmem 未安装,跳过构造测试")


@pytest.mark.asyncio
async def test_v0_0_30_extract_raises_for_now() -> None:
    """0.0.30 当前没稳定"只抽事实"API,extract_facts 抛 LangmemError。"""
    try:
        import langmem  # noqa: F401
    except ImportError:
        pytest.skip("langmem 未安装,跳过")

    a = Langmem0030Adapter()
    with pytest.raises(LangmemError):
        await a.extract_facts([{"role": "user", "content": "我住在上海"}])
