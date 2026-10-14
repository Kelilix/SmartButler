"""QwenEmbedder 单测(Phase 6.2 P0)。

用 respx mock HTTP,验证:
- HTTP 成功路径
- HTTP 4xx/5xx 抛 EmbedderError
- 超时抛 EmbedderError
- 响应结构不对抛 EmbedderError
- 无 api_key 构造时抛错
- batch 顺序对齐
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from smartbutler.capabilities.embedding.base import EmbedderError
from smartbutler.capabilities.embedding.config import EmbeddingSettings
from smartbutler.capabilities.embedding.qwen import QwenEmbedder


def _settings(api_key: str = "test-key", model: str = "qwen3.7-text-embedding-flash") -> EmbeddingSettings:
    return EmbeddingSettings(
        api_key=api_key,
        base_url="https://example.com/v1",
        model=model,
        dimension=1024,
        timeout=5.0,
    )


def _embed_response(vectors: list[list[float]]) -> dict:
    return {
        "data": [{"embedding": v, "index": i} for i, v in enumerate(vectors)],
    }


@pytest.mark.asyncio
async def test_qwen_requires_api_key() -> None:
    """无 api_key 构造抛 EmbedderError。"""
    s = EmbeddingSettings(api_key=None, base_url="https://example.com/v1", dimension=8)
    with pytest.raises(EmbedderError):
        QwenEmbedder(s)


@pytest.mark.asyncio
async def test_qwen_embed_success() -> None:
    """HTTP 200 + 合法响应,返回向量。"""
    s = _settings()
    q = QwenEmbedder(s)
    try:
        with respx.mock(base_url=s.base_url) as mock:
            route = mock.post("/embeddings").respond(
                200,
                json=_embed_response([[0.1] * 1024]),
            )
            v = await q.embed("hello")
            assert len(v) == 1024
            assert v[0] == pytest.approx(0.1)
            assert route.called
    finally:
        q.close()


@pytest.mark.asyncio
async def test_qwen_embed_batch_success() -> None:
    """批量调用顺序对齐。"""
    s = _settings()
    q = QwenEmbedder(s)
    try:
        with respx.mock(base_url=s.base_url) as mock:
            mock.post("/embeddings").respond(
                200,
                json=_embed_response([
                    [0.1] * 1024,
                    [0.2] * 1024,
                ]),
            )
            vs = await q.embed_batch(["a", "b"])
            assert len(vs) == 2
            assert vs[0][0] == pytest.approx(0.1)
            assert vs[1][0] == pytest.approx(0.2)
    finally:
        q.close()


@pytest.mark.asyncio
async def test_qwen_embed_batch_alignment_with_empty() -> None:
    """批量里有空文本,空位补 0 向量,顺序与输入一致。"""
    s = _settings()
    q = QwenEmbedder(s)
    try:
        with respx.mock(base_url=s.base_url) as mock:
            # 输入 ["a", "", "b"],实际发 ["a", "b"]
            mock.post("/embeddings").mock(
                side_effect=lambda req: httpx.Response(
                    200,
                    content=json.dumps(_embed_response([
                        [0.1] * 1024,
                        [0.3] * 1024,
                    ])).encode(),
                )
            )
            vs = await q.embed_batch(["a", "", "b"])
            assert len(vs) == 3
            assert vs[0][0] == pytest.approx(0.1)
            assert vs[1] == [0.0] * 1024  # 空位
            assert vs[2][0] == pytest.approx(0.3)
    finally:
        q.close()


@pytest.mark.asyncio
async def test_qwen_embed_http_4xx() -> None:
    """HTTP 4xx 抛 EmbedderError。"""
    s = _settings()
    q = QwenEmbedder(s)
    try:
        with respx.mock(base_url=s.base_url) as mock:
            mock.post("/embeddings").respond(401, json={"error": "unauthorized"})
            with pytest.raises(EmbedderError) as exc:
                await q.embed("hello")
            assert "401" in str(exc.value)
    finally:
        q.close()


@pytest.mark.asyncio
async def test_qwen_embed_http_5xx() -> None:
    """HTTP 5xx 抛 EmbedderError。"""
    s = _settings()
    q = QwenEmbedder(s)
    try:
        with respx.mock(base_url=s.base_url) as mock:
            mock.post("/embeddings").respond(500, text="internal error")
            with pytest.raises(EmbedderError):
                await q.embed("hello")
    finally:
        q.close()


@pytest.mark.asyncio
async def test_qwen_embed_timeout() -> None:
    """超时抛 EmbedderError。"""
    s = _settings()
    q = QwenEmbedder(s)
    try:
        with respx.mock(base_url=s.base_url) as mock:
            mock.post("/embeddings").mock(
                side_effect=httpx.TimeoutException("timeout"),
            )
            with pytest.raises(EmbedderError):
                await q.embed("hello")
    finally:
        q.close()


@pytest.mark.asyncio
async def test_qwen_embed_bad_response() -> None:
    """响应缺 data 字段抛 EmbedderError。"""
    s = _settings()
    q = QwenEmbedder(s)
    try:
        with respx.mock(base_url=s.base_url) as mock:
            mock.post("/embeddings").respond(200, json={"foo": "bar"})
            with pytest.raises(EmbedderError) as exc:
                await q.embed("hello")
            assert "data" in str(exc.value).lower() or "结构" in str(exc.value)
    finally:
        q.close()


@pytest.mark.asyncio
async def test_qwen_embed_empty_text() -> None:
    """空文本不调 HTTP,直接抛 EmbedderError。"""
    s = _settings()
    q = QwenEmbedder(s)
    try:
        with pytest.raises(EmbedderError) as exc:
            await q.embed("")
        assert "空" in str(exc.value)
    finally:
        q.close()


@pytest.mark.asyncio
async def test_qwen_dimension_property() -> None:
    """dimension 属性返回 settings.dimension。"""
    s = _settings()
    q = QwenEmbedder(s)
    try:
        assert q.dimension == 1024
    finally:
        q.close()
