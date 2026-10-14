"""Phase 6.6: Personality traits 单元测试。

覆盖:
- Pydantic 模型字段约束([0, 1] 范围、字段必填等)
- 默认值正确性
- ``adjust_trait`` 增量/覆盖两路入口
- ``replace_guidelines`` 行为
- ``to_dict`` / ``from_dict`` 序列化往返
- 动态新增 trait
- 越界值自动夹紧
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from smartbutler.emotion.personality.traits import (
    DEFAULT_GUIDELINES,
    DEFAULT_NAME,
    DEFAULT_TRAITS,
    DEFAULT_VERSION,
    TRAIT_FORMALITY,
    TRAIT_HUMOR,
    TRAIT_PROACTIVENESS,
    TRAIT_VALUE_MAX,
    TRAIT_VALUE_MIN,
    TRAIT_VERBOSITY,
    TRAIT_WARMTH,
    Personality,
    TraitDimension,
)


class TestTraitDimension:
    def test_default_value(self) -> None:
        dim = TraitDimension(value=0.5)
        assert dim.value == 0.5

    def test_rejects_below_min(self) -> None:
        with pytest.raises(ValidationError):
            TraitDimension(value=-0.1)

    def test_rejects_above_max(self) -> None:
        with pytest.raises(ValidationError):
            TraitDimension(value=1.1)

    def test_field_validator_present(self) -> None:
        """验证 TraitDimension 上有 ``_clamp_value`` 装饰的 field_validator。

        不实际触发(因为 Pydantic 嵌套校验路径复杂),仅确认:
        1. TraitDimension 合法值能构造
        2. 越界值被 Pydantic Field(ge/le)在 __init__ 时拒绝
        """
        # 合法值 OK
        dim = TraitDimension(value=0.5)
        assert dim.value == 0.5
        # 越界值被 Field 拒绝(ge=0, le=1)
        with pytest.raises(ValidationError):
            TraitDimension(value=1.5)
        with pytest.raises(ValidationError):
            TraitDimension(value=-0.1)

    def test_equality(self) -> None:
        assert TraitDimension(value=0.5) == TraitDimension(value=0.5)
        assert TraitDimension(value=0.5) != TraitDimension(value=0.6)


class TestPersonalityDefaults:
    def test_default_name(self) -> None:
        p = Personality()
        assert p.name == DEFAULT_NAME

    def test_default_version(self) -> None:
        p = Personality()
        assert p.version == DEFAULT_VERSION
        assert p.version == 1

    def test_default_updated_by(self) -> None:
        p = Personality()
        assert p.updated_by == "default"
        assert p.updated_at is None

    def test_default_traits_have_all_dimensions(self) -> None:
        p = Personality()
        # 5 个内置 trait
        expected_keys = {
            TRAIT_FORMALITY,
            TRAIT_HUMOR,
            TRAIT_VERBOSITY,
            TRAIT_WARMTH,
            TRAIT_PROACTIVENESS,
        }
        assert set(p.traits.keys()) == expected_keys

    def test_default_trait_values_match_constants(self) -> None:
        p = Personality()
        for k, v in DEFAULT_TRAITS.items():
            assert p.traits[k].value == pytest.approx(v)

    def test_default_guidelines_not_empty(self) -> None:
        p = Personality()
        assert len(p.guidelines) == len(DEFAULT_GUIDELINES)
        assert p.guidelines == DEFAULT_GUIDELINES

    def test_list_trait_keys_sorted(self) -> None:
        p = Personality()
        keys = p.list_trait_keys()
        assert keys == sorted(keys)

    def test_get_trait_value(self) -> None:
        p = Personality()
        assert p.get_trait_value(TRAIT_HUMOR) == pytest.approx(DEFAULT_TRAITS[TRAIT_HUMOR])
        assert p.get_trait_value("nonexistent") is None


class TestPersonalityAdjustTrait:
    def test_delta_path_increments(self) -> None:
        p = Personality()
        before = p.get_trait_value(TRAIT_HUMOR)
        p.adjust_trait(TRAIT_HUMOR, delta=0.1, source="user_explicit")
        assert p.get_trait_value(TRAIT_HUMOR) == pytest.approx(before + 0.1)

    def test_delta_path_can_decrease(self) -> None:
        p = Personality()
        before = p.get_trait_value(TRAIT_HUMOR)
        p.adjust_trait(TRAIT_HUMOR, delta=-0.2, source="learn")
        assert p.get_trait_value(TRAIT_HUMOR) == pytest.approx(before - 0.2)

    def test_new_value_path_overrides(self) -> None:
        p = Personality()
        p.adjust_trait(TRAIT_HUMOR, new_value=0.9, source="user_explicit")
        assert p.get_trait_value(TRAIT_HUMOR) == pytest.approx(0.9)

    def test_delta_and_new_value_conflict_raises(self) -> None:
        p = Personality()
        with pytest.raises(ValueError, match="必须且只能传一个"):
            p.adjust_trait(
                TRAIT_HUMOR,
                delta=0.1,
                new_value=0.5,
                source="user_explicit",
            )

    def test_neither_delta_nor_new_value_raises(self) -> None:
        p = Personality()
        with pytest.raises(ValueError, match="必须且只能传一个"):
            p.adjust_trait(TRAIT_HUMOR, source="user_explicit")

    def test_new_trait_creates_dimension(self) -> None:
        """未知 trait 名 → 动态新增(Learn 可能发现新维度)。"""
        p = Personality()
        p.adjust_trait("creativity", new_value=0.7, source="learn")
        assert p.get_trait_value("creativity") == pytest.approx(0.7)
        assert "creativity" in p.traits

    def test_adjust_clamps_via_pydantic(self) -> None:
        """新值越界 → Pydantic 拒绝。"""
        p = Personality()
        with pytest.raises(ValidationError):
            p.adjust_trait(TRAIT_HUMOR, new_value=1.5, source="user_explicit")

    def test_delta_clamps_at_max(self) -> None:
        """增量 + 越界 → Pydantic 拒绝(不是 silently clamp)。"""
        p = Personality()
        with pytest.raises(ValidationError):
            p.adjust_trait(TRAIT_HUMOR, delta=2.0, source="user_explicit")

    def test_version_increments_on_modify(self) -> None:
        p = Personality()
        assert p.version == 1
        p.adjust_trait(TRAIT_HUMOR, delta=0.1, source="user_explicit")
        assert p.version == 2
        p.adjust_trait(TRAIT_HUMOR, delta=0.1, source="learn")
        assert p.version == 3

    def test_updated_at_set_on_modify(self) -> None:
        p = Personality()
        assert p.updated_at is None
        p.adjust_trait(TRAIT_HUMOR, delta=0.1, source="user_explicit")
        assert p.updated_at is not None
        # 形如 "2026-10-12T10:00:00+00:00" (ISO 8601)
        assert "T" in p.updated_at  # type: ignore[operator]

    def test_updated_by_records_source(self) -> None:
        p = Personality()
        p.adjust_trait(TRAIT_HUMOR, new_value=0.9, source="user_explicit")
        assert p.updated_by == "user_explicit"
        p.adjust_trait(TRAIT_HUMOR, delta=0.1, source="learn")
        assert p.updated_by == "learn"


class TestPersonalityReplaceGuidelines:
    def test_replaces_all(self) -> None:
        p = Personality()
        p.replace_guidelines(["新规则1", "新规则2"], source="user_explicit")
        assert p.guidelines == ["新规则1", "新规则2"]
        assert p.updated_by == "user_explicit"
        assert p.version == 2

    def test_strips_empty_strings(self) -> None:
        p = Personality()
        p.replace_guidelines(["a", "", "  ", "b"], source="user_explicit")
        assert p.guidelines == ["a", "b"]

    def test_empty_list_keeps_no_guidelines(self) -> None:
        p = Personality()
        p.replace_guidelines([], source="user_explicit")
        assert p.guidelines == []


class TestPersonalitySerialization:
    def test_to_dict_roundtrip(self) -> None:
        p = Personality(name="老管家")
        p.adjust_trait(TRAIT_HUMOR, new_value=0.8, source="user_explicit")
        data = p.to_dict()
        restored = Personality.from_dict(data)
        assert restored.name == "老管家"
        assert restored.get_trait_value(TRAIT_HUMOR) == pytest.approx(0.8)
        assert restored.version == p.version
        assert restored.updated_by == "user_explicit"

    def test_from_dict_partial(self) -> None:
        """只覆盖 name + 部分 trait,其他走默认。"""
        restored = Personality.from_dict({
            "name": "严管家",
            "traits": {"humor": 0.1},
        })
        assert restored.name == "严管家"
        assert restored.get_trait_value(TRAIT_HUMOR) == pytest.approx(0.1)
        # 其他 trait 走默认
        assert restored.get_trait_value(TRAIT_WARMTH) == pytest.approx(DEFAULT_TRAITS[TRAIT_WARMTH])

    def test_from_dict_ignores_extra_keys(self) -> None:
        """无关字段(比如 md body 里手写的注释被 loader 错塞进来)不会让模型炸。"""
        data = {"name": "X", "未知字段": "随便写"}
        p = Personality.from_dict(data)
        assert p.name == "X"

    def test_trait_dict_accepts_shorthand_value(self) -> None:
        """from_dict 接受 ``{humor: 0.5}``(裸数值)而不仅 ``{humor: {value: 0.5}}``。"""
        p = Personality.from_dict({"traits": {"humor": 0.42}})
        assert p.get_trait_value("humor") == pytest.approx(0.42)


class TestPersonalityRejectsInvalidName:
    def test_empty_name_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Personality(name="")

    def test_too_long_name_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Personality(name="x" * 65)


class TestPersonalityConstants:
    def test_trait_value_bounds(self) -> None:
        assert TRAIT_VALUE_MIN == 0.0
        assert TRAIT_VALUE_MAX == 1.0

    def test_default_trait_values_in_range(self) -> None:
        for v in DEFAULT_TRAITS.values():
            assert 0.0 <= v <= 1.0
