"""声纹自动注册服务(2026-11-01)。

## 职责

把「采集到的音频」变成「声纹库里的一个档案」,并决定是**更新**还是**新建**。

```
采集完成(segments)
   → 提取每段 embedding
   → 多段取平均
   → find_best_match(avg) 找最相似的已注册成员
      ├─ 相似度 >= threshold  → update(覆盖 embedding,**保留原 user_id**)
      └─ 否则                  → register(新 user_id = 声纹哈希)
```

## 为什么必须有"更新 vs 新建"的分支(用户决策)

用户拍板:**"按A,更新embedding,保留原user_id"**。

原因:人物表里存的就是声纹库生成的 user_id。如果重新采集时换 id,
人物表的绑定就断了 —— 这正是"听声识人"要避免的。

所以重新采集同一个人(比如换了麦、环境变了、模型升级了)时,
必须走 update 而不是 register。

## user_id 的生成

`generate_user_id(embedding)`:取 embedding 前 8 维做 sha256,取 7 位 hex。

⚠️ **实测说明**:声纹 embedding 的前 8 维**随说话内容变化** —— 同一说话人
说不同的话会得到不同的哈希(实测 4 句话 → 4 个不同 id)。

所以这个 user_id 只在"**注册那一刻**"有意义,之后必须**固定在 profile 上复用**,
绝不能每次识别都重新算。识别路径走的是 `find_best_match` → 拿 profile 上
已存的 user_id,不会重新哈希。

Author: SmartButler Team
Date: 2026-11-01
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import numpy as np

from smartbutler.events.routing.speaker_registry import SpeakerProfile

if TYPE_CHECKING:
    from smartbutler.events.routing.speaker_registry import SpeakerRegistry

logger = logging.getLogger(__name__)

# 采集样本数下限:少于这个数认为音频质量不足以建档。
#
# 🆕 2026-11-01:实测 VoiceAdapter 的采集逻辑是"攒够 3 句才退出",
# 但**用户可能只说了一句就停** —— 采集循环退出时会把剩余 buffer 也存下来,
# 所以实际最少能拿到 1 段。这里设成 1,别把用户的有效样本扔掉。
MIN_SAMPLES_FOR_ENROLLMENT = 1
# 单段最短时长(秒):太短的片段(如"嗯")不带声纹信息
MIN_SEGMENT_SECONDS = 0.8
# 🆕 2026-11-01: 采集时的最低 RMS。低于此值 = 静音/底噪,不建档。
MIN_AUDIO_RMS_FOR_ENROLLMENT = 0.005

# 🆕 2026-11-01 (Phase 7.6.1): 低置信样本阈值。
#
# 采集目标是 3 句,但注册门槛是 1 段(见上)。只采到 1-2 段时,
# 声纹噪声偏大 → 标记 low_confidence=True,后续匹配时降权。
LOW_CONFIDENCE_SAMPLE_THRESHOLD = 3


@dataclass(frozen=True)
class EnrollmentResult:
    """注册/更新结果。"""

    status: str
    """``registered`` | ``updated`` | ``skipped`` | ``error``。"""

    user_id: str | None = None
    """最终生效的 user_id(更新时是**原有**的 id)。"""

    display_name: str = ""
    """人名(新注册时可能是"陌生人")。"""

    similarity: float = 0.0
    """与最相似已注册成员的相似度(更新时 >= 阈值)。"""

    sample_count: int = 0
    """参与建档的音频片段数。"""

    message: str = ""
    """给 LLM / 日志看的结果描述。"""


def generate_user_id(embedding: list[float]) -> str:
    """从 embedding 生成 user_id(声纹哈希)。

    取前 8 维做 sha256,取 7 位 hex,前缀 ``speaker_``。

    ⚠️ 见模块 docstring:这个值在"注册那一刻"生成后**必须固定复用**,
    绝不能每轮重新计算(同一说话人不同句子会算出不同值)。
    """
    embed_key = bytes(str(list(embedding[:8])).encode())
    short_hash = hashlib.sha256(embed_key).hexdigest()[:7]
    return f"speaker_{short_hash}"


def _norm(embedding: list[float]) -> float:
    """embedding 的 L2 范数(诊断用)。全 0 说明音频没可用信号。"""
    try:
        return float(sum(v * v for v in embedding) ** 0.5)
    except Exception:  # noqa: BLE001
        return float("nan")


class VoiceprintEnrollmentService:
    """声纹自动注册服务。

    Usage:
        service = VoiceprintEnrollmentService(registry, embedder)
        # 注入到 VoiceAdapter
        adapter.set_enrollment_callback(service.enroll_from_segments)
    """

    def __init__(
        self,
        registry: "SpeakerRegistry",
        embedder: Any,
        *,
        display_name: str = "陌生人",
    ) -> None:
        """
        Args:
            registry: 声纹注册表。
            embedder: 声纹 embedding 生成器。
            display_name: 自动注册时使用的默认名字(用户尚未自报姓名时)。
        """
        self._registry = registry
        self._embedder = embedder
        self._default_display_name = display_name

    async def enroll_from_segments(
        self,
        segments: list[tuple[np.ndarray, int]],
        *,
        display_name: str | None = None,
        known_user_id: str | None = None,
    ) -> EnrollmentResult:
        """把采集到的音频注册/更新到声纹库。

        Args:
            segments: ``[(audio, sample_rate), ...]``,每项一句话。
            display_name: 人名(可选)。None = 用默认名"陌生人"。
            known_user_id: 🆕 已知 user_id(可选)。

                **这是关键参数**:当触发采集时我们**已经知道**这个人的
                user_id(比如 LLM 之前调用过 enroll_speaker(display_name="妈妈"),
                或者这是重新采集已注册的人),传进来就能直接 update,
                彻底避免"重新采集 → 新建 → 人物表断链"。

        Returns:
            EnrollmentResult。
        """
        if not segments:
            return EnrollmentResult(
                status="skipped",
                message="没有采集到音频,跳过注册",
            )

        # ---------- 1. 过滤无效片段 ----------
        valid: list[tuple[np.ndarray, int]] = []
        for audio, sr in segments:
            dur = len(audio) / sr if sr else 0.0
            if dur < MIN_SEGMENT_SECONDS:
                logger.info(
                    "⏭️ 跳过过短片段: 时长=%.2fs < 最短 %.2fs",
                    dur,
                    MIN_SEGMENT_SECONDS,
                )
                continue
            # 🆕 2026-11-01: RMS 太低 = 静音/底噪,建档只会污染声纹
            rms = float(np.sqrt(np.mean(np.asarray(audio, dtype=np.float32) ** 2)))
            if rms < MIN_AUDIO_RMS_FOR_ENROLLMENT:
                logger.info(
                    "⏭️ 跳过静音片段: RMS=%.5f < 最低 %.5f (时长=%.2fs)",
                    rms,
                    MIN_AUDIO_RMS_FOR_ENROLLMENT,
                    dur,
                )
                continue
            valid.append((audio, sr))

        if len(valid) < MIN_SAMPLES_FOR_ENROLLMENT:
            return EnrollmentResult(
                status="skipped",
                message=(
                    f"有效音频只有 {len(valid)} 段(至少需要 "
                    f"{MIN_SAMPLES_FOR_ENROLLMENT} 段),跳过注册"
                ),
            )

        # ---------- 2. 提取 embedding ----------
        embeddings: list[list[float]] = []
        for idx, (audio, sr) in enumerate(valid):
            dur = len(audio) / sr
            try:
                emb = await self._embedder.embed(audio, sample_rate=sr)
                embeddings.append(emb)
                logger.info(
                    "🔢 声纹提取 #%d: 时长=%.2fs 维度=%d 范数=%.4f",
                    idx + 1,
                    dur,
                    len(emb),
                    _norm(emb),
                )
            except Exception as e:  # noqa: BLE001
                logger.warning("⚠️ 声纹提取失败 #%d: %s", idx + 1, e)

        if not embeddings:
            return EnrollmentResult(
                status="error",
                message="所有片段的声纹提取均失败",
            )

        # 多段取平均
        dim = len(embeddings[0])
        avg_embedding = [
            sum(emb[i] for emb in embeddings) / len(embeddings) for i in range(dim)
        ]
        logger.info(
            "📊 平均声纹: %d 段, 维度=%d, 范数=%.4f",
            len(embeddings),
            dim,
            _norm(avg_embedding),
        )

        name = (display_name or self._default_display_name).strip()

        # ---------- 3. 已知 user_id → 直接 update ----------
        if known_user_id:
            existing = await self._registry.get(known_user_id)
            if existing is not None:
                profile = SpeakerProfile(
                    user_id=existing.user_id,  # ⭐ 保留原 user_id
                    display_name=existing.display_name or name,
                    embedding=avg_embedding,
                    enrolled_at=existing.enrolled_at or datetime.now(UTC),
                    sample_count=len(valid),
                    metadata={
                        **(existing.metadata or {}),
                        "enrolled_via": "auto_enrollment_update",
                        "updated_at": datetime.now(UTC).isoformat(),
                        # 🆕 2026-11-01 (Phase 7.6.1): 低置信标记(见 §5.3)
                        "low_confidence": len(valid) < LOW_CONFIDENCE_SAMPLE_THRESHOLD,
                    },
                )
                try:
                    await self._registry.update(profile)
                except Exception as e:  # noqa: BLE001
                    logger.warning("⚠️ 声纹更新失败: %s", e)
                    return EnrollmentResult(
                        status="error",
                        message=f"声纹更新失败:{e}",
                        user_id=known_user_id,
                    )
                return EnrollmentResult(
                    status="updated",
                    user_id=existing.user_id,
                    display_name=existing.display_name or name,
                    sample_count=len(valid),
                    message=(
                        f"已更新 {existing.display_name or name} 的声纹"
                        f"(user_id 保持 {existing.user_id})"
                    ),
                )

        # ---------- 4. 未知 → 找最相似的已注册成员 ----------
        best = await self._registry.find_best_match(avg_embedding)
        similarity = best.confidence if best.matched_user_id else 0.0

        # 用户决策:"更新embedding,保留原user_id"
        # → 相似度达标就认为是同一个人,走 update(不换 id)
        threshold = getattr(self._registry, "_threshold", 0.70)
        if best.matched_user_id and similarity >= threshold:
            existing = await self._registry.get(best.matched_user_id)
            if existing is not None:
                profile = SpeakerProfile(
                    user_id=existing.user_id,  # ⭐ 保留原 user_id
                    display_name=existing.display_name or name,
                    embedding=avg_embedding,
                    enrolled_at=existing.enrolled_at or datetime.now(UTC),
                    sample_count=len(valid),
                    metadata={
                        **(existing.metadata or {}),
                        "enrolled_via": "auto_enrollment_update",
                        "updated_at": datetime.now(UTC).isoformat(),
                        "similarity": round(similarity, 4),
                        # 🆕 2026-11-01 (Phase 7.6.1): 低置信标记(见 §5.3)
                        "low_confidence": len(valid) < LOW_CONFIDENCE_SAMPLE_THRESHOLD,
                    },
                )
                try:
                    await self._registry.update(profile)
                except Exception as e:  # noqa: BLE001
                    logger.warning("⚠️ 声纹更新失败: %s", e)
                    return EnrollmentResult(
                        status="error",
                        message=f"声纹更新失败:{e}",
                        user_id=best.matched_user_id,
                    )
                logger.info(
                    "🔄 识别为已注册成员,更新声纹: %s (相似度=%.4f ≥ %.2f)",
                    best.matched_user_id,
                    similarity,
                    threshold,
                )
                return EnrollmentResult(
                    status="updated",
                    user_id=existing.user_id,
                    display_name=existing.display_name or name,
                    similarity=similarity,
                    sample_count=len(valid),
                    message=(
                        f"已更新 {existing.display_name or name} 的声纹"
                        f"(user_id 保持 {existing.user_id})"
                    ),
                )

        # ---------- 5. 确认是新人 → register ----------
        user_id = generate_user_id(avg_embedding)
        # 🆕 2026-11-01 (Phase 7.6.1): 低置信标记。
        #
        # 现状 MIN_SAMPLES_FOR_ENROLLMENT = 1,意味着用户只说一句"嗯"
        # 也会建档 —— 单句样本噪声大,匹配时容易误认成别人。
        #
        # 不改阈值(改阈值会把用户的有效样本也扔掉),改成**打标记**:
        # profile.metadata["low_confidence"] = True,
        # 后续匹配时可以对低置信档案降权。见 §5.3 设计文档。
        low_confidence = len(valid) < LOW_CONFIDENCE_SAMPLE_THRESHOLD
        profile = SpeakerProfile(
            user_id=user_id,
            display_name=name,
            embedding=avg_embedding,
            enrolled_at=datetime.now(UTC),
            sample_count=len(valid),
            metadata={
                "enrolled_via": "auto_enrollment",
                "similarity": round(similarity, 4),
                "low_confidence": low_confidence,
            },
        )
        if low_confidence:
            logger.warning(
                "⚠️ 只采到 %d 段音频(建议 ≥ %d 段),标记 low_confidence=True",
                len(valid),
                LOW_CONFIDENCE_SAMPLE_THRESHOLD,
            )
        try:
            await self._registry.register(profile)
        except ValueError:
            # user_id 撞了(极小概率)→ 换个后缀重试一次
            logger.warning("⚠️ user_id 冲突(%s),加后缀重试", user_id)
            profile = SpeakerProfile(
                user_id=f"{user_id}_{len(valid)}",
                display_name=name,
                embedding=avg_embedding,
                enrolled_at=datetime.now(UTC),
                sample_count=len(valid),
                metadata=profile.metadata,
            )
            try:
                await self._registry.register(profile)
            except ValueError as e2:
                logger.error("❌ 声纹注册失败: %s", e2)
                return EnrollmentResult(
                    status="error",
                    message=f"声纹注册失败:{e2}",
                )

        logger.info(
            "✅ 声纹注册成功: user_id=%s display_name=%s samples=%d",
            profile.user_id,
            name,
            len(valid),
        )
        return EnrollmentResult(
            status="registered",
            user_id=profile.user_id,
            display_name=name,
            similarity=similarity,
            sample_count=len(valid),
            message=f"已记住 {name} 的声音(user_id={profile.user_id})",
        )


__all__ = [
    "VoiceprintEnrollmentService",
    "EnrollmentResult",
    "generate_user_id",
    "MIN_SAMPLES_FOR_ENROLLMENT",
    "MIN_SEGMENT_SECONDS",
    "LOW_CONFIDENCE_SAMPLE_THRESHOLD",
]
