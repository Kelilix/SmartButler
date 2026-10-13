"""性格系统(Phase 6.6)。

子模块:
- :mod:`traits`    Pydantic 数据模型(Personality + TraitDimension)
- :mod:`loader`    personality.md(YAML frontmatter)加载/保存
- :mod:`injector`  Personality → system prompt 片段渲染

Phase 6.6 已交付:静态 trait + 默认配置 + 注入到 thinking 层。
Phase 6.7 待做:Learn 触发 ``Personality.adjust_trait()``。
"""
from smartbutler.emotion.personality.injector import render_persona_block
from smartbutler.emotion.personality.loader import (
    DEFAULT_PERSONALITY_FILENAME,
    load_personality,
    save_personality,
)
from smartbutler.emotion.personality.traits import (
    DEFAULT_GUIDELINES,
    DEFAULT_IDENTITY,
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

__all__ = [
    # traits
    "Personality",
    "TraitDimension",
    "TRAIT_VALUE_MIN",
    "TRAIT_VALUE_MAX",
    "TRAIT_FORMALITY",
    "TRAIT_HUMOR",
    "TRAIT_VERBOSITY",
    "TRAIT_WARMTH",
    "TRAIT_PROACTIVENESS",
    "DEFAULT_TRAITS",
    "DEFAULT_IDENTITY",
    "DEFAULT_NAME",
    "DEFAULT_VERSION",
    "DEFAULT_GUIDELINES",
    # loader
    "load_personality",
    "save_personality",
    "DEFAULT_PERSONALITY_FILENAME",
    # injector
    "render_persona_block",
]
