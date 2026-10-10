"""langmem 抽象层 + fallback 单测(Phase 6.2 P0)。"""

from __future__ import annotations

import pytest

from smartbutler.capabilities.langmem import (
    BaseLangmemAdapter,
    ExtractedFact,
    Langmem0030Adapter,
    LangmemError,
    RegexFactExtractor,
    build_default_adapter,
)


@pytest.mark.asyncio
async def test_fallback_extracts_name() -> None:
    """匹配"我叫/我是"。"""
    a = RegexFactExtractor()
    facts = await a.extract_facts([
        {"role": "user", "content": "你好,我叫张三"},
    ])
    assert any(f.fact_type == "name" for f in facts)


@pytest.mark.asyncio
async def test_fallback_extracts_location() -> None:
    """匹配"我住在"。"""
    a = RegexFactExtractor()
    facts = await a.extract_facts([
        {"role": "user", "content": "我住在上海浦东"},
    ])
    assert any(f.fact_type == "location" for f in facts)


@pytest.mark.asyncio
async def test_fallback_extracts_preference() -> None:
    """匹配"我喜欢"。"""
    a = RegexFactExtractor()
    facts = await a.extract_facts([
        {"role": "user", "content": "我喜欢喝美式咖啡"},
    ])
    assert any(f.fact_type == "preference" for f in facts)


@pytest.mark.asyncio
async def test_fallback_ignores_assistant_messages() -> None:
    """只抽 user 消息,忽略 assistant。"""
    a = RegexFactExtractor()
    facts = await a.extract_facts([
        {"role": "assistant", "content": "用户叫张三,住在上海"},
        {"role": "user", "content": "随便聊聊"},
    ])
    # 上面两个 fact 不该被抽出来(不在 user 消息)
    name_facts = [f for f in facts if f.fact_type == "name"]
    assert len(name_facts) == 0


@pytest.mark.asyncio
async def test_fallback_empty_conversation() -> None:
    """空对话返回空列表。"""
    a = RegexFactExtractor()
    assert await a.extract_facts([]) == []


@pytest.mark.asyncio
async def test_fallback_no_match() -> None:
    """无触发词时返回空。"""
    a = RegexFactExtractor()
    facts = await a.extract_facts([
        {"role": "user", "content": "今天天气不错"},
    ])
    assert facts == []


@pytest.mark.asyncio
async def test_fallback_deduplicates_within_message() -> None:
    """同一条消息内不重复加相同内容。"""
    a = RegexFactExtractor()
    facts = await a.extract_facts([
        {"role": "user", "content": "我喜欢喝咖啡,我也喜欢喝咖啡"},
    ])
    contents = [f.content for f in facts if f.fact_type == "preference"]
    # 同一 content 只出现一次
    assert len(contents) == len(set(contents))


@pytest.mark.asyncio
async def test_fallback_returns_extracted_fact_type() -> None:
    """ExtractedFact 是 frozen dataclass,字段可访问。"""
    a = RegexFactExtractor()
    facts = await a.extract_facts([
        {"role": "user", "content": "我老婆叫李四"},
    ])
    assert len(facts) >= 1
    assert all(isinstance(f, ExtractedFact) for f in facts)
    assert all(isinstance(f.content, str) for f in facts)
    assert all(isinstance(f.fact_type, str) for f in facts)


def test_fallback_implements_protocol() -> None:
    """RegexFactExtractor 实现 BaseLangmemAdapter。"""
    a: BaseLangmemAdapter = RegexFactExtractor()
    assert hasattr(a, "extract_facts")


def test_factory_returns_adapter() -> None:
    """工厂永远返回 BaseLangmemAdapter 实例。"""
    a = build_default_adapter()
    assert isinstance(a, BaseLangmemAdapter)


def test_factory_returns_usable_fallback() -> None:
    """工厂返回的实例必须能用(extract_facts 不抛)。"""
    import asyncio

    a = build_default_adapter()
    result = asyncio.run(a.extract_facts([
        {"role": "user", "content": "我住在上海,我叫张三"},
    ]))
    # 至少能正常返回(可能是空或命中)
    assert isinstance(result, list)
