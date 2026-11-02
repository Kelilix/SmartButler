"""EmbeddingSpeakerRegistry — 基于余弦相似度的声纹识别实现。

依赖 BaseSpeakerEmbedder 实现(resemblyzer / ecapa-tdnn)。

设计:
- 接收声纹 embedding,计算与所有已注册成员的余弦相似度
- 返回匹配结果(candidates 排序)
- 支持阈值过滤

Author: SmartButler Team
Date: 2026-10-15
"""

from __future__ import annotations

import logging
import math
from typing import Any

import numpy as np

from smartbutler.events.core.event import SPEAKER_MATCH_THRESHOLD
from smartbutler.events.routing.speaker_registry import (
    SpeakerMatch,
    SpeakerProfile,
    SpeakerRegistry,
)

logger = logging.getLogger(__name__)

# 🆕 2026-11-01: 采集时的最低音频 RMS。
#
# 低于这个值说明录到的是静音/底噪,拿去建档只会污染声纹。
# 正常说话(麦克风增益下)RMS 通常在 0.01 以上。
MIN_AUDIO_RMS_FOR_ENROLLMENT = 0.005


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """计算两个向量的余弦相似度。

    Args:
        a: 向量 A。
        b: 向量 B。

    Returns:
        float: 余弦相似度 [0.0, 1.0]。
    """
    if len(a) != len(b):
        raise ValueError(f"Vector dimension mismatch: {len(a)} vs {len(b)}")

    dot = sum(ai * bi for ai, bi in zip(a, b))
    norm_a = math.sqrt(sum(ai * ai for ai in a))
    norm_b = math.sqrt(sum(bi * bi for bi in b))

    if norm_a == 0 or norm_b == 0:
        return 0.0

    return dot / (norm_a * norm_b)


class EmbeddingSpeakerRegistry(SpeakerRegistry):
    """基于余弦相似度的声纹注册表。

    Usage:

        from smartbutler.voice.speaker.resemblyzer import ResemblyzerEmbedder
        from smartbutler.events.routing.speaker_registry_simple import EmbeddingSpeakerRegistry

        # 7.6 启动:Resemblyzer + EmbeddingSpeakerRegistry
        embedder = ResemblyzerEmbedder()
        registry = EmbeddingSpeakerRegistry(embedder=embedder)

        # 注册家庭成员
        from smartbutler.events.routing.speaker_registry import SpeakerProfile
        await registry.register(SpeakerProfile(
            user_id="dad",
            display_name="爸爸",
            embedding=[...],  # 从 3-5 句注册语音提取
        ))

        # 实时识别
        match = await registry.identify(embedding)  # embedding = 声纹向量
        print(f"说话人: {match.matched_user_id}, 置信度: {match.confidence:.0%}")
    """

    def __init__(
        self,
        embedder: Any,  # BaseSpeakerEmbedder,使用 Any 避免循环 import
        threshold: float = SPEAKER_MATCH_THRESHOLD,
        enrollment_threshold: float | None = None,
    ) -> None:
        """
        Args:
            embedder: 声纹 embedding 生成器(ResemblyzerEmbedder / EcapaTdnnEmbedder)。
                用于 enroll 时验证 embedding 维度。
            threshold: 余弦相似度阈值,低于此值视为陌生人。
            enrollment_threshold: 🆕 2026-11-01 采集触发阈值(双阈值)。

                **为什么需要双阈值**:低于 ``threshold`` 有两种完全不同的原因:
                  - 真的是陌生人 → 应该采集
                  - 是熟人但这次说得含糊/环境吵 → **不该采集**

                如果一律采集,后者会用噪声覆盖掉正确的声纹,越用越差。
                所以:
                  - 相似度 >= threshold                → 识别成功
                  - enrollment_threshold <= 相似度 < threshold → 陌生人,触发采集
                  - 相似度 < enrollment_threshold         → 疑似音频质量差,**不采集**

                默认取 ``threshold * 0.78``(0.70 → 0.546)。
        """
        self._embedder = embedder
        self._threshold = threshold
        self._enrollment_threshold = (
            enrollment_threshold
            if enrollment_threshold is not None
            else threshold * 0.78
        )
        self._store: dict[str, SpeakerProfile] = {}

    async def register(self, profile: SpeakerProfile) -> None:
        """注册家庭成员。

        Args:
            profile: 声纹档案。

        Raises:
            ValueError: user_id 已注册 或 embedding 为 None。
        """
        if profile.user_id in self._store:
            raise ValueError(
                f"user_id={profile.user_id!r} 已注册,"
                f"请用 unregister() 先注销再 register()"
            )
        if profile.embedding is None:
            raise ValueError("EmbeddingSpeakerRegistry 要求 embedding 必须非 None")

        # 验证 embedding 维度
        expected_len = len(profile.embedding)
        if expected_len == 0:
            raise ValueError("embedding 不能为空")

        self._store[profile.user_id] = profile

    async def get(self, user_id: str) -> SpeakerProfile | None:
        """获取已注册档案。"""
        return self._store.get(user_id)

    async def update(self, profile: SpeakerProfile) -> None:
        """更新已注册档案的 embedding(**保留原 user_id**)。

        🆕 2026-11-01(用户决策:"按A,更新embedding,保留原user_id")。

        为什么需要:重新采集声纹时,**必须保留 user_id** —— 人物表里存的就是
        这个 id,换了就断链。所以"更新声纹"和"新建档案"是两件不同的事:
          - 声纹相似度 >= 阈值 → update(覆盖 embedding,user_id 不变)
          - 声纹相似度 <  阈值 → register(全新的人,新 user_id)

        Args:
            profile: 新的档案(``user_id`` 必须与已存在的档案一致)。

        Raises:
            ValueError: user_id 未注册,或 embedding 为 None。
        """
        if profile.user_id not in self._store:
            msg = f"user_id={profile.user_id!r} 未注册，无法更新"
            raise ValueError(msg)
        if profile.embedding is None:
            msg = "EmbeddingSpeakerRegistry 要求 embedding 必须非 None"
            raise ValueError(msg)

        # ⚠️ 保留原 user_id(虽然 profile 里也是同一个,这里显式强调语义)
        self._store[profile.user_id] = profile
        logger.info(
            "🔄 声纹档案已更新: user_id=%s display_name=%s (user_id 保持不变)",
            profile.user_id,
            profile.display_name,
        )

    async def get_by_display_name(self, display_name: str) -> SpeakerProfile | None:
        """按姓名查找已注册档案。

        用于"重新采集同一人的声纹"时定位原档案。
        """
        target = display_name.strip()
        for profile in self._store.values():
            if profile.display_name.strip() == target:
                return profile
        return None

    async def find_best_match(
        self, embedding: list[float] | None
    ) -> SpeakerMatch:
        """找出最相似的已注册成员(不套用 is_known 判定)。

        用于"采集完成后判断是更新还是新建":拿新采集的声纹去比对,
        相似度够高 → 是已注册的人 → update;不够 → 新建。

        与 :meth:`identify` 的区别:``identify`` 会套 ``_threshold``
        判定 is_known;这里只返回最相似的那一个,由调用方自己决定阈值。

        Args:
            embedding: 声纹向量。

        Returns:
            SpeakerMatch: matched_user_id 为最相似的 user_id(即便低于阈值),
            confidence 为相似度,is_known 恒为 False(不套阈值)。
        """
        if embedding is None:
            return SpeakerMatch(
                matched_user_id=None,
                confidence=0.0,
                is_known=False,
                is_above_threshold=False,
                candidates=(),
            )

        candidates: list[tuple[str, float]] = []
        for user_id, profile in self._store.items():
            if profile.embedding is None:
                continue
            similarity = cosine_similarity(embedding, profile.embedding)
            candidates.append((user_id, similarity))

        candidates.sort(key=lambda x: x[1], reverse=True)
        if not candidates:
            return SpeakerMatch(
                matched_user_id=None,
                confidence=0.0,
                is_known=False,
                is_above_threshold=False,
                candidates=(),
            )

        best_user_id, best_similarity = candidates[0]
        return SpeakerMatch(
            matched_user_id=best_user_id,
            confidence=best_similarity,
            is_known=False,  # 不套阈值,由调用方判断
            is_above_threshold=best_similarity >= self._threshold,
            candidates=tuple(candidates),
        )

    def should_trigger_enrollment(
        self,
        match: SpeakerMatch,
        *,
        audio_rms: float | None = None,
    ) -> tuple[bool, str]:
        """判断是否应该触发声纹采集(双阈值判定)。

        🆕 2026-11-01(用户决策:"按A,加双阈值")。

        ## 双阈值的真实含义

        实测(Resemblyzer,4 个说话人 × 5 句,合成类人声):

            同人相似度: min=0.39  p5=0.41  p25=0.54  均值=0.76
            异人相似度: min=0.34              max=0.95  均值=0.58

        注意 **异人相似度可以低到 0.34** —— 真正嗓音完全不同的人,
        跟库里任何人的相似度都很低。所以:

        - **低相似度 ≠ 音频质量差**。第一次见面的人就是低相似度,
          必须采集!否则"陌生人永远不会被记住"。
        - **高相似度但未达识别阈值** → 这才是需要保护的场景:
          熟人这次说得含糊 / 环境噪音,不该用噪声覆盖他正确的声纹。

        因此两个阈值的落点是:

        - ``_threshold`` (match,默认 0.70):**识别**门槛。
          达到 = 认出来了,不采集。
        - ``_enrollment_threshold`` (默认 0.55):**保护线**。
          相似度落在这条线之上但没到识别线 → 是熟人但这次不清楚 →
          **不采集**(避免污染声纹),交由 LLM 用别的方式确认身份。

        低于保护线 → 就是陌生人 → **采集**。

        Args:
            match: :meth:`identify` 的结果。
            audio_rms: 本段音频的 RMS(可选)。太低说明录音质量差,
                此时不采集(避免拿噪声建档)。

        Returns:
            (should_enroll, reason) 元组。reason 用于日志与调试。
        """
        if match.is_known and match.matched_user_id:
            return False, "already_identified"

        # 音频质量保护:RMS 太低 → 录到的是静音/噪声,采了也没用
        if audio_rms is not None and audio_rms < MIN_AUDIO_RMS_FOR_ENROLLMENT:
            return False, "poor_audio_quality"

        if not match.candidates:
            # 声纹库为空 —— 没别的办法,只能采集
            return True, "empty_registry"

        best_similarity = match.candidates[0][1]
        if best_similarity >= self._enrollment_threshold:
            # 相似度够高但没到识别线 —— 是熟人但这次说得不清楚。
            # 不采集,避免用噪声覆盖掉他正确的声纹。
            return False, "ambiguous_similar_not_enough"

        # 低于保护线 → 判定为陌生人 → 采集
        return True, "stranger"

    @property
    def enrollment_threshold(self) -> float:
        """声纹保护线(双阈值的下界)。"""
        return self._enrollment_threshold

    async def list_members(self) -> list[SpeakerProfile]:
        """列出所有已注册家庭成员。"""
        return list(self._store.values())

    async def unregister(self, user_id: str) -> bool:
        """注销一个家庭成员。"""
        if user_id in self._store:
            del self._store[user_id]
            return True
        return False

    async def identify(self, embedding: list[float] | None) -> SpeakerMatch:
        """声纹识别:计算余弦相似度并匹配。

        Args:
            embedding: 声纹向量。

        Returns:
            SpeakerMatch: 匹配结果。
        """
        if embedding is None:
            logger.info("🔍 声纹识别: embedding 为空，无法匹配")
            return SpeakerMatch(
                matched_user_id=None,
                confidence=0.0,
                is_known=False,
                is_above_threshold=False,
                candidates=(),
            )

        # 计算与所有已注册成员的相似度
        candidates: list[tuple[str, float]] = []
        for user_id, profile in self._store.items():
            if profile.embedding is None:
                continue
            similarity = cosine_similarity(embedding, profile.embedding)
            candidates.append((user_id, similarity))

        # 按相似度降序排序
        candidates.sort(key=lambda x: x[1], reverse=True)

        # 🐛 2026-11-01 加日志:相似度明细。
        #
        # 之前 candidates 算出来了但没人看 → 识别失败时完全无从判断:
        # 是"没注册过"还是"注册了但相似度不够"?差多少?
        # 这条日志能直接区分这两种情况,并看出阈值是否需要调整。
        if candidates:
            detail = ", ".join(
                f"{uid}={sim:.4f}" for uid, sim in candidates
            )
            logger.info(
                "🔍 声纹识别: %d 个候选 [%s] 阈值=%.2f",
                len(candidates),
                detail,
                self._threshold,
            )
        else:
            # ⚠️ 最常见的失败原因:一个声纹都没注册过
            logger.warning(
                "🔍 声纹识别: 注册表为空（0 个已注册声纹），无法匹配任何用户"
            )

        if not candidates:
            return SpeakerMatch(
                matched_user_id=None,
                confidence=0.0,
                is_known=False,
                is_above_threshold=False,
                candidates=tuple(candidates),
            )

        best_user_id, best_similarity = candidates[0]
        is_above = best_similarity >= self._threshold

        if is_above:
            logger.info(
                "✅ 声纹识别成功: %s (相似度=%.4f ≥ 阈值=%.2f)",
                best_user_id,
                best_similarity,
                self._threshold,
            )
        else:
            # 低于阈值 → 判定为陌生人
            logger.info(
                "❌ 声纹识别未匹配: 最高 %s 相似度=%.4f < 阈值=%.2f（判为陌生人）",
                best_user_id,
                best_similarity,
                self._threshold,
            )

        return SpeakerMatch(
            matched_user_id=best_user_id if is_above else None,
            confidence=best_similarity,
            is_known=is_above,
            is_above_threshold=is_above,
            candidates=tuple(candidates),
        )

    # ---- 便利方法 ----

    def register_sync(self, profile: SpeakerProfile) -> None:
        """同步版 register(单测不跑 event loop 时用)。"""
        if profile.user_id in self._store:
            raise ValueError(f"user_id={profile.user_id!r} 已注册")
        if profile.embedding is None:
            raise ValueError("EmbeddingSpeakerRegistry 要求 embedding 必须非 None")
        self._store[profile.user_id] = profile

    def clear_all(self) -> None:
        """清空所有成员(测试隔离用)。"""
        self._store.clear()

    def count(self) -> int:
        """已注册成员数(测试用)。"""
        return len(self._store)


__all__ = ["EmbeddingSpeakerRegistry", "cosine_similarity"]
