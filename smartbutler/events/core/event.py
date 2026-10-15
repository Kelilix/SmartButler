"""事件基类与协议字段（Phase 7）。

## 设计原则

1. **不可变**：事件一旦产生,内容不可改;Normalizer 如需改 → 产生新事件
2. **强类型**：所有字段用 Pydantic 校验,设备 adapter 入参错误立即报错
3. **协议稳定**：BaseEvent 字段一旦敲定,Phase 7 启动后**不删不改**（详见 ADR-008 §5）

## 字段语义

| 字段 | 含义 | 必填 | 示例 |
|------|------|------|------|
| ``event_id`` | 事件唯一 ID（用于 dedup） | ✅ | "evt_20261008_184501_abc123" |
| ``source`` | 事件来源分类 | ✅ | EventSource.DEVICE |
| ``topic`` | 事件总线 topic | ✅ | "device.door.opened" |
| ``user_id`` | 关联用户（设备主） | ✅ | "gaotianyu" |
| ``device_id`` | 设备 ID | 条件 | "door_front_01" |
| ``timestamp`` | 事件时间（设备时间,非接收时间） | ✅ | datetime(2026, 10, 8, 18, 45) |
| ``priority`` | 优先级（路由策略） | ✅ | EventPriority.NORMAL |
| ``payload`` | 业务负载（设备 adapter 自由定义） | ✅ | {"open_method": "fingerprint"} |
| ``parent_event_id`` | 联动事件链（热水器在门开 5 分钟前启动） | 否 | "evt_20261008_184001_xyz" |
| ``correlation_id`` | 关联 ID（多事件描述同一业务） | 否 | "session_arrival_20261008" |

## 不变量

- ``event_id`` 全局唯一,由 DeviceAdapter 生成
- ``timestamp`` 必须带时区（aware datetime）
- ``payload`` 必须是 JSON 序列化的 dict（不可塞二进制/类实例）
"""
from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


class EventSource(StrEnum):
    """事件来源分类。

    决定 parent_agent 协议前缀:
    - DEVICE → ``trigger:device.<subtype>``
    - VOICE  → ``trigger:voice.<subtype>``
    - TIMER  → ``trigger:timer.<subtype>``
    - WEBHOOK → ``trigger:webhook.<subtype>``
    """

    DEVICE = "device"          # 智能门锁 / 灯 / 空调 / 热水器 ...
    VOICE = "voice"            # 智能音响 / 麦克风阵列唤醒
    TIMER = "timer"            # 定时器 / 闹钟
    WEBHOOK = "webhook"        # 外部 API 回调


class EventPriority(StrEnum):
    """事件优先级。

    决定 AnswerRouter 路由策略:
    - SILENT  — 丢弃,不通知（用于 ack 事件等）
    - NORMAL  — 推默认设备
    - URGENT  — 推全屋设备,加大音量
    """

    SILENT = "silent"
    NORMAL = "normal"
    URGENT = "urgent"


# ---------------------------------------------------------------------------
# 🆕 Phase 7.6 声纹识别(Speaker Diarization + Recognition)扩展位
# ---------------------------------------------------------------------------
# 设计动机(2026-10-15):
# 1. TOTHINK 1.1 场景:男主人"今天周几?"+女主人"明天要上班"——两个 user 同一个会话,
#    管家需要知道当前在跟谁对话,才能正确拆分 task 归属 + 情绪归属(Phase 8)。
# 2. 决策:Frozen BaseEvent 不允许改 user_id(代表"设备主",向后兼容),
#    但**新增** speakers 字段(多说话人)。这样 7.0-7.5 现有代码零修改,
#    7.6+ 才能在 Normalizer/Trigger 链路上感知"会话参与者"。
# 3. 不阻塞 7.6:本字段 7.5 阶段加好(协议占位),7.6 真声纹模型(resemblyzer/ecapa-tdnn)
#    落地时直接填 embedding + confidence,7.5 之前所有调用方都把它当空 list 即可。
# 4. SpeakerRegistry(7.6 实现)负责"embedding → user_id"映射,本文件只定义协议。

# 声纹嵌入默认维度(7.6 选型 resemblyzer=256 / ecapa-tdnn=192,预留到 512)
SPEAKER_EMBEDDING_DIM = 512

# 声纹匹配阈值(0.0-1.0 余弦相似度),低于此值视为陌生人(register 失败)
SPEAKER_MATCH_THRESHOLD = 0.70


class SpeakerInfo(BaseModel):
    """单说话人信息(7.6 声纹识别落地点)。

    Attributes:
        user_id: 识别的 user_id(匹配成功时 = 已注册 user;陌生人/未匹配时 = "unknown:<hash>")。
        confidence: 识别置信度(0.0-1.0,余弦相似度)。Phase 8 情绪治理按此排序情绪贡献。
        is_known: 是否匹配到已注册家庭成员。True=已知 user,False=陌生人(待用户注册)。
        embedding: 声纹向量(512 维),7.6 真声纹模型算;7.5 阶段默认 None(占位)。
        voice_segment_id: 同一段语音内的说话人 ID(0/1/2...),用于 VAD 切分后的片段归属。
            例:一段 5s 音频里妈妈说 2s + 爸爸说 3s,会生成 2 个 SpeakerInfo,segment_id=0/1。
        language: BCP-47 语种代码(从 voice_event 透传)。

    Note:
        7.5 阶段(当前):VoiceEvent.speakers 永远为空 list(协议占位)。
        7.6 阶段:VoiceAdapter 调 resemblyzer/ecapa-tdnn 算 embedding,写本字段。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    user_id: str = Field(..., min_length=1, description="识别的 user_id(陌生人为 unknown:xxx)")
    confidence: float = Field(..., ge=0.0, le=1.0, description="识别置信度(余弦相似度)")
    is_known: bool = Field(default=False, description="是否匹配到已注册家庭成员")
    embedding: list[float] | None = Field(
        default=None,
        description="声纹向量(7.6 真模型算;7.5 占位 = None)",
    )
    voice_segment_id: int = Field(
        default=0,
        ge=0,
        description="同一段语音内的说话人片段 ID(0/1/2...)",
    )
    language: str | None = Field(default=None, description="BCP-47 语种代码")


class BaseEvent(BaseModel):
    """事件基类。

    所有具体事件（DeviceEvent / VoiceEvent / TimerEvent）继承此类。
    DeviceAdapter 负责把厂商私有事件转成 BaseEvent 派发。

    不可变（model_config frozen=True）—— Normalizer 如需修改,生成新事件。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    # --- 必填字段 ---

    event_id: str = Field(
        default_factory=lambda: f"evt_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:8]}",
        description="事件唯一 ID,全局唯一",
    )
    source: EventSource = Field(..., description="事件来源分类")
    topic: str = Field(
        ...,
        min_length=1,
        max_length=200,
        description="事件总线 topic,推荐 'source.subtype.action' 三段式",
    )
    user_id: str = Field(..., min_length=1, description="关联用户 ID")
    timestamp: datetime = Field(..., description="事件时间（设备时间,带时区）")
    priority: EventPriority = Field(
        default=EventPriority.NORMAL,
        description="事件优先级,决定路由策略",
    )
    payload: dict[str, Any] = Field(
        default_factory=dict,
        description="业务负载,JSON 序列化的 dict",
    )

    # --- 可选字段 ---

    device_id: str | None = Field(
        default=None,
        description="设备 ID（device 类事件必填,其他类型可选）",
    )
    parent_event_id: str | None = Field(
        default=None,
        description="联动事件链 ID（描述因果关系）",
    )
    correlation_id: str | None = Field(
        default=None,
        description="关联 ID,多事件描述同一业务",
    )
    # 🆕 7.4 多模态扩展位(钩子 5)
    # 7.6/7.8 写入:{'image': '/data/butler/img/2026/10/14/xxx.jpg',
    #                'video': '/data/butler/vid/2026/10/14/xxx.mp4',
    #                'audio': '/data/butler/aud/2026/10/14/xxx.wav'}
    # 7.9 多模态 embedding 时直接读这个字段定位原始文件。
    # 不变量:Normalizer 看到此字段一律原样透传,不修改。
    media_refs: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "多模态媒体引用映射(7.4 扩展位)。\n"
            "7.6 VoiceAdapter 写入 audio_ref,7.8 CameraAdapter 写入 image/video_ref。\n"
            "7.9 启动后,Embedder.embed_image/embed_audio 直接读这个字段定位文件。\n"
            "默认空 dict;老调用零修改。\n"
            "不变量(Normalizer):本字段必须原样透传,不修改。"
        ),
    )

    # 🆕 7.6 声纹识别扩展位(钩子 6,2026-10-15)
    # VoiceEvent 写入:VoiceAdapter 调 resemblyzer/ecapa-tdnn 算声纹 embedding,
    # 然后 SpeakerRegistry 匹配已注册家庭成员,得到 user_id + confidence。
    # 7.5 阶段(当前):永远空 list,7.6 之前所有调用方零修改。
    # 7.6 启动后:Normalizer 看到非空 speakers → 透传;Trigger 调 ainvoke 时
    # 把 speakers 转成 multi_user_ids 传给 orchestrator(向后兼容单 user 场景)。
    # 不变量(7.6+):Normalizer/Trigger **不修改** speakers 字段,只读。
    speakers: list[SpeakerInfo] = Field(
        default_factory=list,
        description=(
            "本事件中的说话人列表(7.6 声纹扩展位)。\n"
            "VoiceEvent 专属字段(其他事件类型永远空 list)。\n"
            "TOTHINK 1.1 场景:男主人 + 女主人交替说话 → speakers=[SpeakerInfo(user_id=dad, ...), SpeakerInfo(user_id=mom, ...)]。\n"
            "默认值空 list,7.5 阶段所有现有调用零修改。\n"
            "不变量(Normalizer):本字段必须原样透传,不修改。"
        ),
    )

    # --- 派生方法 ---

    @property
    def parent_agent_tag(self) -> str:
        """派生 parent_agent 协议字段,供 EventTrigger 用。

        Examples:
            >>> e = BaseEvent(topic="device.door.opened", ...)
            >>> e.parent_agent_tag
            'trigger:device.door'
        """
        # topic 前 2 段 → parent_agent
        # 例: "device.door.opened" → "trigger:device.door"
        parts = self.topic.split(".")
        if len(parts) >= 2:
            return f"trigger:{parts[0]}.{parts[1]}"
        return f"trigger:{self.source.value}"

    def to_user_input(self) -> str:
        """派生 user_input 文本,供 EventTrigger 调 ainvoke 用。

        默认实现：``"[系统事件] <topic> user_id=<user_id> device=<device_id> payload=<json>"``
        子类可重写以产出更自然的语言。
        """
        import json

        parts: list[str] = [f"[系统事件] {self.topic}"]
        parts.append(f"user_id={self.user_id}")
        if self.device_id:
            parts.append(f"device_id={self.device_id}")
        parts.append(f"priority={self.priority.value}")
        parts.append(f"timestamp={self.timestamp.isoformat()}")
        if self.payload:
            parts.append(f"payload={json.dumps(self.payload, ensure_ascii=False)}")
        return " ".join(parts)


__all__ = [
    "BaseEvent",
    "EventSource",
    "EventPriority",
    "SpeakerInfo",
    "SPEAKER_EMBEDDING_DIM",
    "SPEAKER_MATCH_THRESHOLD",
]
