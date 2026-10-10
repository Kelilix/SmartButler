"""Qwen Embedder(Phase 6.2 P0)。

调通义千问 embedding 端点(OpenAI 兼容协议)::
    POST {base_url}/embeddings
    Body: {"model": "...", "input": "..."}
    Response: {"data": [{"embedding": [...]}]}

失败处理:
- HTTP 4xx/5xx → ``EmbedderError``
- 超时 / 连接错误 → ``EmbedderError``
- 响应结构不对 → ``EmbedderError``

不实现重试(由调用方控制),不在本层做。
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from smartbutler.capabilities.embedding.base import Embedder, EmbedderError
from smartbutler.capabilities.embedding.config import EmbeddingSettings
from smartbutler.utils.logging import get_logger

logger = get_logger(__name__)


class QwenEmbedder:
    """通义千问 embedding 实现(1024 维,默认)。"""

    def __init__(self, settings: EmbeddingSettings) -> None:
        if not settings.api_key:
            raise EmbedderError(
                "QwenEmbedder 需要 api_key;请设 SMARTBUTLER_EMBEDDING_API_KEY。"
                "未配 api_key 时请用 build_default_embedder 走自动降级。"
            )
        self._settings = settings
        self._client = httpx.Client(
            base_url=settings.base_url,
            timeout=settings.timeout,
            headers={
                "Authorization": f"Bearer {settings.api_key}",
                "Content-Type": "application/json",
            },
        )

    @property
    def dimension(self) -> int:
        return self._settings.dimension

    async def embed(self, text: str) -> list[float]:
        """单文本嵌入。失败抛 ``EmbedderError``。"""
        if not text or not text.strip():
            raise EmbedderError("embed() 输入不能为空")
        try:
            resp = await self._embed_async([text])
        except EmbedderError:
            raise
        except Exception as exc:
            raise EmbedderError(f"Qwen embedding 调用失败: {exc}") from exc
        return resp[0]

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """批量嵌入(顺序与输入对齐)。失败抛 ``EmbedderError``。"""
        if not texts:
            return []
        # 过滤空文本,但保留位置映射,最后填回原位置
        non_empty_indices = [i for i, t in enumerate(texts) if t and t.strip()]
        non_empty_texts = [texts[i] for i in non_empty_indices]
        if not non_empty_texts:
            raise EmbedderError("embed_batch() 所有输入都为空")
        try:
            vectors = await self._embed_async(non_empty_texts)
        except EmbedderError:
            raise
        except Exception as exc:
            raise EmbedderError(f"Qwen embedding 批量调用失败: {exc}") from exc
        # 还原到原顺序(空位用 0 向量)
        out: list[list[float]] = [[0.0] * self.dimension for _ in texts]
        for idx, vec in zip(non_empty_indices, vectors):
            out[idx] = vec
        return out

    async def _embed_async(self, texts: list[str]) -> list[list[float]]:
        """内部:同步 httpx 用 run_in_executor 包成 async。"""
        import asyncio

        body: dict[str, Any] = {
            "model": self._settings.model,
            "input": texts if len(texts) > 1 else texts[0],
        }
        loop = asyncio.get_event_loop()
        try:
            resp = await loop.run_in_executor(
                None,
                lambda: self._client.post("/embeddings", content=json.dumps(body)),
            )
        except httpx.TimeoutException as exc:
            raise EmbedderError(f"Qwen embedding 超时: {exc}") from exc
        except httpx.HTTPError as exc:
            raise EmbedderError(f"Qwen embedding HTTP 错误: {exc}") from exc

        if resp.status_code != 200:
            raise EmbedderError(
                f"Qwen embedding HTTP {resp.status_code}: {resp.text[:300]}"
            )
        try:
            data = resp.json()
        except json.JSONDecodeError as exc:
            raise EmbedderError(f"Qwen embedding 响应非 JSON: {exc}") from exc
        items = data.get("data")
        if not isinstance(items, list) or not items:
            raise EmbedderError(
                f"Qwen embedding 响应结构异常:data 字段缺失或非列表。"
                f"原始: {str(data)[:200]}"
            )
        out: list[list[float]] = []
        for item in items:
            vec = item.get("embedding") if isinstance(item, dict) else None
            if not isinstance(vec, list):
                raise EmbedderError(
                    f"Qwen embedding 响应中 embedding 字段缺失: {item}"
                )
            out.append([float(x) for x in vec])
        if len(out) != len(texts):
            raise EmbedderError(
                f"Qwen embedding 返回数量 {len(out)} != 输入 {len(texts)}"
            )
        return out

    def close(self) -> None:
        """关闭 HTTP client。"""
        try:
            self._client.close()
        except Exception:  # noqa: BLE001
            pass


__all__ = ["QwenEmbedder"]
