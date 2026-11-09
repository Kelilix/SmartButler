"""🆕 Phase 9:图相关的 tool。

⚠️ **不要在这里直接 re-export 函数名** —— ``resolve_person`` 同时是
子模块名和函数名,re-export 会让 ``from ...graph import resolve_person``
拿到函数而不是模块,导致 ``resolve_person.set_graph_dependencies`` 之类的
模块级访问失败。

用 ``import_module`` 的方式引用子模块即可。
"""

from smartbutler.capabilities.tools.graph import resolve_person

__all__ = ["resolve_person"]
