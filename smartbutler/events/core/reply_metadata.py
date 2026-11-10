"""事件 → ButlerReply metadata 的提取工具(单一事实来源)。

## 为什么单独抽一个模块

L2 打断(2026-11-10)需要把「用户输入代次号」从事件透传到 ``ButlerReply``,
让播放端(``TTSOutputAdapter``)能判断「这条回复是否已过时」。

透传点有两处:
    - ``events/routing/answer_router_console.py``(有 AnswerRouter 的路径)
    - ``server/runtime.py``(老逻辑,无 AnswerRouter 的路径)

两处都需要同一个提取函数,但:
    - 放在 ``answer_router_console.py`` → ``server/runtime.py`` 要 import 它
      (server → events.routing,方向反了,且有循环依赖风险)
    - 放在 ``server/runtime.py`` → ``events/routing`` 要 import 它
      (events → server,方向反了)

所以放在 ``events/core/`` —— 这是**所有层都能依赖**的最底层位置。
"""

from __future__ import annotations

from typing import Any

from smartbutler.events.core.event import BaseEvent


def event_metadata(event: BaseEvent) -> dict[str, Any]:
    """从事件抽取要透传给 ``ButlerReply.metadata`` 的字段。

    🆕 2026-11-10:带上 ``generation``(用户输入代次号)。

    播放端(``TTSOutputAdapter``)用它判断:LLM 算完的这段时间里,
    用户是不是又说了新话?是的话这条回复已过时,不该播(L2 打断)。

    详见 :class:`smartbutler.voice.generation.GenerationTracker`。

    Args:
        event: 触发本次回复的事件。

    Returns:
        metadata 字典。非语音事件(DEVICE / TIMER 等)没有代次 → 空 dict,
        行为与改造前完全一致。
    """
    generation = getattr(event, "generation", 0)
    if not generation:
        return {}
    return {"generation": int(generation)}


__all__ = ["event_metadata"]
