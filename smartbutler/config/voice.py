"""Voice 子模块配置（Phase 7+）。

所有"听/说"相关的运行时调参都集中在这里，避免散落在脚本/dataclass 硬编码里。

优先级：
    命令行参数 > .env / 环境变量 > 字段默认值

环境变量前缀：``SMARTBUTLER_VOICE_``
示例：``SMARTBUTLER_VOICE_VAD_THRESHOLD=0.005``
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class VoiceSettings(BaseSettings):
    """Voice 子模块配置。

    Environment variable prefix: ``SMARTBUTLER_VOICE_``
    """

    model_config = SettingsConfigDict(
        env_prefix="SMARTBUTLER_VOICE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ===== 音频参数 =====
    sample_rate: int = Field(
        default=16000,
        gt=0,
        description="采样率(Hz)。16k 适合语音识别, 不要随便改。",
    )

    # ===== VAD 灵敏度 =====
    # 历史经验值:
    #   - 0.3: 脚本默认值, 对增益后的音频较友好
    #   - 0.005~0.01: 真实环境裸测, mic_test.wav 语音 RMS 0.0087~0.0275 时
    #   - 0.0001: 太敏感, 会把麦克风底噪也当成语音, 引发 "永远不停"
    vad_threshold: float = Field(
        default=0.3,
        ge=0.0,
        le=1.0,
        description=(
            "VAD 语音概率阈值(0-1)。越低越敏感。"
            "默认值 0.3 适用于已开启 auto_gain 的耳机麦克风。"
            "真实环境下从 0.005 起步, 调到不漏检也不误检。"
        ),
    )

    # ===== 时序参数 =====
    # 一段话结束的判定: 连续 N ms RMS < vad_threshold → 切下一段
    # 3000ms (3s) 是业内常见默认值, mic_test.wav 语音段 1.5s 用 3s 缓冲足够。
    silence_timeout_ms: int = Field(
        default=3000,
        ge=100,
        description=(
            "静音超时(毫秒)。连续静音超过此时长 → flush buffer 送 ASR。"
            "默认值 3000ms (3s) 适合正常说话节奏。"
        ),
    )

    # 极端保护: 用户连说不停(超过 30s)→ 强制截断送 ASR, 避免 buffer 无限增长。
    # 15s 比 30s 更友好: 一句话不超过 15s 是更常见的语音助手假设。
    max_segment_duration_ms: int = Field(
        default=30000,
        ge=1000,
        description=(
            "单段最大时长(毫秒)。即使用户不停说话, 超过此时长也强制 flush。"
            "生产建议 15000, 默认 30000 偏长。"
        ),
    )

    # ===== 唤醒词 =====
    # 当前 SimpleWakeWordDetector 只支持 "任何声音触发", 预留字段给未来 P0+ 唤醒词
    wake_energy_threshold: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="唤醒能量阈值(0-1, 0=任何声音都触发)",
    )


def load_voice_settings() -> VoiceSettings:
    """加载 Voice 子模块配置。

    每次调用都重新读取环境变量, 便于在测试或运行时动态调参。
    """
    return VoiceSettings()


__all__ = ["VoiceSettings", "load_voice_settings"]