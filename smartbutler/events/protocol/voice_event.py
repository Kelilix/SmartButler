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

from smartbutler.events.core.event import (
    BaseEvent,
    EventPriority,
    EventSource,
    SpeakerInfo,
)


class VoiceAction(StrEnum):
    """语音动作枚举。"""

    WAKE_WORD = "wake_word"      # 唤醒词
    COMMAND = "command"          # 唤醒后用户说话
    INTERRUPT = "interrupt"      # 用户打断
    SILENT = "silent"            # 唤醒后无语音(放弃)
    # 🆕 2026-11-01 (Phase 7.6.1): 声纹采集同意窗口内的用户回答。
    #
    # 场景:Agent 问"方便让我记住你的声音吗?" → 用户回"要"/"不用"
    # → VoiceAdapter 在 CONFIRMING 状态下发这个事件,把原话交给 LLM 判定。
    #
    # 为什么不直接发 COMMAND:
    #   COMMAND 会触发完整的"理解意图 → 作答 → 工具调用"链路,
    #   而这里 LLM 只需要做一个**是/否**判断。用独立 action 可以让
    #   路由层把它导向专门的确认处理分支,避免误触发普通对话逻辑。
    CONFIRM_ANSWER = "confirm_answer"


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
    # 🆕 2026-11-01 (Phase 7.6.1): 是否**建议**询问用户登记声纹。
    #
    # ⚠️ 语义边界(极易误解):
    #   True = "这个人是陌生人,且音频质量达标,**可以**问用户要不要录声纹"
    #   False = 不建议问
    #   **不代表已经采集**,也不代表用户已经同意。
    #
    # 改造前:VoiceAdapter 判定陌生人后**直接**启动采集(后台静默)。
    # 改造后:VoiceAdapter 只**标记**,由 Agent(LLM)决定要不要开口打断用户
    #         —— 用户可能只是随口一句话,不值得为此打断。
    #
    # 真正的采集启动必须走:用户明确同意 → LLM 调 enroll_speaker(confirm=True)。
    suggest_enrollment: bool = Field(
        default=False,
        description="是否建议询问用户登记声纹(仅表示'可以问',不代表已采集/已同意)",
    )
    # 🆕 2026-11-01 (Phase 7.6.1): 触发建议的原因(可观测/调试)。
    # 取值见 SpeakerRegistry.should_trigger_enrollment():
    #   "stranger" / "empty_registry" / "no_registry"
    enrollment_reason: str = Field(
        default="",
        description="建议询问登记声纹的原因(如 stranger / empty_registry)",
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
        speakers: list[SpeakerInfo] | None = None,
        suggest_enrollment: bool = False,
        enrollment_reason: str = "",
    ) -> VoiceEvent:
        """工厂方法。

        🆕 7.6 声纹扩展:``speakers`` 传入声纹识别结果(7.5 阶段默认 None = 空 list)。
        7.6 之前不传,默认空 list,老调用零修改。

        🆕 2026-11-01 (Phase 7.6.1): ``suggest_enrollment`` / ``enrollment_reason``
        告诉 Agent"这个人是陌生人,可以问要不要录声纹"。**不代表已采集**。
        """
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
            speakers=speakers or [],
            suggest_enrollment=suggest_enrollment,
            enrollment_reason=enrollment_reason,
        )

    def to_user_input(self) -> str:
        """重写:把 ASR 文本作为主要 user_input,并把声纹识别的说话人列表拼接到 LLM 输入。

        拼接格式(7.6 声纹落地后):
            "<asr_text>  [会话参与者:<user1>(<conf1>)+<user2>(<conf2>)]"

        7.5 阶段(当前):speakers 永远空,只输出 asr_text,行为不变。
        7.6 阶段:有 speakers 时拼会话参与者列表,LLM 知道当前在跟谁对话。

        TOTHINK 1.1 场景:
            - 妈妈说"明天怎么又要上班了" → speakers=[mom(conf=0.95)]
            - 爸爸说"哦对了,明天我要出差" → speakers=[dad(conf=0.92)]
            - 同会话中两个 user 交替 → 后续 event 的 speakers 反映当前发言者
        """
        if self.action == VoiceAction.COMMAND and self.asr_text:
            # 主输入:ASR 文本
            parts: list[str] = [self.asr_text]
            # 7.6 扩展:拼声纹识别的说话人列表(让 LLM 知道在跟谁对话)
            if self.speakers:
                speaker_strs = []
                for sp in self.speakers:
                    # 🆕 2026-11-01: 优先显示姓名(display_name),
                    # 让 LLM 读得懂"爸爸说"/"妈妈说"而不是一串 speaker_xxx。
                    if sp.is_known:
                        tag = sp.display_name or sp.user_id
                    else:
                        tag = f"陌生人({sp.user_id})"
                    speaker_strs.append(f"{tag}({sp.confidence:.0%})")
                parts.append(f"[会话参与者:{'+'.join(speaker_strs)}]")

            # 🆕 2026-11-01 (Phase 7.6.1): 告诉 Agent"这个人是陌生人,
            # **可以**问要不要录声纹"。
            #
            # ⚠️ 这里只是"建议",不代表已采集、也不代表用户已同意。
            # Agent 可以选择忽略(用户只是随口一句,不值得打断)。
            if self.suggest_enrollment:
                parts.append(
                    "[可询问登记声纹:当前说话人是陌生人且音频质量可用,"
                    "你可以自然地询问对方是否愿意让你记住他的声音,"
                    "并顺便问出对方的名字。必须得到明确同意后才能调用 enroll_speaker]"
                )
            return " ".join(parts)

        # 🆕 2026-11-01 (Phase 7.6.1): 声纹采集同意窗口的回答。
        #
        # 这是一个**是/否**判断,不是普通对话。单独走这个分支,
        # 避免 LLM 把"要"/"不用"当成正常指令去理解。
        if self.action == VoiceAction.CONFIRM_ANSWER:
            parts = [
                "[声纹采集确认] 刚才询问用户是否愿意登记声纹,用户的回答是:",
                f'"{self.asr_text}"',
                "[请判断这是同意还是拒绝:同意(如 要/好/可以/行/yes)→ "
                "调用 enroll_speaker(display_name=..., confirm=true);"
                "拒绝(如 不用/不要/算了/no)或无法判断 → 不要调用该工具,继续正常对话]",
            ]
            return " ".join(parts)

        # 其他情况(唤醒/打断/静默)走标准事件格式
        parts: list[str] = [
            f"[系统事件] 语音事件: {self.action.value}",
            f"device_id={self.device_id}",
        ]
        if self.wake_word:
            parts.append(f"wake_word={self.wake_word}")
        if self.asr_text:
            parts.append(f"asr_text={self.asr_text}")
        if self.speakers:
            speaker_strs = [
                f"{sp.user_id}({sp.confidence:.0%})" for sp in self.speakers
            ]
            parts.append(f"speakers=[{','.join(speaker_strs)}]")
        return " ".join(parts)


__all__ = ["VoiceEvent", "VoiceAction", "SpeakerInfo"]
