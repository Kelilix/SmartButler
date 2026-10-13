"""Phase 6.6: Personality injector 单元测试。

覆盖:
- 默认 Personality 渲染 → 包含所有内置 trait + 默认 guidelines
- 数值三档(low / mid / high)翻译
- 未知 trait 名走兜底描述
- 空 traits / 空 guidelines 走兜底文字
- 渲染结果是纯函数(同输入同输出)
- 不调 LLM
- 与 prompt builder 集成
"""
from __future__ import annotations

import re

from smartbutler.emotion.personality.injector import render_persona_block
from smartbutler.emotion.personality.traits import (
    DEFAULT_NAME,
    TRAIT_FORMALITY,
    TRAIT_HUMOR,
    TRAIT_PROACTIVENESS,
    TRAIT_VERBOSITY,
    TRAIT_WARMTH,
    Personality,
    TraitDimension,
)
from smartbutler.thinking.prompt.builder import ButlerPromptBuilder


class TestRenderPersonaBlock:
    def test_contains_name(self) -> None:
        out = render_persona_block(Personality(name="测试管家"))
        assert "测试管家" in out

    def test_default_name_appears(self) -> None:
        out = render_persona_block(Personality())
        assert DEFAULT_NAME in out

    def test_contains_all_default_traits(self) -> None:
        out = render_persona_block(Personality())
        for key in (
            TRAIT_FORMALITY,
            TRAIT_HUMOR,
            TRAIT_VERBOSITY,
            TRAIT_WARMTH,
            TRAIT_PROACTIVENESS,
        ):
            assert key in out
            # 每行 trait 包含数值
            assert re.search(rf"\*\*{key}\*\*:\s+\d\.\d{{2}}", out), (
                f"trait {key} 没在输出里找到形如 'humor: 0.50' 的行"
            )

    def test_contains_default_guidelines(self) -> None:
        out = render_persona_block(Personality())
        assert "## 回答原则" in out
        # 至少包含一条默认 guideline 的关键字
        assert "简洁" in out or "不知道" in out

    def test_low_value_translates_to_low_bucket(self) -> None:
        """值 < 0.34 → low 档描述。"""
        p = Personality()
        p.traits[TRAIT_HUMOR] = TraitDimension(value=0.1)
        out = render_persona_block(p)
        # humor low 描述包含 "严肃"
        assert "严肃" in out

    def test_mid_value_translates_to_mid_bucket(self) -> None:
        """0.34 <= 值 < 0.67 → mid 档。"""
        p = Personality()
        p.traits[TRAIT_HUMOR] = TraitDimension(value=0.5)
        out = render_persona_block(p)
        # humor mid 描述包含 "偶尔"
        assert "偶尔" in out

    def test_high_value_translates_to_high_bucket(self) -> None:
        """值 >= 0.67 → high 档。"""
        p = Personality()
        p.traits[TRAIT_HUMOR] = TraitDimension(value=0.9)
        out = render_persona_block(p)
        # humor high 描述包含 "幽默"
        assert "幽默" in out or "开玩笑" in out

    def test_unknown_trait_uses_fallback(self) -> None:
        """未知 trait 名 → 走"偏低/中等/偏高"兜底,不崩。"""
        p = Personality()
        p.traits["创造力"] = TraitDimension(value=0.1)  # type: ignore[index]
        out = render_persona_block(p)
        assert "创造力" in out
        assert "偏低" in out

    def test_empty_traits_renders_gracefully(self) -> None:
        p = Personality(traits={})
        out = render_persona_block(p)
        assert "未配置性格维度" in out

    def test_empty_guidelines_omits_section(self) -> None:
        p = Personality(guidelines=[])
        out = render_persona_block(p)
        assert "## 回答原则" not in out

    def test_pure_function(self) -> None:
        """同样的 Personality 调用两次,结果一致(除了 updated_at 可能变)。"""
        p = Personality()
        out1 = render_persona_block(p)
        out2 = render_persona_block(p)
        # updated_at / version 不在渲染里,所以应该一致
        assert out1 == out2

    def test_rendered_value_uses_two_decimals(self) -> None:
        p = Personality()
        p.traits[TRAIT_HUMOR] = TraitDimension(value=0.5)
        out = render_persona_block(p)
        # 找 humor 行
        for line in out.splitlines():
            if "**humor**" in line:
                assert "0.50" in line
                break
        else:
            raise AssertionError("humor 行没找到")


class TestInjectorIntegrationWithPromptBuilder:
    def test_prompt_builder_with_personality_block(self) -> None:
        """ButlerPromptBuilder 接受 personality_block,优先级高于内置 _BUTLER_PERSONA。"""
        block = render_persona_block(Personality(name="我的管家"))
        prompt = ButlerPromptBuilder().build(personality_block=block)
        # Personality block 应该出现
        assert "我的管家" in prompt
        # 内置 _BUTLER_PERSONA 的标志性句子不应出现(被 personality 覆盖了)
        assert "SmartButler 管家" not in prompt

    def test_prompt_builder_without_personality_uses_default(self) -> None:
        """不传 personality_block → 走老行为(内置 _BUTLER_PERSONA)。"""
        prompt = ButlerPromptBuilder().build()
        assert "SmartButler 管家" in prompt

    def test_prompt_builder_empty_string_falls_back_to_default(self) -> None:
        """空字符串走默认,不要渲染出空段。"""
        prompt = ButlerPromptBuilder().build(personality_block="")
        assert "SmartButler 管家" in prompt
        assert "SmartButler 管家" in prompt or "小管家" in prompt  # 至少有一个

    def test_prompt_builder_whitespace_only_treated_as_empty(self) -> None:
        """纯空白字符串也走默认。"""
        prompt = ButlerPromptBuilder().build(personality_block="   \n  ")
        assert "SmartButler 管家" in prompt
