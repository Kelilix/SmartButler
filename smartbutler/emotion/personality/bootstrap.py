"""🆕 Personality 装配入口(2026-11-09)。

## 为什么需要这个模块

Personality 机制(Phase 6.6)本身是完整的 —— ``personality.md`` → ``load_personality()``
→ ``render_persona_block()`` → ``ButlerOrchestrator(personality=...)`` → system prompt。

但**生产代码里从来没调用过 ``load_personality()``** —— 只有单测在用。
所有入口脚本(``voice_service.py`` 等)构造 ``ButlerOrchestrator`` 时都**没传**
``personality`` 参数 → ``self._personality is None`` → ``personality_block`` 渲染成空串
→ ``ButlerPromptBuilder`` 回落到内置的通用 ``_BUTLER_PERSONA``
("你是 SmartButler 管家…")。

**结果**:用户配置的"中世纪英国贵族管家"人设完全没生效,LLM 用的是通用助手人设。

## 本模块解决什么

把「加载 personality + 注入 orchestrator」这件事收敛到一处:

    orchestrator = ButlerOrchestrator(llm=llm, llm_settings=settings)
    attach_personality(orchestrator)   # ← 这一行,人设就生效了

或者更省事,直接用工厂:

    orchestrator = build_orchestrator(llm=llm, llm_settings=settings)

## 降级链(闭合)

1. ``personality.md`` 存在且能解析 → 用它
2. 文件不存在 / 解析失败 → ``load_personality()`` 返回默认 :class:`Personality`
   (默认就是杰贝兹人设 —— 见 ``traits.py`` 的 ``DEFAULT_IDENTITY``)
3. 注入 orchestrator 失败 → 记 warning,管家继续走内置默认人设(不影响对话)

**任何情况下管家都不会崩,也不会"没人设"。**
"""

from __future__ import annotations

from typing import Any

from smartbutler.utils.logging import get_logger

logger = get_logger(__name__)


def load_personality_safe(
    path: str | None = None,
) -> Any:
    """加载 Personality,失败降级到默认值(绝不抛异常)。

    Args:
        path: 配置文件路径;``None`` → 走默认候选列表
            (``<cwd>/personality.md`` → ``~/.smartbutler/personality.md``)。

    Returns:
        合法 :class:`~smartbutler.emotion.personality.traits.Personality` 实例。
    """
    try:
        from smartbutler.emotion.personality.loader import load_personality

        personality = load_personality(path)
    except Exception as e:  # noqa: BLE001
        logger.warning("personality.load_failed,降级到默认人设 err=%s", e)
        try:
            from smartbutler.emotion.personality.traits import Personality

            return Personality()
        except Exception:  # noqa: BLE001
            return None

    logger.info(
        "personality.loaded name=%s traits=%s updated_by=%s",
        getattr(personality, "name", "?"),
        ",".join(personality.list_trait_keys()) if personality else "",
        getattr(personality, "updated_by", "?"),
    )
    return personality


def attach_personality(
    orchestrator: Any,
    *,
    path: str | None = None,
) -> Any:
    """把 Personality 注入 orchestrator(让它的人设进 system prompt)。

    注入成功 → 后续 ``ainvoke`` 的 system prompt 用 personality 渲染结果,
    **覆盖**内置的通用 ``_BUTLER_PERSONA``。

    注入失败 → 记 warning,orchestrator 保持原状(走内置默认人设)。

    Args:
        orchestrator: :class:`ButlerOrchestrator` 实例。
        path: personality 文件路径;``None`` → 默认候选列表。

    Returns:
        注入的 Personality 实例;失败返回 None。
    """
    if orchestrator is None:
        return None

    personality = load_personality_safe(path)
    if personality is None:
        return None

    try:
        orchestrator._personality = personality  # noqa: SLF001
        # 刷渲染缓存(如果有旧缓存,必须清掉)
        orchestrator._current_personality_block = ""  # noqa: SLF001
    except Exception as e:  # noqa: BLE001
        logger.warning("personality.attach_failed err=%s", e)
        return None

    logger.info(
        "personality.attached name=%s identity_len=%d",
        getattr(personality, "name", "?"),
        len(getattr(personality, "identity", "") or ""),
    )
    return personality


def build_orchestrator(
    *,
    llm: Any,
    llm_settings: Any = None,
    personality_path: str | None = None,
    **kwargs: Any,
) -> Any:
    """构造 ButlerOrchestrator 并**自动注入 Personality**(推荐入口)。

    这是所有生产入口脚本(``voice_service.py`` 等)构造 orchestrator 的
    **唯一推荐方式** —— 避免再次出现"忘了传 personality → 人设丢失"的问题。

    Args:
        llm: LLM 实例(必填)。
        llm_settings: LLM 配置。
        personality_path: personality 文件路径;``None`` → 默认候选列表。
        **kwargs: 透传给 ``ButlerOrchestrator``(如 ``memory_facade`` / ``short_term``)。

    Returns:
        已注入 Personality 的 orchestrator。
    """
    from smartbutler.thinking.loop.orchestrator import ButlerOrchestrator

    orchestrator = ButlerOrchestrator(
        llm=llm,
        llm_settings=llm_settings,
        **kwargs,
    )
    attach_personality(orchestrator, path=personality_path)
    return orchestrator


__all__ = ["load_personality_safe", "attach_personality", "build_orchestrator"]
