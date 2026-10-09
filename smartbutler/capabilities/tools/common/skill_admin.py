"""Skill 管理工具(2026-10-09 增)。

提供:
- reload_skills: 通知管家下次重读 skill 目录。

为何是 tool:
管家是 LLM,不是 Python 进程。Skill 改了后,
LLM 不会"自己知道",必须给它一个 tool 来"主动询问" →
LLM 在对话中看到这个 tool 的描述,知道"我修改了 skill 后可以调它"。

设计要点:
1. **不在 thinking 层调工具**:thinking 是业务逻辑,tool 是能力层。
2. **不绕过 SkillRuntime**:tool 只调 SkillRuntime.mark_dirty(),
   thinking 层的封装边界保留,tool 不能直接改 _fingerprint。
3. **scope=BUTLER 而非 GLOBAL**:只有管家能用,子 agent 看不到(它们用 SKILL 自己的工具)。
"""

from __future__ import annotations

from smartbutler.capabilities.tools.decorator import register_tool
from smartbutler.capabilities.tools.types import ToolError, ToolScope
from smartbutler.thinking.skills.runtime import SkillRuntime


def _get_runtime() -> SkillRuntime:
    """取单例。测试时可 patch 替换。"""
    # SkillRuntime 当前没有显式 singleton,这里走 from_settings 懒构造
    # 引入主流程的 settings 装配点,避免分散创建
    from smartbutler.config.skills import get_skill_settings

    settings = get_skill_settings()
    return SkillRuntime.from_settings(settings)


@register_tool(
    name="reload_skills",
    description=(
        "重新加载 skill 目录。在以下情况调用:"
        "1. 你修改了 SKILL.md(描述/正文改动);"
        "2. 你新增、删除或重命名了 skill 目录;"
        "3. 你修改了 skill 下的 scripts/ 或 references/ 等文件。"
        "调用后,管家会在下一次需要时重读整个 skill 树,确保提示词与磁盘一致。"
        "不传参数表示全量重读。"
    ),
    scope=ToolScope.BUTLER,  # 只有管家能看到
    readonly=True,    # 不修改 skill 本身,只改内存缓存
    idempotent=True,  # 重复调用无副作用
)
def reload_skills(skill_name: str | None = None) -> str:
    """重新加载 skill 目录(2026-10-09 增)。

    Args:
        skill_name: 可选,指定要重读的 skill 名。留空表示全量重读。
            注意:目前实现是全量重读(标记下次必扫),
            skill_name 参数保留是给将来"按需重读"留扩展位。

    Returns:
        操作结果描述,LLM 用来回话给用户。

    Raises:
        ToolError: SkillRuntime 不可用(配置缺失等)。
    """
    try:
        runtime = _get_runtime()
    except Exception as exc:  # noqa: BLE001
        msg = f"无法获取 SkillRuntime: {exc}"
        raise ToolError(msg) from exc

    runtime.mark_dirty()
    target = skill_name or "全部"
    return f"已标记重读:{target}。下一轮对话或下次需要时管家会重读 skill 目录。"
