"""声纹注册 Tool（Phase 7.6）。

触发方式：用户说"帮我建立人声档案"。

两阶段状态机：
- 第一阶段：启动声纹采集，返回采集说明。
- 第二阶段：确认采集完成，保存声纹到 SpeakerRegistry。

使用方式（由 LLM 驱动）::

    # 第一步：用户说"帮我建立人声档案"
    result = await enroll_speaker(display_name="妈妈")
    # 返回: "好的，请说几句话让我记住你的声音。采集到足够音频后我会自动处理。"

    # 第二步：用户说完话后（由 LLM 判断是否足够）
    result = await enroll_speaker(display_name="妈妈", confirm=True)
    # 返回: "人声档案建立成功！我已记住妈妈的声音，user_id=speaker_a3f2b1。"

Author: SmartButler Team
Date: 2026-10-16
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from typing import TYPE_CHECKING, Any

import numpy as np

from smartbutler.capabilities.tools.decorator import register_tool

if TYPE_CHECKING:
    from smartbutler.events.routing.speaker_registry import SpeakerRegistry
    from smartbutler.voice.adapter.voice_adapter import VoiceAdapter

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 全局依赖注入（由 VoiceService 启动时设置）
# ---------------------------------------------------------------------------

# 全局 VoiceAdapter 实例（由 voice_service.py 初始化时注入）
_voice_adapter: "VoiceAdapter | None" = None

# 全局 SpeakerRegistry 实例（由 voice_service.py 初始化时注入）
_speaker_registry: "SpeakerRegistry | None" = None

# 声纹采集的音频（start 时写入，confirm 时读出）
_enrollment_audio: list[tuple[np.ndarray, int]] = []


def set_voice_adapter(adapter: "VoiceAdapter") -> None:
    """由 VoiceService 初始化时调用，注入 VoiceAdapter 实例。"""
    global _voice_adapter
    _voice_adapter = adapter


def set_speaker_registry(registry: "SpeakerRegistry") -> None:
    """由 VoiceService 初始化时调用，注入 SpeakerRegistry 实例。"""
    global _speaker_registry
    _speaker_registry = registry


def _generate_user_id(embedding: list[float]) -> str:
    """从 embedding 生成唯一 user_id。

    策略：取前 8 维的 hash 前 7 位，前缀 speaker_。
    """
    embed_key = bytes(str(embedding[:8]).encode())
    short_hash = hashlib.sha256(embed_key).hexdigest()[:7]
    return f"speaker_{short_hash}"


# ---------------------------------------------------------------------------
# Tool 实现
# ---------------------------------------------------------------------------


@register_tool(
    name="enroll_speaker",
    description=(
        "建立声纹档案。当用户说'帮我建立人声档案'或类似意图时调用。\n"
        "第一步：调用 enroll_speaker(display_name='xxx')，启动声纹采集，"
        "LLM 告诉用户'请说几句话让我记住你的声音'。\n"
        "第二步：用户说完后，再次调用 enroll_speaker(display_name='xxx', confirm=True)，"
        "确认采集完成，保存声纹档案。\n"
        "后续：管家能通过声纹识别用户身份，无需重复注册。"
    ),
    scope=None,  # 走默认 GLOBAL
    timeout_seconds=120.0,
    idempotent=False,
)
async def enroll_speaker(
    display_name: str,
    user_id: str | None = None,
    confirm: bool = False,
) -> dict[str, Any]:
    """建立声纹档案。

    Args:
        display_name: 用户自称的名字，如"妈妈"、"爸爸"、"奶奶"。
        user_id: 可选，用户指定的 user_id；None=自动生成 speaker_xxx。
        confirm: 是否确认采集完成。False=启动采集，True=确认保存。

    Returns:
        dict: 含 status / message / user_id 等字段。
    """
    global _enrollment_audio

    if _speaker_registry is None:
        return {
            "status": "error",
            "message": "声纹注册功能未初始化，请联系管理员。",
        }

    # ---------- Phase 1: 启动声纹采集 ----------
    if not confirm:
        if not display_name or not display_name.strip():
            return {
                "status": "error",
                "message": "display_name 不能为空，请提供你的名字（如'妈妈'、'爸爸'）。",
            }

        if _voice_adapter is None:
            return {
                "status": "error",
                "message": "语音采集功能未初始化，无法采集声纹。",
            }

        # 启动 VoiceAdapter 采集模式
        await _voice_adapter.start_voice_enrollment()

        _enrollment_audio.clear()

        logger.info("声纹采集已启动，等待用户说话（display_name=%s）", display_name)

        return {
            "status": "collecting",
            "message": (
                f"好的 {display_name}，请说 3-5 句话让我记住你的声音。\n"
                "每句说 5-10 秒，句与句之间停顿一下。\n"
                "说完后我会自动处理。"
            ),
            "display_name": display_name,
            "instruction": "collecting",
        }

    # ---------- Phase 2: 确认采集完成 ----------
    # 从 VoiceAdapter 取采集到的音频
    if _voice_adapter is None:
        return {
            "status": "error",
            "message": "语音采集功能未初始化。",
        }

    audio_segments = await _voice_adapter.stop_voice_enrollment()
    _enrollment_audio = audio_segments

    if not audio_segments:
        return {
            "status": "error",
            "message": "没有采集到有效音频，请重新说几句话。",
            "display_name": display_name,
            "instruction": "retry",
        }

    # 检查 embedder
    from smartbutler.voice.speaker.base import BaseSpeakerEmbedder

    # 从 speaker_registry 获取 embedder（EmbeddingSpeakerRegistry 有 _embedder 属性）
    embedder: BaseSpeakerEmbedder | None = getattr(_speaker_registry, "_embedder", None)
    if embedder is None:
        # 尝试从 registry 内部获取
        embedder = getattr(_speaker_registry, "_embedder", None)

    if embedder is None:
        return {
            "status": "error",
            "message": "声纹模型未初始化，无法提取声纹特征。",
        }

    # 提取 embedding（多句取平均）
    embeddings: list[list[float]] = []
    for audio, sample_rate in audio_segments:
        try:
            emb = await embedder.embed(audio, sample_rate=sample_rate)
            embeddings.append(emb)
        except Exception as e:
            logger.warning("声纹提取失败: %s", e)
            continue

    if not embeddings:
        return {
            "status": "error",
            "message": "声纹提取失败，请重新尝试。",
            "display_name": display_name,
            "instruction": "retry",
        }

    # 多句 embedding 取平均
    avg_embedding = [
        sum(emb[i] for emb in embeddings) / len(embeddings)
        for i in range(len(embeddings[0]))
    ]

    # 生成或确认 user_id
    if user_id is None or not user_id.strip():
        user_id = _generate_user_id(avg_embedding)

    # 构建 SpeakerProfile
    from datetime import UTC, datetime
    from smartbutler.events.routing.speaker_registry import SpeakerProfile

    profile = SpeakerProfile(
        user_id=user_id,
        display_name=display_name.strip(),
        embedding=avg_embedding,
        enrolled_at=datetime.now(UTC),
        sample_count=len(audio_segments),
        metadata={"enrolled_via": "enroll_speaker_tool"},
    )

    # 注册（若已存在则报错）
    try:
        await _speaker_registry.register(profile)
    except ValueError as e:
        return {
            "status": "error",
            "message": f"注册失败：{e}。如需更新请先注销原有档案。",
        }

    logger.info(
        "声纹档案建立成功: user_id=%s, display_name=%s, samples=%d",
        user_id,
        display_name,
        len(audio_segments),
    )

    return {
        "status": "enrolled",
        "message": (
            f"人声档案建立成功！我已记住 {display_name} 的声音。\n"
            f"user_id: {user_id}\n"
            f"采集样本: {len(audio_segments)} 句\n"
            f"以后每次说话我都能识别你的身份了。"
        ),
        "user_id": user_id,
        "display_name": display_name,
        "sample_count": len(audio_segments),
    }


__all__ = [
    "enroll_speaker",
    "set_voice_adapter",
    "set_speaker_registry",
]
