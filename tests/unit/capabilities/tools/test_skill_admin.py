"""reload_skills tool 测试(2026-10-09 增,Phase 6.3 移到 thinking/skills/)。

覆盖:
- tool 已通过 @register_tool 装饰器注册到默认 registry
- name/scope/readonly/idempotent 元数据正确
- 实际调用走 SkillRuntime.mark_dirty(不直接动 _fingerprint)
- 错误路径:SkillRuntime 不可用 → 抛 ToolError

Phase 6.3 修订:
- skill_admin 从 capabilities/tools/common/ 搬到 thinking/skills/
- 触发 @register_tool 副作用的入口改成 smartbutler.capabilities.tools.bootstrap()
  (见 capabilities/tools/__init__.py 阶段 0)
- 本测试在 import 测试模块后还要调一次 bootstrap() 阶段 0 来保证注册
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

import smartbutler.thinking.skills.skill_admin as skill_admin_module
from smartbutler.capabilities.tools.registry import ToolRegistry
from smartbutler.capabilities.tools.types import ToolError, ToolScope
from smartbutler.thinking.skills.skill_admin import reload_skills


def _ensure_registered() -> None:
    """保证 reload_skills 在默认 registry 里(其他测试可能 reset 过)。

    Phase 6.3 修订:不再用 importlib.reload(那是治标),
    改成调 bootstrap() + 在 bootstrap 内部用 importlib.reload
    重新触发 @register_tool 装饰器副作用。
    """
    if ToolRegistry.get_default().try_get("reload_skills") is not None:
        return
    # 其他测试可能 reset 了 default registry,导致 @register_tool 装饰器
    # 副作用"已触发但被清空"。这里显式 reload 模块,让装饰器重跑。
    import importlib

    from smartbutler.capabilities.tools.common import datetime as _dt
    from smartbutler.capabilities.tools.common import web as _web
    from smartbutler.thinking.skills import skill_admin

    importlib.reload(skill_admin)
    importlib.reload(_dt)
    importlib.reload(_web)


def test_reload_skills_registered_in_default_registry() -> None:
    """reload_skills 必须已通过 @register_tool 注册(import-time 副作用)。"""
    _ensure_registered()
    tool = ToolRegistry.get_default().try_get("reload_skills")
    assert tool is not None, "reload_skills 必须已通过 @register_tool 注册"


def test_reload_skills_metadata() -> None:
    """name / scope / readonly / idempotent 必须符合 BUTLER tool 标准。"""
    _ensure_registered()
    tool = ToolRegistry.get_default().try_get("reload_skills")
    assert tool is not None
    assert tool.name == "reload_skills"
    assert tool.scope == ToolScope.BUTLER, "BUTLER 可见性:管家专属"
    assert tool.readonly is True
    assert tool.idempotent is True
    # 描述里要写明调用场景,LLM 才能判断何时调
    assert "SKILL.md" in tool.description
    assert "skill 目录" in tool.description or "skill 树" in tool.description


def test_reload_skills_calls_mark_dirty() -> None:
    """tool 函数应通过 SkillRuntime.mark_dirty() 标记,而不是直接动 _fingerprint。"""
    fake_runtime = MagicMock()

    with patch.object(
        skill_admin_module,
        "_get_runtime",
        return_value=fake_runtime,
    ):
        result = reload_skills()

    fake_runtime.mark_dirty.assert_called_once_with()
    assert "已标记重读" in result
    assert "全部" in result  # skill_name=None → "全部"


def test_reload_skills_with_name() -> None:
    """传 skill_name → 返回里带名字。"""
    fake_runtime = MagicMock()
    with patch.object(
        skill_admin_module,
        "_get_runtime",
        return_value=fake_runtime,
    ):
        result = reload_skills(skill_name="my-skill")

    fake_runtime.mark_dirty.assert_called_once_with()
    assert "my-skill" in result


def test_reload_skills_raises_tool_error_on_failure() -> None:
    """_get_runtime 失败 → 抛 ToolError,不让 LLM 收到原始异常。"""
    with patch.object(
        skill_admin_module,
        "_get_runtime",
        side_effect=RuntimeError("config missing"),
    ):
        with pytest.raises(ToolError) as exc_info:
            reload_skills()

    assert "config missing" in str(exc_info.value)

