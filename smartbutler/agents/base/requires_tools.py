"""@requires_tools 装饰器 —— 让 Sub-Agent 依赖的 tool "一眼可见"。

设计动机 (为什么用装饰器而不是 ClassVar list):
1. **可读性**: 装饰器挂在类头上方,翻文件第一眼就能看到这个 Agent 依赖哪些 tool。
   比起在 class body 里找 ``required_tool_names: list[str] = [...]``,
   装饰器版本的视觉权重更高,reviewer 不容易漏。
2. **可扩展性**: 后续可能加 ``@requires_tools("a", "b", phase=4)`` 这种参数,
   装饰器能容纳,ClassVar list 很难。
3. **零成本**: 装饰器只在 ``__init_subclass__`` 阶段把名字写到 ``cls.required_tool_names``,
   不引入运行时反射,IDE 跳转和类型检查都通。

用法::

    from smartbutler.agents.base.base import BaseAgent
    from smartbutler.agents.base.requires_tools import requires_tools

    @requires_tools("get_current_time")
    class TestTimeAgent(BaseAgent):
        name = "test_time_agent"
        description = "..."

        def __init__(self, llm: BaseLLM) -> None:
            super().__init__()
            self._llm = llm
            # 框架已从 ToolRegistry.get_default() 把 required_tool_names 解析成
            # self._tools 字典,子类无需再手动 _register_tool。
            self._tool_specs = [t.to_tool_spec() for t in self.tools]

边界:
- **不**做 tool 存在性校验:tool 可能 Phase N 才交付,Phase M 注册的 Sub-Agent
  引用了 Phase N 的 tool 是合理场景。具体校验放到 Sub-Agent 的 __init__
  (那时 registry 一定已满)。
- **不**限制命名空间:tool 名是全局 string,可以跨 Sub-Agent / Skill 复用。
"""
from __future__ import annotations

from typing import Any


def requires_tools(*tool_names: str) -> Any:
    """Sub-Agent 依赖的 tool 名列表。

    Args:
        *tool_names: 工具名（与 ``@register_tool(name=...)`` 指定的 name 一致）。

    Returns:
        类装饰器,把 ``required_tool_names`` ClassVar 写到被装饰的类上。

    Raises:
        TypeError: 任何参数不是非空字符串。
        ValueError: 有重复项。
    """
    # 装饰器可能在 import 阶段就执行,这里 fail-fast
    for n in tool_names:
        if not isinstance(n, str) or not n:
            msg = f"@requires_tools 参数必须是非空字符串,收到: {n!r}"
            raise TypeError(msg)
    if len(set(tool_names)) != len(tool_names):
        dupes = sorted({n for n in tool_names if tool_names.count(n) > 1})
        msg = f"@requires_tools 含重复项: {dupes}"
        raise ValueError(msg)

    def decorator(cls: type[Any]) -> type[Any]:
        # 写 ClassVar,__init_subclass__ 钩子会进一步 freeze + 校验
        cls.required_tool_names = list(tool_names)
        return cls

    return decorator


__all__ = ["requires_tools"]
