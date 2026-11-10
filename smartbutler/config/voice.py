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
    #
    # 🐛 2026-10-26 实测标定(EdgeTTS 真实语音 vs 纯静音 vs 房间底噪):
    #   语音概率中位数 = 0.9999, 纯静音最大 0.0132, 底噪最大 0.0353
    #   阈值   语音命中   静音误判   底噪误判
    #   0.005   99.2%     38.7%      9.7%   <- 旧值:把 1/3 的静音当语音
    #   0.010   96.9%     17.2%      3.2%
    #   0.020   84.6%      0.0%      1.1%   <- 当前默认
    #   0.050   82.3%      0.0%      0.0%
    #
    # ⚠️ 为什么不能太低:VAD 误判为语音会**不断重置静默计时**,
    # 导致 silence_timeout 永远等不到 → 只能靠 max_segment 硬切,
    # 切出一堆碎片事件 → LLM 对每片都回复一次 → 用户听到"旧话陆续重播"。
    # 语音本身概率极高(中位 0.9999),把阈值从 0.005 提到 0.02 几乎不损失检出。
    vad_threshold: float = Field(
        default=0.02,
        ge=0.0,
        le=1.0,
        description=(
            "VAD 语音概率阈值(0-1)。越低越敏感。"
            "默认 0.02:实测语音命中 84.6% 且纯静音 0% 误判,"
            "兼顾检出率与'能等到静默'。"
            "环境极安静可降到 0.01;环境嘈杂可升到 0.05。"
        ),
    )

    # ===== 时序参数 =====
    # 一段话结束的判定: 连续 N ms RMS < vad_threshold → 切下一段
    # 3000ms (3s) 是业内常见默认值, mic_test.wav 语音段 1.5s 用 3s 缓冲足够。
    #
    # 🐛 2026-10-26 下调到 1500ms:
    #   首字延迟实测 = 静默等待 + ASR + LLM ≈ 1.5 + 2.2 + 2.0 = 5.7s
    #   静默等待占了其中 ~1/3。降到 1500ms 后首字延迟降到 ~4.2s,
    #   体感从"等半天"变成"稍等"。
    #   ⚠️ 不要再往下降: <800ms 会在正常说话停顿(换气/思考)时误切,
    #   产生碎片 ASR 请求,反而更慢也更吵。
    silence_timeout_ms: int = Field(
        default=1500,
        ge=100,
        description=(
            "静音超时(毫秒)。连续静音超过此时长 → flush buffer 送 ASR。"
            "默认 1500ms:兼顾首字延迟与'不把换气当停顿'。"
        ),
    )

    # 极端保护: 用户连说不停(超过 30s)→ 强制截断送 ASR, 避免 buffer 无限增长。
    # 15s 比 30s 更友好: 一句话不超过 15s 是更常见的语音助手假设。
    #
    # 🐛 2026-10-26 从 15000 提到 30000:
    #   max_segment 是**无条件硬切**(不看用户是否还在说话),
    #   一句长话会在阈值处被切成两半 → 两次 ASR + 两次 LLM 回复,
    #   用户听到"同一句话被拆成两段分别回答"。
    #   提到 30s 后,正常长句不会被拆;仍保留极端保护。
    max_segment_duration_ms: int = Field(
        default=30000,
        ge=1000,
        description=(
            "单段最大时长(毫秒)。即使用户不停说话, 超过此时长也强制 flush。"
            "默认 30000ms(30s):正常长句不会被硬切成两半。"
            "⚠️ 这是硬切,不区分'用户还在说'与'用户停了',调小会产生碎片回复。"
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

    # ===== L1/L2 打断(代次过期策略) =====
    # 🆕 2026-11-10:代次追踪的宽松过期窗口(秒)。
    #
    # 语义:只有"**最近该时间窗内**出现的更新输入"才让旧回复作废。
    #   0   = 严格模式:只要用户说了新话(哪怕是 30 秒前),旧回复就不播
    #   >0  = 宽松模式:只有"刚刚(窗口内)说了新话"才打断旧回复
    #
    # 为什么需要宽松模式:
    #   用户:"帮我查下明天天气"        ← LLM 在算(10s)
    #   用户:"嗯"                       ← 随口应答, 不算换话题
    #   LLM 算完 → 严格模式下会丢弃这条回复,用户白等 10s 却啥也没听到
    #
    # 为什么默认值选 0(严格):
    #   严格模式行为可预测 —— "我说了新话,旧回复就不播",符合"打断"的直觉。
    #   宽松模式需要按实际使用场景调参(3s/5s/10s 各有取舍),
    #   等有了真实反馈再调默认值,避免拍脑袋定错。
    stale_window_seconds: float = Field(
        default=0.0,
        ge=0.0,
        description=(
            "代次过期宽松窗口(秒),0=严格模式(任何新输入都作废旧回复)。"
            ">0:只有最近该窗口内的新输入才作废旧回复。"
        ),
    )


def load_voice_settings() -> VoiceSettings:
    """加载 Voice 子模块配置。

    每次调用都重新读取环境变量, 便于在测试或运行时动态调参。
    """
    return VoiceSettings()


__all__ = ["VoiceSettings", "load_voice_settings"]
