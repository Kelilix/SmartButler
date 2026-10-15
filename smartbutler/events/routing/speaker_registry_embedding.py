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

import math
from typing import Any

import numpy as np

from smartbutler.events.core.event import SPEAKER_MATCH_THRESHOLD
from smartbutler.events.routing.speaker_registry import (
    SpeakerMatch,
    SpeakerProfile,
    SpeakerRegistry,
)


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
    ) -> None:
        """
        Args:
            embedder: 声纹 embedding 生成器(ResemblyzerEmbedder / EcapaTdnnEmbedder)。
                用于 enroll 时验证 embedding 维度。
            threshold: 余弦相似度阈值,低于此值视为陌生人。
        """
        self._embedder = embedder
        self._threshold = threshold
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
