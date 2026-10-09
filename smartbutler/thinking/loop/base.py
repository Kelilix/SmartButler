"""BaseLoop 协议——两条循环的最小契约(参考 ADR-009 §3.1)。

## 为什么用 Protocol 而不是 ABC

ReactiveLoop 当前是 **LangGraph 编译出的 ``CompiledStateGraph``**(由 ButlerGraphBuilder.build() 产出),
**不是普通类**。强行让它继承 ABC 既别扭又没用。

所以 ``BaseLoop`` 用 ``Protocol`` 定义最小契约,ProactiveLoop 实现这个协议。
ReactiveLoop 由 LangGraph 框架保证"循环行为"——不需要实现本协议。

## 契约(4 个方法)

- ``loop_type`` (property) : 标识自己是 REACTIVE 还是 PROACTIVE
- ``tick`` : 单次循环入口
    - ReactiveLoop: 暴露为 ``ButlerOrchestrator.ainvoke(user_msg)``
    - ProactiveLoop: 暴露为 ``ButlerLoop.tick(event)``
- ``get_tools`` : 当前循环可用的工具列表
- ``get_system_prompt`` : 当前循环使用的 system prompt

## 不变量

1. **ProactiveLoop 必须实现本协议** —— 测试代码靠 ``isinstance(loop, BaseLoop)`` 判断
2. **ReactiveLoop 不强制实现** —— 已有 ButlerGraphBuilder 自身就是循环,不需要再套一层
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from smartbutler.thinking.proactive.triggers import LoopType


@runtime_checkable
class BaseLoop(Protocol):
    """管家循环的最小契约。

    两条循环(Reactive / Proactive)都应满足:
    - 能标识自己的 loop_type
    - 有 ``tick`` 入口接受输入跑一轮循环
    - 能告诉外界自己有哪些 tool / system prompt(便于 AnswerRouter / 调试)
    """

    @property
    def loop_type(self) -> LoopType:
        """标识本循环类型(REACTIVE / PROACTIVE)。"""
        ...

    def get_tools(self) -> list[Any]:
        """当前循环可用的工具列表(LangChain StructuredTool)。"""
        ...

    def get_system_prompt(self) -> str:
        """当前循环使用的 system prompt。"""
        ...


class ReactiveLoopHandle:
    """ReactiveLoop 的协议适配器(Protocol 适配层)。

    ReactiveLoop 实质是 ``ButlerOrchestrator.ainvoke()`` 入口,本身没有"类"
    可以继承 BaseLoop。``ReactiveLoopHandle`` 把 ButlerOrchestrator 适配成
    ``BaseLoop`` 协议——便于在 EventTrigger / AnswerRouter 等"对循环无感"
    的代码里统一用 ``loop.tick(...)`` 风格调用。

    不重写任何逻辑,只是薄薄一层包装。
    """

    def __init__(self, orchestrator: Any) -> None:
        self._orch = orchestrator

    @property
    def loop_type(self) -> LoopType:
        return LoopType.REACTIVE

    def get_tools(self) -> list[Any]:
        # ButlerOrchestrator 内部不缓存 tools 列表,需要走 _collect_tools
        if hasattr(self._orch, "_all_tools") and self._orch._all_tools:  # noqa: SLF001
            return list(self._orch._all_tools)  # noqa: SLF001
        if hasattr(self._orch, "_collect_tools"):
            return self._orch._collect_tools()  # noqa: SLF001
        return []

    def get_system_prompt(self) -> str:
        # 跟 get_tools 类似,从 orchestrator 拿已构建的 prompt
        # 如果 orchestrator 还没构建,返回空字符串
        if hasattr(self._orch, "_compiled") and self._orch._compiled is not None:  # noqa: SLF001
            # 实际 prompt 在 graph 内部,这里只能拿到 "" 兜底
            # 单测 / 调试时不会走到这条路径
            return ""
        return ""

    async def tick(self, user_input: str, **kwargs: Any) -> str:
        """适配 ``ainvoke`` 接口。"""
        return await self._orch.ainvoke(user_input, **kwargs)


__all__ = [
    "BaseLoop",
    "ReactiveLoopHandle",
]
