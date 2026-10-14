"""性格 → system prompt 注入器(Phase 6.6)。

职责:把 :class:`Personality` 实例渲染成可拼到管家 system prompt 的 markdown 片段。

不做:
- 不调 LLM
- 不改 Personality(纯函数)
- 不查 trait 互动规则(冲突由 LLM 自行融合)

设计:
- 数值 → 自然语言:把 0-1 浮点按三档(low/mid/high)翻译成中文描述,
  让 LLM 看到的是「幽默度 0.7 (很幽默)」而不是裸数字
- 输出用中文(管家整体人设是中文),方便亲属手改
- 空 traits 走兜底,空 guidelines 走兜底
"""
from __future__ import annotations

from smartbutler.emotion.personality.traits import TRAIT_VALUE_MAX, TRAIT_VALUE_MIN, Personality

# 数值档位 → 中文描述的映射
# 阈值: 0.0-0.34 low, 0.34-0.67 mid, 0.67-1.0 high
_LOW_THRESHOLD: float = 0.34
_HIGH_THRESHOLD: float = 0.67

# 内置 trait 的三档描述(用 trait 名做 key,未知 trait 走 _FALLBACK_DESCRIPTIONS)
_TRAIT_DESCRIPTIONS: dict[str, dict[str, str]] = {
    "formality": {
        "low": "非常随意,像朋友聊天",
        "mid": "自然得体,日常用语",
        "high": "正式规范,用敬语",
    },
    "humor": {
        "low": "严肃认真,不开玩笑",
        "mid": "偶尔轻松,适度幽默",
        "high": "幽默风趣,爱开玩笑",
    },
    "verbosity": {
        "low": "极简,一句话答完",
        "mid": "适中,讲清要点",
        "high": "详尽,展开解释",
    },
    "warmth": {
        "low": "冷静理性,公事公办",
        "mid": "友善平和",
        "high": "热情体贴,主动关心",
    },
    "proactiveness": {
        "low": "被动,只答所问",
        "mid": "适度主动,必要时会多说一句",
        "high": "主动积极,经常给建议",
    },
}

# 兜底描述(未知 trait 名走这里)
_FALLBACK_DESCRIPTIONS: dict[str, str] = {
    "low": "偏低",
    "mid": "中等",
    "high": "偏高",
}

# 模板
_IDENTITY_HEADER: str = "## 身份认同"
_HEADER_TEMPLATE: str = "你是「{name}」,管家的性格画像如下:"
_TRAIT_LINE_TEMPLATE: str = "- **{name}**: {value:.2f} ({desc})"
_GUIDELINES_HEADER: str = "## 回答原则"
_FOOTER_NOTE: str = (
    "(以上是软性指引,不是死规则。当用户明确说「详细点」/「简短点」"
    "等指令时,以指令为准,无需每次解释 trait 设置。)"
)


def _describe_trait(trait_key: str, value: float) -> str:
    """把 trait 数值翻译成中文档位描述。"""
    if value < _LOW_THRESHOLD:
        bucket = "low"
    elif value < _HIGH_THRESHOLD:
        bucket = "mid"
    else:
        bucket = "high"

    trait_map = _TRAIT_DESCRIPTIONS.get(trait_key, _FALLBACK_DESCRIPTIONS)
    return trait_map[bucket]


def render_persona_block(personality: Personality) -> str:
    """把 Personality 渲染成可注入 system prompt 的 markdown 片段。

    渲染结果形如::

        ## 身份认同
        你是一个善解人意、细致耐心的智能管家。...

        你是「小管家」,管家的性格画像如下:

        ## 性格特征

        - **formality**: 0.30 (自然得体,日常用语)
        - **humor**: 0.50 (偶尔轻松,适度幽默)
        - **verbosity**: 0.40 (适中,讲清要点)
        - **warmth**: 0.70 (热情体贴,主动关心)
        - **proactiveness**: 0.50 (适度主动,必要时会多说一句)

        ## 回答原则

        - 回答简洁,默认不超过 3 句话
        - 不知道就说不知道,不要瞎猜
        - 能直接答的(常识 / 闲聊 / 简单计算)就直接答,不要无意义调工具

        (以上是软性指引,不是死规则。...)

    Args:
        personality: 待渲染的 Personality。

    Returns:
        拼好的 markdown 字符串(可作 system prompt 片段)。
        即便 Personality 是空的,也会输出最小可用片段(只有名字)。
    """
    parts: list[str] = [

        _IDENTITY_HEADER,
        personality.identity,
        "",
        _HEADER_TEMPLATE.format(name=personality.name),
        "",
        "## 性格特征",
    ]

    # 性格维度:按字典序输出,便于测试断言
    if personality.traits:
        for key in personality.list_trait_keys():
            dim = personality.traits[key]
            # 防御:虽然 Pydantic 限制了 0-1,这里再夹一次(避免 model_copy 之类的脏数据)
            clamped = max(TRAIT_VALUE_MIN, min(TRAIT_VALUE_MAX, dim.value))
            desc = _describe_trait(key, clamped)
            parts.append(
                _TRAIT_LINE_TEMPLATE.format(name=key, value=clamped, desc=desc)
            )
    else:
        parts.append("(未配置性格维度,按默认平衡型处理)")

    # 行为准则
    if personality.guidelines:
        parts.append("")
        parts.append(_GUIDELINES_HEADER)
        for g in personality.guidelines:
            line = g.strip()
            if line:
                parts.append(f"- {line}")

    parts.append("")
    parts.append(_FOOTER_NOTE)

    return "\n".join(parts)


__all__ = ["render_persona_block"]
