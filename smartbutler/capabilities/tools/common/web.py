"""网页查询工具（HTTP GET + HTML 转纯文本）。

提供:
- web_fetch: 给定 URL 抓取页面内容，转为纯文本返回。

注意:
- Phase 2 仅做基础 HTTP 抓取，**不**做反爬绕过 / JS 渲染（那需要 Playwright / Puppeteer）。
- 大文档截断到 max_length 字符，避免撑爆 LLM context window。
- HTML → 文本用 Python 内置 html.parser（无额外依赖）。
- 业务 LLM 真要做网页搜索，请走 search Sub-Agent（Phase 3 接入搜索引擎 API）。
"""

from __future__ import annotations

import html
import re
from html.parser import HTMLParser

import httpx

from smartbutler.capabilities.tools.decorator import register_tool


class _TextExtractor(HTMLParser):
    """极简 HTML → 纯文本提取器。

    策略:
    - script/style/nav/header/footer 内容丢弃
    - 块级元素（div/p/br/h1-h6/li）后加换行
    - 其余标签只剥不替换
    - HTML 实体反转义（&amp; → &）
    """

    _DROP_TAGS = frozenset({"script", "style", "nav", "header", "footer", "noscript", "aside"})
    _BLOCK_TAGS = frozenset(
        {"p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "table"}
    )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self._buf: list[str] = []
        self._drop_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._DROP_TAGS:
            self._drop_depth += 1
            return
        if tag in self._BLOCK_TAGS:
            self._buf.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._DROP_TAGS:
            if self._drop_depth > 0:
                self._drop_depth -= 1
            return
        if tag in self._BLOCK_TAGS:
            self._buf.append("\n")

    def handle_data(self, data: str) -> None:
        if self._drop_depth == 0:
            self._buf.append(data)

    def text(self) -> str:
        # 反转义 + 合并多空白
        raw = html.unescape("".join(self._buf))
        # 把连续空白 / 换行压缩：保留段落分隔（双换行），去掉多余空白
        cleaned = re.sub(r"[ \t]+", " ", raw)
        cleaned = re.sub(r"\n[ \t]+", "\n", cleaned)
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
        return cleaned.strip()


@register_tool(
    name="web_fetch",
    description=(
        "抓取指定 URL 的网页内容，转为纯文本返回。"
        "会自动剥离 script/style/导航等噪音，保留正文。"
        "返回内容会被截断到 max_length 字符。"
        "场景: 用户要求'帮我看看这篇链接讲了什么'。"
    ),
    readonly=True,
    timeout_seconds=15.0,
)
async def web_fetch(url: str, max_length: int = 5000) -> str:
    """HTTP GET 抓取 URL 并提取正文。

    Args:
        url: 目标网址（必须 http/https）。
        max_length: 返回文本最大字符数（超出截断并标注）。

    Returns:
        提取后的纯文本内容 + 元信息（HTTP 状态 / 编码 / 实际长度）。

    Raises:
        ValueError: url 协议非法。
        RuntimeError: HTTP 错误（非 2xx）或网络层失败。
    """
    if not url.startswith(("http://", "https://")):
        msg = f"web_fetch 仅支持 http(s) URL: {url!r}"
        raise ValueError(msg)

    timeout = httpx.Timeout(15.0, connect=5.0)
    async with httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=True,
        headers={"User-Agent": "SmartButler/0.1 (+https://github.com/smartbutler)"},
    ) as client:
        try:
            resp = await client.get(url)
        except httpx.HTTPError as exc:
            msg = f"web_fetch 网络错误: {type(exc).__name__}: {exc}"
            raise RuntimeError(msg) from exc

    if resp.status_code >= 400:
        msg = f"web_fetch HTTP {resp.status_code} for {url}"
        raise RuntimeError(msg)

    content_type = resp.headers.get("content-type", "")
    html_body = resp.text

    # 非 HTML（如纯文本 / JSON）直接返回
    if "html" not in content_type.lower():
        body = html_body
    else:
        extractor = _TextExtractor()
        extractor.feed(html_body)
        extractor.close()
        body = extractor.text()

    truncated = len(body) > max_length
    if truncated:
        body = body[:max_length] + f"\n\n... [已截断，原文 {len(body)} 字符]"

    meta = f"[HTTP {resp.status_code}, content-type={content_type.split(';')[0]}, length={len(body)}]"
    return f"{meta}\n\n{body}"
