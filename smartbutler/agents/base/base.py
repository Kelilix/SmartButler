"""Sub-Agent 层 — 抽象基类。

设计原则（参考 TECHNICAL_DESIGN.md §3.2.3 + ADR-005）：
1. **业务层零 LangChain**：BaseAgent 不 import StructuredTool，LangChain 包装
   仅在 ``to_langchain_tool()`` 这一个边界出现。
2. **name/description 必填**：description 是管家 LLM 路由的关键，强制子类显式声明。
3. **tools 是声明，不参与执行**：子类自己管理 ``self._tools``（L1 内存），
   ``BaseAgent.tools`` 仅暴露只读视图，避免子类不小心修改共享 list。
4. **ainvoke() 必实现，handle() 包装超时/重试/异常归一**：子类只写业务 handle()，
   框架负责横切关注点（与 BaseTool.ainvoke() 对称）。
"""
from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from typing import Any

from smartbutler.agents.base.errors import AgentTimeoutError
from smartbutler.agents.base.types import AgentContext, AgentInput, AgentOutput
from smartbutler.capabilities.tools.base import BaseTool
from smartbutler.capabilities.tools.types import ToolContext

# 默认超时 / 重试 —— 允许子类 ClassVar 覆写
_DEFAULT_TIMEOUT: float = 60.0
_DEFAULT_MAX_RETRIES: int = 0


class BaseAgent(ABC):
    """Sub-Agent 抽象基类。

    所有具体 Sub-Agent（TestTimeAgent / 未来的 HomeAgent / ScheduleAgent / SearchAgent）必须继承本类。
    Phase 4 的 LangGraph Supervisor 通过 ``to_langchain_tool()`` 把本类包装成
    ``delegate_to_<name>`` 工具，管家 LLM 据此路由。

    声明依赖 tool 的两种方式（按推荐顺序）:

    1. **装饰器** ``@requires_tools("a", "b")``（推荐，**类头上方一眼可见**）:
        @requires_tools("get_current_time")
        class TimeAgent(BaseAgent): ...
    2. **ClassVar** ``required_tool_names = ["a", "b"]``（装饰器本质就是写这个）:

        class TimeAgent(BaseAgent):
            required_tool_names: list[str] = ["get_current_time"]

    两种写法由 ``__init_subclass__`` 统一收口成 ``cls.required_tool_names``，
    供 ``__init__`` 内部 ``self._register_tool(...)`` 时使用。
    """

    # ---------- 元数据（子类必须覆写 name / description） ----------
    name: str = ""
    description: str = ""

    # ---------- 依赖 tool 声明 ----------
    # 由 @requires_tools 装饰器或子类 ClassVar 显式声明。
    # __init__ 里据此从 ToolRegistry 取 tool 并 _register_tool。
    required_tool_names: list[str] = []

    # ---------- 运行时参数（子类可覆写） ----------
    timeout_seconds: float = _DEFAULT_TIMEOUT
    max_retries: int = _DEFAULT_MAX_RETRIES

    def __init_subclass__(cls, **kwargs: Any) -> None:  # noqa: D401
        """Subclass hook:校验 ``required_tool_names`` 合法,只读 freeze。

        规则:
        - 必须是不重复的非空字符串列表（可为空,代表不依赖任何 tool）。
        - freeze 后防止子类在运行时偷偷改 list 影响 _register_tool。
        """
        super().__init_subclass__(**kwargs)
        names = getattr(cls, "required_tool_names", [])
        if not isinstance(names, list):
            msg = (
                f"{cls.__name__}.required_tool_names 必须是 list[str],"
                f"实际是 {type(names).__name__}"
            )
            raise TypeError(msg)
        for n in names:
            if not isinstance(n, str) or not n:
                msg = f"{cls.__name__}.required_tool_names 含非空字符串: {n!r}"
                raise ValueError(msg)
        if len(set(names)) != len(names):
            dupes = sorted({n for n in names if names.count(n) > 1})
            msg = f"{cls.__name__}.required_tool_names 含重复项: {dupes}"
            raise ValueError(msg)
        # freeze:防止运行时篡改
        cls.required_tool_names = list(names)
        cls._required_tool_names_frozen = True  # type: ignore[attr-defined]

    def __init__(self, registry: Any = None) -> None:  # noqa: ANN401
        """校验元数据 + 初始化 tool 注册表。

        Args:
            registry: 可选 ToolRegistry 实例。
                - 传 None (默认): 自动按需触发 ``get_current_time`` 等常见 tool 的 import
                  以保证 ``@register_tool`` 装饰器副作用生效,
                  然后用 ``ToolRegistry.get_default()`` 解析 ``required_tool_names``。
                - 显式传 registry: 用于测试注入或自定义环境。

        Sub-Agent 子类如果需要更复杂的初始化(LLM / DB / etc),
        应在自己的 ``__init__`` 里 **先** 调 ``super().__init__()`` 再做自己的事。
        """
        if not self.name:
            msg = f"{type(self).__name__} 必须声明 name（Sub-Agent 唯一 ID）"
            raise ValueError(msg)
        if not self.description:
            msg = f"{type(self).__name__} 必须声明 description（路由关键，管家 LLM 依据）"
            raise ValueError(msg)
        # 子类在 __init__ 里通过 _register_tool() 注入 tool
        self._tools: dict[str, BaseTool] = {}

        # 框架级:按 required_tool_names 从 registry 取 tool 并注册。
        # 子类只要 @requires_tools("a", "b") 就什么都不用写。
        if self.required_tool_names:
            self._resolve_and_register_tools(registry)

    def _resolve_and_register_tools(self, registry: Any = None) -> None:
        """框架方法:从 ``required_tool_names`` + ``registry`` 解析并注册 tool。

        Sub-Agent 子类**通常不需要**直接调用本方法;
        ``__init__`` 会自动按需触发。
        Sub-Agent 子类**可**在自己额外依赖动态 tool 时显式调
        ``self._register_tool(some_tool)`` 补充。

        Args:
            registry: ToolRegistry 实例或 None (用默认 + 自动 import common tools)。

        Raises:
            RuntimeError: 任一 required tool 在 registry 里找不到。
        """

        target = registry if registry is not None else self._ensure_default_registry_with_common_tools()
        for tool_name in self.required_tool_names:
            tool = target.try_get(tool_name)
            if tool is None:
                msg = (
                    f"Agent {self.name!r} 依赖的 tool {tool_name!r} 未在 registry 中找到。"
                    "请确保对应的 @register_tool 装饰器已 import / 已注册。"
                )
                raise RuntimeError(msg)
            self._register_tool(tool)

    @staticmethod
    def _ensure_default_registry_with_common_tools() -> Any:  # noqa: ANN401
        """确保默认 ToolRegistry 里有 common tool (``get_current_time`` 等)。

        装饰器在模块 import 时才注册,所以这里显式 import 一次(幂等)。
        返回 ToolRegistry.get_default()。
        """
        from smartbutler.capabilities.tools.common.datetime import (  # noqa: F401
            get_current_time,
        )
        from smartbutler.capabilities.tools.common.web import (  # noqa: F401
            web_fetch,
        )
        from smartbutler.capabilities.tools.registry import ToolRegistry

        return ToolRegistry.get_default()

    # ---------- Tool 管理（子类用） ----------

    def _register_tool(self, tool: BaseTool) -> None:
        """子类在 __init__ 里调用，把 BaseTool 实例挂到本 Agent。"""
        if tool.name in self._tools:
            msg = f"Agent {self.name!r} 重复注册 tool {tool.name!r}"
            raise ValueError(msg)
        self._tools[tool.name] = tool

    @property
    def tools(self) -> list[BaseTool]:
        """只读视图：暴露给 AgentManager 收集。

        返回新 list 防外部修改污染内部状态。
        """
        return list(self._tools.values())

    def get_tool(self, name: str) -> BaseTool | None:
        """按 name 查找 tool（不抛异常）。"""
        return self._tools.get(name)

    # ---------- 业务入口 ----------

    @abstractmethod
    async def handle(self, input: AgentInput, ctx: AgentContext) -> AgentOutput:
        """执行业务逻辑；子类必须实现。

        框架负责：超时控制（timeout_seconds）/ 异常归一（包成 AgentOutput）/ 计时。
        业务方法不应 raise AgentError 之外的非预期异常，捕获后转成 AgentOutput(success=False)。
        """

    async def ainvoke(self, input: AgentInput, ctx: AgentContext) -> AgentOutput:
        """框架入口：超时 + 重试 + 异常归一。

        Args:
            input: 任务输入。
            ctx: 运行时上下文。

        Returns:
            AgentOutput: 统一外壳；失败时 success=False + error 字段。
        """
        if not input.raw or not input.raw.strip():
            return AgentOutput(
                content="",
                success=False,
                error="AgentInput.raw 不能为空",
            )

        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                if self.timeout_seconds is not None and self.timeout_seconds > 0:
                    return await asyncio.wait_for(
                        self.handle(input, ctx), timeout=self.timeout_seconds
                    )
                return await self.handle(input, ctx)
            except TimeoutError:
                msg = (
                    f"Agent {self.name!r} handle() 超时 "
                    f"({self.timeout_seconds}s, attempt {attempt + 1})"
                )
                last_exc = AgentTimeoutError(msg)
            except Exception as exc:  # noqa: BLE001 - 业务异常统一捕获
                # 框架级异常（如 AgentError）也算业务失败，包装后返回
                last_exc = exc
                if attempt < self.max_retries:
                    continue
                break

        # 全部重试耗尽 / 不可重试错误 → AgentOutput
        return AgentOutput(
            content="",
            success=False,
            error=f"{type(last_exc).__name__ if last_exc else 'UnknownError'}: "
            f"{last_exc if last_exc else 'no error captured'}",
        )

    # ---------- LangChain 适配（边界） ----------

    def to_langchain_tool(self) -> Any:  # noqa: ANN401 - 故意返回 Any
        """把 Sub-Agent 暴露成 LangChain StructuredTool。

        返回类型是 ``langchain_core.tools.StructuredTool``，本类不静态引用，
        避免业务层引入 LangChain 依赖。Phase 4 LangGraph 节点会拿到返回值并
        ``llm.bind_tools([...])``。
        """
        # 局部 import：唯一允许的 LangChain 接触点
        from langchain_core.tools import StructuredTool
        from pydantic import BaseModel as PydanticBaseModel
        from pydantic import Field as PydanticField

        agent_name = self.name
        agent_description = self.description

        class DelegateInput(PydanticBaseModel):
            """Sub-Agent 委托调用的入参 schema。"""

            task: str = PydanticField(
                ...,
                description=(
                    f"交给 {agent_name} 的具体任务描述。"
                    "应包含完整需求、上下文、约束、期望输出格式。"
                ),
                min_length=1,
            )

        async def call_agent(task: str) -> str:
            """真正的 LangChain tool 调用入口。

            协议：返回 str（LLM 看到）。错误也走字符串回流，遵循 ToolError 的模式。
            """
            # 构造最小 AgentContext；Phase 4 会从 LangGraph config 注入完整 ctx
            ctx = AgentContext(
                user_id="system",
                session_id="system",
                parent_agent="butler",
            )
            try:
                result = await self.ainvoke(AgentInput(raw=task), ctx)
            except Exception as exc:  # noqa: BLE001 - 兜底，绝不冒泡
                return f"[AgentError] {type(exc).__name__}: {exc}"
            if not result.success:
                return f"[AgentError] {result.error or 'unknown error'}"
            return result.content

        return StructuredTool.from_function(
            coroutine=call_agent,
            name=f"delegate_to_{agent_name}",
            description=agent_description,
            args_schema=DelegateInput,
        )

    # ---------- 内部辅助 ----------

    def _build_tool_context(self, agent_ctx: AgentContext) -> ToolContext:
        """把 AgentContext 转成 ToolContext，给 Sub-Agent 内部 tool 调用用。

        子类 handle() 实现里直接调用，避免每次重复构造。
        """
        return ToolContext(
            user_id=agent_ctx.user_id,
            session_id=agent_ctx.session_id,
            permissions=set(agent_ctx.permissions),
            parent_agent=agent_ctx.parent_agent or self.name,
            metadata=agent_ctx.metadata,
        )


__all__ = ["BaseAgent"]
