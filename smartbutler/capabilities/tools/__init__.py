"""Tool 能力层(capabilities/tools/)。

对业务层暴露:
- BaseTool / FunctionTool: 抽象基类
- ToolRegistry: 注册表
- register_tool: 装饰器
- ToolScope / Permission / ToolContext / ToolResult: 数据类型
- ToolError 及子异常: 异常体系
- to_langchain_tool: LangChain 适配(用于 Phase 4 接入 LangGraph)
- bootstrap(): 启动期入口,保证 ToolRegistry.get_default() 拿到"全量 tool"

业务层用法:
    from smartbutler.capabilities.tools import (
        BaseTool, ToolRegistry, register_tool, ToolScope,
        ToolContext, Permission,
    )

    @register_tool()
    def my_tool() -> str:
        return "ok"

启动期用法(应用入口 / main / FastAPI lifespan):
    from smartbutler.capabilities.tools import bootstrap
    from smartbutler.thinking.skills.runtime import SkillRuntime

    runtime = SkillRuntime.from_settings(...)
    bootstrap(skill_runtime=runtime)  # ← 一行,后续 get_default() 一定是全量

LangChain 适配(Phase 4 才用):
    from smartbutler.capabilities.tools import to_langchain_tool
    lc_tools = [to_langchain_tool(t) for t in registry.list_all()]
"""

from __future__ import annotations

from smartbutler.capabilities.tools.base import BaseTool, FunctionTool

# 显式 import common/ 下的所有 tool 模块,
# 触发 @register_tool 副作用,把 2 个 common tool 注册到 default registry。
# (Phase 6.3:reload_skills 已搬到 thinking/skills/,由 bootstrap() 阶段 0 触发 import)
# 任何 `import smartbutler.capabilities.tools` 的人都会自动执行这段。
from smartbutler.capabilities.tools.common import (  # noqa: F401
    datetime as _common_datetime,
)
from smartbutler.capabilities.tools.common import (  # noqa: F401
    web as _common_web,
)
from smartbutler.capabilities.tools.decorator import register_tool
from smartbutler.capabilities.tools.langchain_adapter import (
    collect_langchain_tools,
    to_langchain_tool,
)
from smartbutler.capabilities.tools.registry import ToolRegistry
from smartbutler.capabilities.tools.types import (
    Permission,
    ToolAlreadyRegisteredError,
    ToolContext,
    ToolError,
    ToolNotFoundError,
    ToolPermissionDeniedError,
    ToolResult,
    ToolScope,
    ToolTimeoutError,
)

# 全量 tool 白名单 ——
# 任何"对管家 + 业务"可见的 tool,启动期必须全部出现在 ToolRegistry.get_default() 中。
# 缺一个 → bootstrap() 抛 RuntimeError,进程不启动。
# 多一个 → 警告(可能是新增 tool 忘了改这里),不阻塞启动。
EXPECTED_TOOLS: frozenset[str] = frozenset(
    {
        # common/(装饰器副作用,无需 backend)
        "get_current_time",
        "reload_skills",
        "web_fetch",
        # skills/(需要 backend,bootstrap() 阶段由 skill_runtime 装配)
        "read_file",
        "write_file",
        "edit_file",
        "delete_file",
        "ls",
        "grep",
        "glob",
    }
)


class ToolRegistryBootstrapError(RuntimeError):
    """ToolRegistry 启动期校验失败(缺 tool / 多 tool)。"""


def bootstrap(
    skill_runtime=None,
    *,
    registry: ToolRegistry | None = None,
    strict: bool = True,
) -> ToolRegistry:
    """启动期入口:保证 ToolRegistry.get_default() 拿到"全量 tool"。

    两阶段:
    1. 验证 common/ tool 已注册(由本模块 import 副作用完成)
    2. 显式注册 7 个 skill tool(需要 backend,所以这里注入)

    Args:
        skill_runtime: SkillRuntime 实例(提供 backend)。
            传 None = 跳过 skill tool 注册(只校验 common 段)。
        registry: 自定义 registry(默认 = ToolRegistry.get_default())。
            测试可传独立实例,避免污染全局单例。
        strict: True = 缺/多 tool 都抛错;False = 仅 warn。

    Returns:
        实际生效的 registry(== registry 参数 or get_default())。

    Raises:
        ToolRegistryBootstrapError: 启动校验失败(注册或白名单不匹配)。
    """
    import structlog

    _logger = structlog.get_logger(__name__)
    target = registry if registry is not None else ToolRegistry.get_default()

    # ---- 阶段 0:触发所有 common/ thinking/ skill_admin 的 @register_tool 副作用 ----
    # Phase 6.3 修订:reload_skills 从 capabilities.tools.common/ 搬到 thinking/skills/,
    # 顶层 import 不再由 capabilities.tools.__init__ 触发,
    # 这里显式 import 一次让 @register_tool 副作用发生。
    # 这么改后,capabilities.tools 不再 import 任何 thinking 模块,
    # agents ↔ capabilities.tools 整体循环被彻底切断。
    #
    # 同样道理:common/{datetime,web} 的 @register_tool 副作用原本由
    # capabilities.tools.__init__ 顶层 import 触发,但其他测试可能 reset registry,
    # 导致副作用"看似已触发,实际被清空"。这里统一由 bootstrap() 显式触发,
    # 任何时刻调 bootstrap() 都能保证 common tool 重新出现在 registry 里。
    import smartbutler.capabilities.tools.common.datetime  # noqa: F401
    import smartbutler.capabilities.tools.common.web  # noqa: F401
    import smartbutler.thinking.skills.skill_admin  # noqa: F401

    # ---- 阶段 1:校验 common tool 已就位 ----
    actual_names = {t.name for t in target.list_all()}
    expected_common = {"get_current_time", "reload_skills", "web_fetch"}
    missing_common = expected_common - actual_names
    if missing_common:
        msg = (
            f"bootstrap 阶段 1 失败:common tool 未注册 {missing_common}。"
            f"通常意味着 `from smartbutler.capabilities.tools import ...` 没被执行,"
            f"或 import 顺序导致 @register_tool 副作用没触发。"
        )
        raise ToolRegistryBootstrapError(msg)

    # ---- 阶段 2:注册 skill tool(7 个文件工具)----
    if skill_runtime is not None:
        # 走 thinking 层入口 ——
        # 7 个 file tool 现住 thinking/skills/llm_tools/,
        # register_default_skill_tools(backend, registry) 在那里定义,
        # thinking → capabilities 单向依赖,无需 lazy
        from smartbutler.thinking.skills.llm_tools import (
            register_default_skill_tools,
        )

        try:
            register_default_skill_tools(
                backend=skill_runtime.backend,
                registry=target,
            )
        except ToolAlreadyRegisteredError as exc:
            msg = f"bootstrap 阶段 2 失败:skill tool 重复注册 {exc}"
            raise ToolRegistryBootstrapError(msg) from exc

    # ---- 阶段 3:白名单校验 ----
    final_names = {t.name for t in target.list_all()}
    missing = EXPECTED_TOOLS - final_names
    extra = final_names - EXPECTED_TOOLS

    if missing:
        msg = (
            f"bootstrap 校验失败:ToolRegistry 缺 {len(missing)} 个 tool: "
            f"{sorted(missing)}。检查 EXPECTED_TOOLS 或 tool 模块是否被 import。"
        )
        if strict:
            raise ToolRegistryBootstrapError(msg)
        _logger.warning("tool_registry.bootstrap.missing", missing=sorted(missing))

    if extra:
        msg = (
            f"bootstrap 发现意外 tool(未列入 EXPECTED_TOOLS): {sorted(extra)}。"
            f"可能是新增 tool 忘了改 EXPECTED_TOOLS 白名单。"
        )
        if strict:
            # 多 tool 默认 warn,不阻塞(可能有 phase 2/3 tool 暂未禁用)
            _logger.warning("tool_registry.bootstrap.extra", extra=sorted(extra))
        else:
            _logger.warning("tool_registry.bootstrap.extra", extra=sorted(extra))

    _logger.info(
        "tool_registry.bootstrap.ok",
        total=len(final_names),
        names=sorted(final_names),
    )
    return target


__all__ = [
    # 抽象与实现
    "BaseTool",
    "FunctionTool",
    # 注册
    "ToolRegistry",
    "register_tool",
    # 启动期
    "bootstrap",
    "EXPECTED_TOOLS",
    "ToolRegistryBootstrapError",
    # 数据类型
    "ToolScope",
    "Permission",
    "ToolContext",
    "ToolResult",
    # 异常
    "ToolError",
    "ToolNotFoundError",
    "ToolAlreadyRegisteredError",
    "ToolPermissionDeniedError",
    "ToolTimeoutError",
    # LangChain 适配
    "to_langchain_tool",
    "collect_langchain_tools",
]
