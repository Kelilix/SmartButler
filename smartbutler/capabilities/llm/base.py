"""LLM 能力层 — 抽象接口与异常体系（按文档 §5.2）。

设计原则：
1. **接口最小化**：BaseLLM 只暴露 chat() 与 chat_stream() 两个方法；
   tool 选择、温度等参数由各实现自行提供或在 chat() 的可选参数里收敛。
2. **异步优先**：默认全部 async,LangGraph 节点本来就跑在事件循环里。
3. **可观测性埋点**：异常体系细分,方便上层做重试 / 降级 / 监控。
4. **协议无关**：本模块不 import 任何具体厂商 SDK,确保后续切换厂商不影响上层。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import Any

from smartbutler.capabilities.llm.types import LLMResponse, Message, StreamChunk, ToolSpec


class LLMError(Exception):
    """LLM 调用的基类异常。

    所有具体厂商适配器抛出的异常都应继承本类,业务层可以 except LLMError 一并捕获。
    """


class LLMAuthError(LLMError):
    """认证失败(401/403)——通常是 API key 错误或配额问题,不应重试。"""


class LLMRateLimitError(LLMError):
    """限流(429)——可以退避后重试。"""


class LLMContextLengthError(LLMError):
    """上下文超长(400 + context_length_exceeded)——需要上层做截断。"""


class LLMTimeoutError(LLMError):
    """请求超时——可重试。"""


class LLMUpstreamError(LLMError):
    """上游服务错误(500/502/503)——可重试。"""


class LLMContentFilterError(LLMError):
    """内容被策略拦截——不应重试,需要修改 prompt。"""


class BaseLLM(ABC):
    """LLM 抽象基类。

    所有具体厂商(OpenAI / Anthropic / Ollama / DeepSeek / Qwen ...) 必须继承本类。
    LangGraph 节点仅依赖 BaseLLM 抽象,不直接 import 具体实现。
    """

    @abstractmethod
    async def chat(
        self,
        messages: list[Message],
        *,
        tools: list[ToolSpec] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        extra_body: dict[str, Any] | None = None,
    ) -> LLMResponse:
        """发送一次完整对话,返回模型完整响应。

        Args:
            messages: 对话历史,按时间顺序排列。
            tools: 可用工具列表;None 表示不开放工具调用。
            temperature: 覆盖默认温度;None 表示用配置默认值。
            max_tokens: 覆盖默认 max_tokens;None 表示用配置默认值。
            extra_body: 额外请求体字段,直接透传给底层 API
                        （如 DeepSeek thinking={"type":"disabled"}）。

        Returns:
            LLMResponse: 模型响应(含文本 / 工具调用 / 用量)。

        Raises:
            LLMAuthError / LLMRateLimitError / LLMContextLengthError /
            LLMTimeoutError / LLMUpstreamError / LLMContentFilterError
        """

    @abstractmethod
    def chat_stream(
        self,
        messages: list[Message],
        *,
        tools: list[ToolSpec] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        extra_body: dict[str, Any] | None = None,
    ) -> AsyncIterator[StreamChunk]:
        """发送一次对话,以流式方式逐 chunk 返回。

        异步生成器,业务层用 async for 消费。
        异常通过抛出 GeneratorExit 之外的正常异常传递(同 chat())。

        Args:
            messages: 对话历史。
            tools: 可用工具列表;None 表示不开放工具调用。
            temperature: 覆盖默认温度;None 表示用配置默认值。
            max_tokens: 覆盖默认 max_tokens;None 表示用配置默认值。
            extra_body: 额外请求体字段（如 DeepSeek thinking 参数）。

        子类必须实现为 ``async def`` + ``yield`` 的异步生成器,返回类型可窄化为
        ``AsyncGenerator[StreamChunk, None]``。
        """
        raise NotImplementedError

    async def aclose(self) -> None:  # noqa: B027
        """关闭底层资源(连接池等)。

        BaseLLM 默认无操作;子类如有 httpx.AsyncClient 等需要关闭的资源请 override。
        """
