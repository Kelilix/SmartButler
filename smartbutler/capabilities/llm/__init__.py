"""LLM 能力层 — 公共 API。

按文档 §5.2,本包对外只暴露：
- 抽象：BaseLLM + 异常体系
- 数据类型：Message / Role / ToolSpec / ToolCall / LLMResponse / StreamChunk
- 工厂：create_llm

具体实现（OpenAICompatibleLLM 等）不导出,业务层必须通过 create_llm() 路由获取。
"""

from smartbutler.capabilities.llm.base import (
    BaseLLM,
    LLMAuthError,
    LLMContentFilterError,
    LLMContextLengthError,
    LLMError,
    LLMRateLimitError,
    LLMTimeoutError,
    LLMUpstreamError,
)
from smartbutler.capabilities.llm.factory import create_llm
from smartbutler.capabilities.llm.types import (
    FinishReason,
    FunctionCall,
    FunctionSpec,
    LLMResponse,
    Message,
    Role,
    StreamChunk,
    ToolCall,
    ToolSpec,
    Usage,
)

__all__ = [
    # 抽象
    "BaseLLM",
    "LLMError",
    "LLMAuthError",
    "LLMRateLimitError",
    "LLMContextLengthError",
    "LLMTimeoutError",
    "LLMUpstreamError",
    "LLMContentFilterError",
    # 数据类型
    "Role",
    "Message",
    "FunctionSpec",
    "FunctionCall",
    "ToolSpec",
    "ToolCall",
    "Usage",
    "FinishReason",
    "LLMResponse",
    "StreamChunk",
    # 工厂
    "create_llm",
]
