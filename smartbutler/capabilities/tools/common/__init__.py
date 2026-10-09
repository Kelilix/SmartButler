"""通用工具集合(不属于任何 Sub-Agent / Skill)。

所有 scope=GLOBAL/BUTLER,默认对管家可见。
具体 Sub-Agent 工具放 smartbutler/capabilities/tools/home/ 等子目录。

Phase 6.3:reload_skills 已搬到 thinking/skills/,
由 smartbutler.capabilities.tools.bootstrap() 阶段 0 触发 import。
"""

from smartbutler.capabilities.tools.common.datetime import get_current_time
from smartbutler.capabilities.tools.common.web import web_fetch

__all__ = ["get_current_time", "web_fetch"]
