"""日期 / 时间查询工具。

提供:
- get_current_time: 返回指定时区的当前时间（ISO 8601 格式）。
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from smartbutler.capabilities.tools.decorator import register_tool
from smartbutler.capabilities.tools.types import ToolError

# Import-time 守卫:确保 IANA tz 数据库可用。
#
# Python 3.9+ 的 zoneinfo 在 Windows 上依赖 PEP 615 的第三方 `tzdata` 包
# (Linux/macOS 自带 tzdata,空操作)。如果 Windows runner 漏装 tzdata,
# ZoneInfo("Asia/Shanghai") 会直接抛 ZoneInfoNotFoundError。
#
# 这里 fail-fast,而不是拖到运行时让 get_current_time 静默回退 UTC,
# 误导 LLM 看到"UTC 时间"后"自作聪明"手动 +8 兜底(参见 time-cn e2e 历史日志)。
#
# 选 "Asia/Shanghai" 而不是 "UTC":UTC 是 zoneinfo 内置 fallback,
# 永远解析成功,无法反映 tzdata 是否真的可用。
try:
    ZoneInfo("Asia/Shanghai")
except ZoneInfoNotFoundError as exc:
    msg = (
        "smartbutler.capabilities.tools.common.datetime: ZoneInfo('Asia/Shanghai') 不可用,"
        "Windows 上需要安装 `tzdata` 包(pip install tzdata);"
        "Linux/macOS 系统自带 tzdata 不需要额外操作。"
        "参考 pyproject.toml dependencies 段。补装后重启进程即可。"
    )
    raise RuntimeError(msg) from exc


@register_tool(
    name="get_current_time",
    description=(
        "返回指定时区的当前日期和时间。"
        "返回 ISO 8601 格式（含时区偏移）。"
        "场景: 用户问'现在几点'/'今天几号'/'星期几'/'北京现在几点'等。"
    ),
    readonly=True,
    idempotent=True,
)
def get_current_time(timezone_name: str = "UTC") -> str:
    """获取指定时区的当前时间（ISO 8601 格式）。

    Args:
        timezone_name: IANA 时区名（如 "UTC" / "Asia/Shanghai" / "America/New_York"）。

    Returns:
        ISO 8601 时间字符串，如 "2026-09-30T14:24:00+08:00"。

    Raises:
        ToolError: timezone_name 不是合法 IANA 时区名。
            不再静默回退 UTC —— 否则 LLM 会拿到误导性时间后自行 +8 兜底,
            这种"瞎猫碰上死耗子"的成功比明确失败更危险。
    """
    try:
        tz = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        msg = (
            f"未知时区: {timezone_name!r}。"
            f"请使用 IANA 时区名(如 'UTC' / 'Asia/Shanghai' / 'America/New_York')。"
        )
        raise ToolError(msg) from exc
    return datetime.now(tz).isoformat()
