"""InputAdapter / OutputAdapter 抽象接口（Phase 7.1）。

## 设计

Adapter 模式 —— 把"数据从哪来/到哪去"从 server 主体里抽出来:

- **InputAdapter** — 把外部输入(REPL / HA / 智能音箱 / Webhook)统一转成
  ``submit_reactive()`` 能接受的文本(或更结构化的输入,Phase 7.5 接 LLM 多模态)
- **OutputAdapter** — 把 server 产出的答复(文本 + 元数据)分发到目标通道
  (stdout / HA 设备 / 智能音箱 / Push)

Stub 实现见 :mod:`smartbutler.server.adapters.stub`。

## 不变量

- 适配器**不**持有业务状态(只做翻译),状态归 server 管
- ``OutputAdapter.push()`` 必须容忍多次调用、必须不抛异常(吞掉 + log)
- 同一 server 可挂多个 OutputAdapter(广播语义)
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# 数据类
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReactiveSubmission:
    """Reactive 输入的统一形态（Phase 7.1）。

    字段:
    - ``user_input``: 纯文本,直接喂给 LLM
    - ``user_id``: 谁说的(多 user 场景)
    - ``session_id``: LangGraph thread_id(多终端场景)
    - ``parent_agent``: 父调用方标签(默认 "user")
    - ``spoken_at``: 本条消息的"实际发生时间"(回灌历史用)
    - ``metadata``: 自由扩展(后续接 HA / 智能音箱可塞 device_id / intent 等)

    Phase 7.5+ 可在此基础上加 ``content_blocks: list[ContentBlock]`` 支持
    多模态(图片 / 音频),本阶段只走文本。

    🆕 7.4 metadata 协议注释(钩子 5):
        约定键(可加新键,但**不动 dataclass 字段**——沿用不变量 #8 协议稳定):
        - ``modality_tags: list[str]``
            - 文本交互 → ``["text"]``
            - VoiceAdapter(7.6) → ``["text", "audio"]``
            - CameraAdapter(7.8) → ``["text", "image", "video"]``
        - ``image_ref: str``  —— 7.8 写入原始图片路径
        - ``audio_ref: str``  —— 7.6 写入原始音频路径
        7.9 多模态召回时按 modality_tags 过滤;7.4-7.8 业务层不读这些键。
    """

    user_input: str
    user_id: str = "user"
    session_id: str = "default"
    parent_agent: str = "user"
    spoken_at: Any = None  # datetime | None —— 避免循环 import
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProactiveSubmission:
    """Proactive 输入的统一形态（Phase 7.1）。

    字段:
    - ``topic``: 事件 topic,会传给 EventBus
    - ``user_id``: 谁触发
    - ``payload``: 事件 payload(BaseEvent.payload 同形)
    - ``device_id``: 可选,设备类事件必填
    - ``source``: ``"device"`` / ``"timer"`` / ``"voice"`` / ``"webhook"``
    - ``priority``: ``"silent"`` / ``"normal"`` / ``"urgent"``
    - ``metadata``: 自由扩展
    """

    topic: str
    user_id: str = "user"
    payload: dict[str, Any] = field(default_factory=dict)
    device_id: str | None = None
    source: str = "device"
    priority: str = "normal"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ButlerReply:
    """Server → OutputAdapter 的答复形态。

    字段:
    - ``content``: 文本答复
    - ``user_id``: 哪个 user 的答复
    - ``session_id``: 哪个 session 的答复(用于路由回原通道)
    - ``source``: 哪条入口产生 — ``"reactive"`` / ``"proactive"``
    - ``related_event_id``: Proactive 时挂的关联事件 ID
    - ``metadata``: 自由扩展(后续可塞 tool_call 统计 / latency 等)

    🆕 7.4 metadata 协议注释(钩子 5):
        约定键(可加新键,**不动 dataclass 字段**——沿用不变量 #8 协议稳定):
        - ``modality_tags: list[str]``
            - 纯文本答复 → ``["text"]``
            - 含 TTS(7.7) → ``["text", "audio"]``
        - ``latency_ms: float``  —— 后续 7.5 性能监控
        - ``tool_call_count: int`` —— 后续 7.5 工具调用统计
    """

    content: str
    user_id: str = "user"
    session_id: str = "default"
    source: str = "reactive"
    related_event_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Adapter 抽象
# ---------------------------------------------------------------------------


class InputAdapter(ABC):
    """输入适配器抽象基类。

    实现类负责:
    1. 从外部通道(REPL / HA / 智能音箱 / Webhook)读输入
    2. 翻译成 :class:`ReactiveSubmission` 或 :class:`ProactiveSubmission`
    3. 通过 ``submit`` 回调送入 server

    Stub / 测试场景下也用于把 server 包装成"无外部通道"的纯计算节点。
    """

    @abstractmethod
    async def start(self) -> None:
        """启动后台读循环(REPL 阻塞 input / HA 轮询 / 智能音箱 SDK 回调)。"""
        ...

    @abstractmethod
    async def stop(self) -> None:
        """停止读循环,等待当前 in-flight 任务完成。"""
        ...

    @abstractmethod
    def set_sink(
        self,
        submit_reactive: Callable[[ReactiveSubmission], Awaitable[ButlerReply]],
        submit_proactive: Callable[[ProactiveSubmission], Awaitable[None]],
    ) -> None:
        """注入 server 的两个入口回调(避免 adapter 反向依赖 server)。

        必须在 ``start()`` 之前调,否则 adapter 不知道往哪送。
        """
        ...


class OutputAdapter(ABC):
    """输出适配器抽象基类。

    实现类负责:
    1. 接收 :class:`ButlerReply`
    2. 翻译成目标通道的协议(stdout 打印 / HA 设备 TTS / Push 推送)

    设计原则:``push`` 内部不抛异常(吞掉 + log),server 主体不被下游卡住。
    """

    @abstractmethod
    async def push(self, reply: ButlerReply) -> None:
        """把答复推送到目标通道。

        实现要求:
        - 内部 try/except,失败 log + 继续(不能让 server 崩)
        - 同步 / 异步实现都支持(协议里就是 async)
        """
        ...


from smartbutler.server.adapters.stub import (
    InMemoryInputAdapter,
    InMemoryOutputAdapter,
    PrintOutputAdapter,
)

__all__ = [
    "ReactiveSubmission",
    "ProactiveSubmission",
    "ButlerReply",
    "InputAdapter",
    "OutputAdapter",
    "InMemoryInputAdapter",
    "InMemoryOutputAdapter",
    "PrintOutputAdapter",
]
