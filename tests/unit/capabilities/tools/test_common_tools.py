"""common tools 单元测试。

get_current_time: 纯函数，不打外部
web_fetch: 需要外部 HTTP，使用 respx mock httpx 请求
"""

from __future__ import annotations

import asyncio
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
import pytest
import respx

from smartbutler.capabilities.tools.common.datetime import get_current_time
from smartbutler.capabilities.tools.common.web import web_fetch
from smartbutler.capabilities.tools.registry import ToolRegistry
from smartbutler.capabilities.tools.types import (
    Permission,
    ToolContext,
)


def _ctx(perms: set[Permission] | None = None) -> ToolContext:
    return ToolContext(user_id="u", session_id="s", permissions=perms or set())


def _has_tzdata(timezone_name: str) -> bool:
    """检测系统是否装了 tzdata（Python 3.14 默认不带）。"""
    try:
        ZoneInfo(timezone_name)
        return True
    except ZoneInfoNotFoundError:
        return False


# ---------- get_current_time ----------


class TestGetCurrentTime:
    def test_returns_iso_format(self) -> None:
        result = get_current_time()
        assert re.match(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", result)

    def test_utc_timezone(self) -> None:
        result = get_current_time("UTC")
        assert "+00:00" in result

    @pytest.mark.skipif(
        not _has_tzdata("Asia/Shanghai"),
        reason="tzdata 未安装,系统会回退 UTC",
    )
    def test_shanghai_timezone_with_tzdata(self) -> None:
        result = get_current_time("Asia/Shanghai")
        assert "+08:00" in result

    def test_unknown_timezone_fallback(self) -> None:
        """未知时区必须 fail-loud(抛 ToolError),不静默回退 UTC。

        设计决策(见 datetime.py:64-66 注释):
        静默回退 UTC 会让 LLM 拿到误导性时间后自行 +8 兜底,
        这种"瞎猫碰上死耗子"的成功比明确失败更危险。
        """
        from smartbutler.capabilities.tools.types import ToolError

        with pytest.raises(ToolError, match="未知时区"):
            get_current_time("Mars/Olympus")

    def test_tool_registered(self) -> None:
        # 模块导入已触发 @register_tool
        registry = ToolRegistry.get_default()
        assert "get_current_time" in registry
        tool = registry.get("get_current_time")
        assert tool.readonly is True
        assert tool.idempotent is True

    def test_tool_ainvoke(self) -> None:
        tool = ToolRegistry.get_default().get("get_current_time")
        result = asyncio.run(tool.ainvoke(_ctx(), timezone_name="UTC"))
        assert result.success is True
        assert "+00:00" in result.content


# ---------- web_fetch ----------


class TestWebFetchInputValidation:
    async def test_non_http_url_raises(self) -> None:
        with pytest.raises(ValueError, match="http"):
            await web_fetch("ftp://example.com/file")

    async def test_empty_url_raises(self) -> None:
        with pytest.raises(ValueError):
            await web_fetch("")


class TestWebFetchHTTPBehavior:
    """使用 respx mock httpx 请求，不依赖外部网络。"""

    async def test_successful_html_fetch(self, respx_mock: respx.MockRouter) -> None:
        respx_mock.get("https://example.com/page").mock(
            return_value=httpx.Response(
                200,
                text="<html><head><title>Hi</title></head>"
                "<body><h1>Welcome</h1><p>Hello <b>World</b></p>"
                "<script>alert('x')</script></body></html>",
                headers={"content-type": "text/html; charset=utf-8"},
            )
        )
        result = await web_fetch("https://example.com/page")
        assert "Welcome" in result
        assert "Hello World" in result
        # script 应被剥离
        assert "alert" not in result
        # meta 行
        assert "HTTP 200" in result

    async def test_truncation(self, respx_mock: respx.MockRouter) -> None:
        long_body = "<p>" + ("x" * 10000) + "</p>"
        respx_mock.get("https://example.com/long").mock(
            return_value=httpx.Response(
                200, text=long_body, headers={"content-type": "text/html"}
            )
        )
        result = await web_fetch("https://example.com/long", max_length=100)
        assert "已截断" in result
        # 截断后总长不超过 100 + 截断说明
        assert len(result) < 250

    async def test_http_error_raises(self, respx_mock: respx.MockRouter) -> None:
        respx_mock.get("https://example.com/404").mock(
            return_value=httpx.Response(404, text="Not Found")
        )
        with pytest.raises(RuntimeError, match="HTTP 404"):
            await web_fetch("https://example.com/404")

    async def test_network_error_raises(self, respx_mock: respx.MockRouter) -> None:
        respx_mock.get("https://example.com/error").mock(
            side_effect=httpx.ConnectError("network down")
        )
        with pytest.raises(RuntimeError, match="网络错误"):
            await web_fetch("https://example.com/error")

    async def test_non_html_response_passthrough(
        self, respx_mock: respx.MockRouter
    ) -> None:
        respx_mock.get("https://example.com/data.json").mock(
            return_value=httpx.Response(
                200,
                text='{"k": "v"}',
                headers={"content-type": "application/json"},
            )
        )
        result = await web_fetch("https://example.com/data.json")
        assert '"k": "v"' in result


class TestWebFetchIntegration:
    def test_tool_registered(self) -> None:
        # 模块导入已触发 @register_tool
        registry = ToolRegistry.get_default()
        assert "web_fetch" in registry
        tool = registry.get("web_fetch")
        assert tool.readonly is True
        assert tool.timeout_seconds == 15.0

    async def test_tool_ainvoke_success(self, respx_mock: respx.MockRouter) -> None:
        respx_mock.get("https://example.com/ok").mock(
            return_value=httpx.Response(
                200, text="<p>OK</p>", headers={"content-type": "text/html"}
            )
        )
        tool = ToolRegistry.get_default().get("web_fetch")
        result = await tool.ainvoke(_ctx(), url="https://example.com/ok")
        assert result.success is True
        assert "OK" in result.content

    async def test_tool_ainvoke_http_error_propagates(
        self, respx_mock: respx.MockRouter
    ) -> None:
        """HTTP 错误会抛 RuntimeError（业务普通异常,不是 ToolError 子类）。"""
        respx_mock.get("https://example.com/500").mock(
            return_value=httpx.Response(500, text="err")
        )
        tool = ToolRegistry.get_default().get("web_fetch")
        with pytest.raises(RuntimeError):
            await tool.ainvoke(_ctx(), url="https://example.com/500")
