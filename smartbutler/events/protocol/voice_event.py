"""语音事件协议（Phase 7）。

智能音响 / 麦克风阵列的唤醒 + ASR 文本事件 schema。

## 典型 topic

- ``voice.wake_word``              — 检测到唤醒词
- ``voice.command``                — 唤醒后用户说话
- ``voice.interrupt``              — 用户打断(管家说话中)
- ``voice.silent``                 — 唤醒后 5 秒无语音(放弃)

## 关键流程

```
[智能音响] 检测到唤醒词 →  voice.wake_word 事件
[智能音响] ASR 识别文本  →  voice.command 事件(含 asr_text)
[管家]     处理 command   →  调 Sub-Agent / 回复文本
[管家]     TTS 输出       →  智能音响播放
[用户]     打断管家说话    →  voice.interrupt 事件(管家停止 TTS)
```

## 关键字段

| 字段 | 必填 | 说明 |
|------|------|------|
| ``device_id`` | ✅ | 智能音响 / 麦克风设备 ID |
| ``wake_word`` | 条件 | 唤醒词(wake_word 类事件必填) |
| ``asr_text`` | 条件 | ASR 识别后的文本(command 类事件必填) |
| ``asr_confidence`` | ❌ | ASR 置信度 [0, 1] |
| ``audio_duration_ms`` | ❌ | 用户说话时长 |
| ``language`` | ❌ | 语种(zh-CN / en-US) |
"""
from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import Field

from smartbutler.events.core.event import BaseEvent, EventPriority, EventSource


class VoiceAction(StrEnum):
    """语音动作枚举。"""

    WAKE_WORD = "wake_word"      # 唤醒词
    COMMAND = "command"          # 唤醒后用户说话
    INTERRUPT = "interrupt"      # 用户打断
    SILENT = "silent"            # 唤醒后无语音(放弃)


class VoiceEvent(BaseEvent):
    """语音事件。"""

    # 必填
    action: VoiceAction = Field(..., description="语音动作枚举")
    device_id: str = Field(..., min_length=1, description="智能音响/麦克风设备 ID")

    # 条件必填
    wake_word: str | None = Field(default=None, description="唤醒词(wake_word 类事件必填)")
    asr_text: str | None = Field(default=None, description="ASR 识别文本(command 类事件必填)")

    # 可选
    asr_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    audio_duration_ms: int | None = Field(default=None, ge=0)
    language: str | None = Field(default=None, description="BCP-47 语种代码,如 zh-CN")
    audio_meta: dict[str, Any] | None = Field(
        default=None,
        description="音频元数据(采样率 / 声道 / VAD 信息)",
    )

    @classmethod
    def make(
        cls,
        *,
        action: VoiceAction,
        device_id: str,
        user_id: str,
        wake_word: str | None = None,
        asr_text: str | None = None,
        asr_confidence: float | None = None,
        audio_duration_ms: int | None = None,
        language: str | None = None,
        audio_meta: dict[str, Any] | None = None,
        priority: EventPriority = EventPriority.NORMAL,
        payload: dict[str, Any] | None = None,
        timestamp: datetime | None = None,
    ) -> VoiceEvent:
        """工厂方法。"""
        topic = f"voice.{action.value}"
        return cls(
            source=EventSource.VOICE,
            topic=topic,
            user_id=user_id,
            device_id=device_id,
            action=action,
            wake_word=wake_word,
            asr_text=asr_text,
            asr_confidence=asr_confidence,
            audio_duration_ms=audio_duration_ms,
            language=language,
            audio_meta=audio_meta,
            priority=priority,
            payload=payload or {},
            timestamp=timestamp or datetime.now().astimezone(),
        )

    def to_user_input(self) -> str:
        """重写:把 ASR 文本作为主要 user_input。"""
        if self.action == VoiceAction.COMMAND and self.asr_text:
            # 语音命令直接当 user_input 喂给 LLM
            return self.asr_text
        # 其他情况(唤醒/打断/静默)走标准事件格式
        parts: list[str] = [
            f"[系统事件] 语音事件: {self.action.value}",
            f"device_id={self.device_id}",
        ]
        if self.wake_word:
            parts.append(f"wake_word={self.wake_word}")
        if self.asr_text:
            parts.append(f"asr_text={self.asr_text}")
        return " ".join(parts)


__all__ = ["VoiceEvent", "VoiceAction"]
