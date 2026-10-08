"""Tool 能力层 — BaseTool 抽象与 FunctionTool 实现。

设计原则（参考 TECHNICAL_DESIGN.md §3.2.2 tools/ + ADR-002）：
1. **基类零 LangChain 依赖**：BaseTool 只依赖 capabilities.llm.types 的 ToolSpec，
   不引用 StructuredTool；LangChain 适配放 langchain_adapter.py。
2. **权限 / 超时 / 计时统一在 ainvoke**：子类只实现 arun 业务逻辑，
   框架负责横切关注点（permission check / asyncio.wait_for / 计时）。
3. **scope + owner 二维声明**：tool 通过 ClassVar 声明元数据，
   registry / LangGraph 节点据此做可见性过滤。
"""

from __future__ import annotations

import asyncio
import inspect
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, Field, create_model

from smartbutler.capabilities.llm.types import FunctionSpec, ToolSpec
from smartbutler.capabilities.tools.types import (
    Permission,
    ToolContext,
    ToolError,
    ToolPermissionDeniedError,
    ToolResult,
    ToolScope,
    ToolTimeoutError,
    now_ms,
)

# 类型别名：tool 元数据（避免重复写 str | None 等）
_OwnerType = str | None
_PermissionSet = frozenset[Permission]
_ToolMeta = tuple[str, str, ToolScope, _OwnerType, _OwnerType, _PermissionSet, float | None, bool, bool]


class BaseTool(ABC):
    """Tool 抽象基类。

    所有具体 tool（FunctionTool / 内置工具 / Skill 工具）必须继承本类。
    LangGraph 节点仅依赖 BaseTool + ToolSpec，不直接 import 具体子类。

    元数据声明方式（普通实例/类属性，子类可在 class body 覆写）：

    - name: 唯一 ID，registry 用它去重。
    - description: 路由关键 —— LLM 看到 description 决定是否调。
    - scope: 可见性范围（GLOBAL / BUTLER / AGENT / SKILL / COMMON）。
    - owner_agent / owner_skill: scope=AGENT/SKILL 时填，否则 None。
    - required_permissions: 必需权限，调用方缺则抛 ToolPermissionDeniedError。
    - timeout_seconds: None 表示不限；超过则抛 ToolTimeoutError。
    - idempotent: 是否幂等（用于 LangGraph 重试 / LLM 自愈判定）。
    - readonly: 是否只读（用于审计 / UI 标注）。
    """

    name: str = ""  # 子类必须覆写
    description: str = ""  # 子类必须覆写

    scope: ToolScope = ToolScope.GLOBAL
    owner_agent: str | None = None
    owner_skill: str | None = None

    required_permissions: frozenset[Permission] = frozenset()
    timeout_seconds: float | None = None
    idempotent: bool = False
    readonly: bool = False

    def __init__(self) -> None:
        """校验元数据合法性。

        name 是硬性要求（registry 去重 / 路由识别必需）；
        description 推荐但允许为空（LLM 路由会受影响，调用方自行负责）；
        scope=AGENT/SKILL 时必须声明对应 owner。
        """
        if not self.name:
            msg = f"{type(self).__name__} 必须声明 name"
            raise ValueError(msg)
        if self.scope == ToolScope.AGENT and not self.owner_agent:
            msg = f"{self.name}: scope=AGENT 必须声明 owner_agent"
            raise ValueError(msg)
        if self.scope == ToolScope.SKILL and not self.owner_skill:
            msg = f"{self.name}: scope=SKILL 必须声明 owner_skill"
            raise ValueError(msg)

    @abstractmethod
    async def arun(self, **kwargs: Any) -> Any:
        """执行业务逻辑；返回任意类型（str / dict / dataclass / Pydantic model）。

        ainvoke() 会统一包装成 ToolResult，
        arun() 不需要关心权限 / 超时 / 计时这些横切关注点。
        """

    async def ainvoke(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        """调用入口：权限校验 → 超时控制 → 计时 → 异常包装 → 返回 ToolResult。

        Args:
            ctx: 调用方上下文（含 permissions / parent_agent / ...）。
            **kwargs: 透传给 arun() 的业务参数。

        Returns:
            ToolResult: 统一外壳；success=False 时 LLM 看到的是 error 信息。

        Raises:
            ToolPermissionDeniedError: 权限不足。
            ToolTimeoutError: 超过 timeout_seconds。
        """
        # 1. 权限校验（缺失集合）
        missing = self.required_permissions - ctx.permissions
        if missing:
            msg = (
                f"Tool '{self.name}' 缺少权限: {sorted(p.value for p in missing)}; "
                f"调用方持有: {sorted(p.value for p in ctx.permissions)}"
            )
            raise ToolPermissionDeniedError(msg)

        started = now_ms()

        # 2. 超时控制 + 业务执行
        try:
            if self.timeout_seconds is not None:
                raw = await asyncio.wait_for(self.arun(**kwargs), timeout=self.timeout_seconds)
            else:
                raw = await self.arun(**kwargs)
        except ToolError:
            # 业务主动抛的 ToolError 直接冒泡（langchain adapter 会包成 ToolMessage）
            raise
        except TimeoutError as exc:
            duration_ms = now_ms() - started
            msg = (
                f"Tool '{self.name}' 超时 ({self.timeout_seconds}s), "
                f"实际耗时 {duration_ms:.0f}ms"
            )
            raise ToolTimeoutError(msg) from exc

        duration_ms = now_ms() - started

        # 3. 结果归一化
        return _normalize_result(self.name, raw, duration_ms)

    def to_tool_spec(self) -> ToolSpec:
        """导出为 OpenAI 兼容的 ToolSpec（供 LLM chat() 使用）。

        JSON Schema 默认空对象；FunctionTool 子类根据函数签名自动填充。
        """
        return ToolSpec(
            type="function",
            function=FunctionSpec(
                name=self.name,
                description=self.description,
                parameters={"type": "object", "properties": {}},
            ),
        )


class FunctionTool(BaseTool):
    """由 Python 函数（或 async 函数）包装而成的 BaseTool。

    主要用途：
    - @register_tool 装饰器底层使用（见 decorator.py）。
    - 业务方一行代码定义一个 tool，零样板。
    """

    def __init__(
        self,
        func: Callable[..., Any],
        *,
        name: str | None = None,
        description: str | None = None,
        scope: ToolScope = ToolScope.GLOBAL,
        owner_agent: str | None = None,
        owner_skill: str | None = None,
        required_permissions: frozenset[Permission] | None = None,
        timeout_seconds: float | None = None,
        idempotent: bool = False,
        readonly: bool = False,
    ) -> None:
        self._func = func
        # 写元数据（实例属性覆盖 ClassVar）
        self.name = name or func.__name__
        # description 兜底链：显式参数 → docstring 首行 → 函数签名 → 占位
        if description is not None:
            self.description = description
        elif func.__doc__:
            self.description = func.__doc__.strip().split("\n")[0]
        else:
            self.description = _signature_summary(func)
        self.scope = scope
        self.owner_agent = owner_agent
        self.owner_skill = owner_skill
        self.required_permissions = required_permissions or frozenset()
        self.timeout_seconds = timeout_seconds
        self.idempotent = idempotent
        self.readonly = readonly

        # 元数据校验（复用基类 __init__）
        BaseTool.__init__(self)

        # 缓存 Pydantic args schema（langchain adapter 用）
        self._args_schema = _build_args_schema(func)

    @property
    def args_schema(self) -> type[BaseModel]:
        """从函数签名推导出的 Pydantic 模型，用于 LangChain StructuredTool。"""
        return self._args_schema

    async def arun(self, **kwargs: Any) -> Any:
        """调用底层函数，自动适配 sync / async。"""
        if inspect.iscoroutinefunction(self._func):
            return await self._func(**kwargs)
        return self._func(**kwargs)

    def to_tool_spec(self) -> ToolSpec:
        """导出 OpenAI 协议 ToolSpec，含从函数签名推导的 JSON Schema。"""
        cleaned = _clean_json_schema(self._args_schema.model_json_schema())
        return ToolSpec(
            type="function",
            function=FunctionSpec(
                name=self.name,
                description=self.description,
                parameters=cleaned,
            ),
        )


def _build_args_schema(func: Callable[..., Any]) -> type[BaseModel]:
    """从函数签名推导 Pydantic BaseModel。

    支持:
    - 类型注解（int / str / bool / float / list / dict / 自定义类）
    - 默认值
    - 异步函数（用 signature(func) 即可，参数表一致）

    Returns:
        Pydantic 模型类。
    """
    sig = inspect.signature(func)
    fields: dict[str, tuple[Any, Any]] = {}
    for param_name, param in sig.parameters.items():
        if param_name == "self":
            continue
        annotation = param.annotation if param.annotation is not inspect.Parameter.empty else str
        if param.default is inspect.Parameter.empty:
            fields[param_name] = (annotation, Field(...))
        else:
            fields[param_name] = (annotation, Field(default=param.default))
    return create_model(func.__name__, **fields)  # type: ignore[call-overload,no-any-return]


def _signature_summary(func: Callable[..., Any]) -> str:
    """从函数签名生成简短 description（无 docstring 时兜底）。"""
    sig = inspect.signature(func)
    params = ", ".join(p for p in sig.parameters if p != "self")
    return f"Function {func.__name__}({params})"


def _clean_json_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """清理 Pydantic 生成的 schema，保留 LLM 关心的字段。

    保留: type / properties / required / description。
    删除: title / $defs（避免污染 LLM prompt）。
    """
    cleaned: dict[str, Any] = {"type": "object", "properties": {}}
    raw_props = schema.get("properties", {})
    cleaned_props: dict[str, Any] = {}
    for prop_name, prop_schema in raw_props.items():
        if not isinstance(prop_schema, dict):
            continue
        prop_cleaned: dict[str, Any] = {"type": prop_schema.get("type", "string")}
        if "description" in prop_schema:
            prop_cleaned["description"] = prop_schema["description"]
        cleaned_props[prop_name] = prop_cleaned
    cleaned["properties"] = cleaned_props
    if "required" in schema:
        cleaned["required"] = schema["required"]
    return cleaned


def _normalize_result(name: str, raw: Any, duration_ms: float) -> ToolResult:
    """把 arun() 的返回值统一归一为 ToolResult 外壳。"""
    if isinstance(raw, ToolResult):
        return raw.model_copy(update={"duration_ms": duration_ms})
    if isinstance(raw, BaseModel):
        return ToolResult(
            success=True,
            content=raw.model_dump_json(),
            data=raw.model_dump(),
            duration_ms=duration_ms,
        )
    if isinstance(raw, str):
        return ToolResult(success=True, content=raw, duration_ms=duration_ms)
    if isinstance(raw, dict):
        import json

        return ToolResult(
            success=True,
            content=json.dumps(raw, ensure_ascii=False, default=str),
            data=raw,
            duration_ms=duration_ms,
        )
    import json

    return ToolResult(
        success=True,
        content=json.dumps(raw, ensure_ascii=False, default=str),
        data=None,
        duration_ms=duration_ms,
    )
