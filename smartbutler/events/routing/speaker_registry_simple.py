"""SimpleSpeakerRegistry — SpeakerRegistry 内存实现(Phase 7.5 占位)。

## 7.5 阶段简化

| 维度 | 实现 |
|------|------|
| 数据存储 | 内存 dict[user_id, SpeakerProfile] |
| 声纹 embedding | 7.5 阶段 = None(7.6 真正声纹模型) |
| identify() 行为 | 7.5 不做余弦相似度,直接按 user_id 精确匹配 |
|                  | 7.5 identify(embedding=None) 永远返 SpeakerMatch(is_known=False) |
| 触发 register | dev_chat 命令 / 测试 fixture / 未来 7.6 启动后自动注册 |

## 7.6 真正声纹模型接入(后续阶段)

| 候选 | 维度 | 特点 |
|------|------|------|
| resemblyzer | 256 | 英文好,中文一般,轻量 |
| ecapa-tdnn | 192 | 端到端,跨语种,中等 |
| wespeaker | 512 | 中文友好,工业级 |

7.5 阶段本文件只负责接口,7.6 启动时换 EmbeddingSpeakerRegistry(余弦相似度)。

## 决策记录

- 2026-10-15 创建,与 SimplePresenceService 同模式(in-memory dict)
- 不持久化(7.5 阶段):重启丢 = OK,因为 7.5 没真声纹,7.6 启动时
  会做一次"全员重新注册"流程(用户依次说 3 句注册语)
"""
from __future__ import annotations

from typing import Any

from smartbutler.events.core.event import SPEAKER_MATCH_THRESHOLD
from smartbutler.events.routing.speaker_registry import (
    SpeakerMatch,
    SpeakerProfile,
    SpeakerRegistry,
)


class SimpleSpeakerRegistry(SpeakerRegistry):
    """Phase 7.5:in-memory dict 实现的声纹注册表。

    用法::

        registry = SimpleSpeakerRegistry()
        await registry.register(
            SpeakerProfile(user_id="gaotianyu", display_name="高天禹")
        )
        await registry.register(
            SpeakerProfile(user_id="wife", display_name="太太")
        )
        match = await registry.identify(None)  # 7.5 阶段永远 is_known=False
        # 7.6 真正声纹:
        # match = await registry.identify(embedding=[0.1, 0.2, ...])
    """

    def __init__(self) -> None:
        self._store: dict[str, SpeakerProfile] = {}

    async def register(self, profile: SpeakerProfile) -> None:
        """注册家庭成员。

        Raises:
            ValueError: user_id 已存在(避免误覆盖)。
        """
        if profile.user_id in self._store:
            raise ValueError(
                f"user_id={profile.user_id!r} 已注册,"
                f"请用 unregister() 先注销再 register()"
            )
        self._store[profile.user_id] = profile

    async def get(self, user_id: str) -> SpeakerProfile | None:
        return self._store.get(user_id)

    async def list_members(self) -> list[SpeakerProfile]:
        return list(self._store.values())

    async def unregister(self, user_id: str) -> bool:
        if user_id in self._store:
            del self._store[user_id]
            return True
        return False

    async def identify(self, embedding: list[float] | None) -> SpeakerMatch:
        """7.5 阶段简化版:不做余弦相似度(embedding=None 时永远 is_known=False)。

        7.6 真正声纹实现(EmbeddingSpeakerRegistry)按余弦相似度匹配,
        本类 7.6 阶段废弃(或改为只做"精确 embedding 比对",用于测试)。
        """
        if embedding is None:
            # 7.5 阶段:无 embedding = 视为陌生人
            return SpeakerMatch(
                matched_user_id=None,
                confidence=0.0,
                is_known=False,
                is_above_threshold=False,
                candidates=(),
            )
        # 7.5 占位:embedding 非 None 但 registry 都是 None embedding,
        # 永远无法相似度匹配,视为陌生人。
        # (7.6 真正实现:用 numpy.cos 求每个候选的相似度,排序)
        return SpeakerMatch(
            matched_user_id=None,
            confidence=0.0,
            is_known=False,
            is_above_threshold=False,
            candidates=(),
        )

    # ---- 便利方法(单测 / dev_chat 用) ----

    def register_sync(self, profile: SpeakerProfile) -> None:
        """同步版 register(单测不跑 event loop 时用)。"""
        if profile.user_id in self._store:
            raise ValueError(f"user_id={profile.user_id!r} 已注册")
        self._store[profile.user_id] = profile

    def clear_all(self) -> None:
        """清空所有成员(测试隔离用)。"""
        self._store.clear()

    def count(self) -> int:
        """已注册成员数(测试用)。"""
        return len(self._store)


__all__ = ["SimpleSpeakerRegistry"]
