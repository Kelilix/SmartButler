"""SpeakerRegistry 抽象接口(Phase 7.6 声纹识别 — 2026-10-15 决策)。

## 职责

回答"当前说话的人是谁"——把声纹 embedding 匹配到已注册的家庭成员 user_id。

## 为什么单独做一个抽象(不归 PresenceService 管)

| 维度 | PresenceService | SpeakerRegistry |
|------|----------------|-----------------|
| 回答的问题 | "用户在哪儿,推哪台设备" | "说话的人是谁 user_id" |
| 数据源 | 门锁 / 手环 / GPS | 声纹 embedding |
| 调用时机 | TTS 推送前(选设备) | ASR 后立刻(决定归属) |
| 7.5 状态 | ✅ SimplePresenceService | ⏳ 本轮加占位(7.6 真正声纹模型) |

## Phase 7 启动时的简化实现

| 阶段 | 实现 | 数据源 |
|------|------|--------|
| 7.5a(本文件) | 内存 dict,声纹 embedding = user_id 直接映射 | dev_chat 手动配置 |
| 7.6(7.6 真正接入) | resemblyzer 256 维 / ecapa-tdnn 192 维 + 余弦相似度阈值匹配 | 麦克风音频 → 声纹模型 |
| 7.6+ | 长期语音样本采集 + 聚类自动归并(陌生人 → 用户确认 → 注册) | 真实家庭音频 |

## 关键字段

| 字段 | 说明 |
|------|------|
| ``user_id`` | 家庭成员 user_id(gaotianyu / wife / child) |
| ``display_name`` | 人类可读名字(高天禹 / 太太 / 孩子) |
| ``embedding`` | 声纹向量(7.5 占位 = None;7.6 真声纹模型算) |
| ``enrolled_at`` | 注册时间(7.6 起才用) |
| ``sample_count`` | 采集样本数(7.6 模型质量监控) |

## 决策记录

- 2026-10-15 创建(用户决策:为每个会话增加 user_id 拆分 task + 情绪)
- 与 TOTHINK 1.1 场景绑定:多说话人 → 多 user 参与同一会话,task 维度优先
  但 user 维度用于情绪归属(Phase 8)
- 7.5 阶段:本抽象存在,SimpleSpeakerRegistry 跑内存 dict,VoiceAdapter 不接,
  VoiceEvent.speakers 永远空 → 老调用零修改
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from smartbutler.events.core.event import (
    SPEAKER_EMBEDDING_DIM,
    SPEAKER_MATCH_THRESHOLD,
    SpeakerInfo,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SpeakerProfile:
    """已注册家庭成员的声纹档案。"""

    user_id: str
    display_name: str
    embedding: list[float] | None = None  # 7.5 占位 None;7.6 真声纹
    enrolled_at: datetime | None = None
    sample_count: int = 0
    metadata: dict[str, Any] | None = None  # 关系(男主人/女主人/孩子)/ 年龄 / 角色等

    def to_dict(self) -> dict[str, Any]:
        """可序列化 dict(供 persistence 层 / 测试 fixture 用)。"""
        return {
            "user_id": self.user_id,
            "display_name": self.display_name,
            "embedding": self.embedding,
            "enrolled_at": self.enrolled_at.isoformat() if self.enrolled_at else None,
            "sample_count": self.sample_count,
            "metadata": self.metadata or {},
        }


@dataclass(frozen=True)
class SpeakerMatch:
    """声纹匹配结果(由 identify() 返回)。"""

    matched_user_id: str | None
    """匹配到的 user_id;None = 未匹配(陌生人 / 阈值不够)。"""

    confidence: float
    """余弦相似度 [0, 1];失败时 = 最高候选的相似度(便于诊断)。"""

    is_known: bool
    """是否匹配到已注册家庭成员。"""

    is_above_threshold: bool
    """是否超过 ``SPEAKER_MATCH_THRESHOLD``(= 0.70)。"""

    candidates: tuple[tuple[str, float], ...] = ()
    """所有候选 (user_id, similarity) 排序(降序),调试 / 测试用。"""


class SpeakerRegistry(ABC):
    """声纹注册表抽象接口。

    ## 不变量

    - user_id 全局唯一(register 重复 → 抛 ValueError)
    - identify() 永远不抛异常(陌生人 / 错误 embedding → 返回 SpeakerMatch with is_known=False)
    - 7.5 阶段(本轮):SimpleSpeakerRegistry 用内存 dict;embedding 可为 None,
      7.5 阶段 identify() 只按 user_id 精确匹配(不做余弦相似度)
    - 7.6 阶段:替换为 EmbeddingSpeakerRegistry,embedding 必填,按余弦相似度匹配
    """

    @abstractmethod
    async def register(self, profile: SpeakerProfile) -> None:
        """注册一个家庭成员的声纹档案。

        Args:
            profile: 声纹档案(7.5 阶段 embedding 可为 None,7.6 必填)。

        Raises:
            ValueError: user_id 已注册。
        """
        ...

    @abstractmethod
    async def get(self, user_id: str) -> SpeakerProfile | None:
        """获取已注册档案。"""
        ...

    @abstractmethod
    async def update(self, profile: SpeakerProfile) -> None:
        """更新已注册档案的 embedding(**保留原 user_id**)。

        🆕 2026-11-01:重新采集同一人的声纹时必须保留 user_id ——
        人物表存的就是这个 id,换了就断链。

        Args:
            profile: 新档案(user_id 必须已存在)。

        Raises:
            ValueError: user_id 未注册。
        """
        ...

    @abstractmethod
    async def get_by_display_name(self, display_name: str) -> SpeakerProfile | None:
        """按姓名查找已注册档案(用于重新采集时定位原档案)。"""
        ...

    @abstractmethod
    async def find_best_match(self, embedding: list[float] | None) -> SpeakerMatch:
        """找出最相似的已注册成员(**不套阈值**判定 is_known)。

        用于"采集完成后判断是更新还是新建"。
        """
        ...

    @abstractmethod
    async def list_members(self) -> list[SpeakerProfile]:
        """列出所有已注册家庭成员(dev_chat / 调试用)。"""
        ...

    @abstractmethod
    async def unregister(self, user_id: str) -> bool:
        """注销一个家庭成员。返回是否真的删除了(存在 = True)。"""
        ...

    @abstractmethod
    async def identify(self, embedding: list[float] | None) -> SpeakerMatch:
        """声纹识别:把 embedding 匹配到已注册 user_id。

        Args:
            embedding: 声纹向量(7.5 占位 = None,只按 user_id 匹配;
                7.6 必填,按余弦相似度匹配)。

        Returns:
            SpeakerMatch: 匹配结果。陌生人 → matched_user_id=None,is_known=False。
        """
        ...

    # ---- 便利方法(由基类提供,子类不需重写) ----

    def should_trigger_enrollment(
        self,
        match: SpeakerMatch,
        *,
        audio_rms: float | None = None,
    ) -> tuple[bool, str]:
        """判断是否应该触发声纹采集(双阈值判定)。

        🆕 2026-11-01(用户决策:"按A,加双阈值")。

        ## 双阈值的真实含义

        实测(Resemblyzer,合成类人声):

            同人相似度: min=0.39  p5=0.41  p25=0.54  均值=0.76
            异人相似度: min=0.34              max=0.95  均值=0.58

        **低相似度 ≠ 音频质量差** —— 真正嗓音不同的人跟库里任何人都很低,
        必须采集,否则"陌生人永远不会被记住"。

        所以两个阈值的落点是:

        - ``threshold`` (识别门槛,默认 0.70):达到 = 认出来了,不采集。
        - ``enrollment_threshold`` (保护线,默认 0.55):相似度在这条线之上
          但没到识别线 → **熟人但这次说得不清楚** → 不采集,
          避免用噪声覆盖掉他正确的声纹。

        低于保护线 → 陌生人 → 采集。

        Args:
            match: :meth:`identify` 的结果。
            audio_rms: 本段音频 RMS(可选)。太低 = 录音质量差,不采集。

        Returns:
            (should_enroll, reason) 元组。reason 用于日志与调试。
        """
        if match.is_known and match.matched_user_id:
            return False, "already_identified"
        if not match.candidates:
            return True, "empty_registry"
        # 基类没有阈值概念,保守起见一律采集(子类会给出更精确的判定)。
        return True, "stranger"

    async def identify_speaker_info(
        self, embedding: list[float] | None
    ) -> SpeakerInfo:
        """把 SpeakerMatch 转成 SpeakerInfo(供 VoiceEvent.speakers 直接填)。

        Args:
            embedding: 声纹向量。

        Returns:
            SpeakerInfo:user_id 字段 = match.matched_user_id 或 "unknown:<hash>",
                confidence = match.confidence, is_known = match.is_known。
        """
        match = await self.identify(embedding)
        if match.is_known and match.matched_user_id:
            # 🐛 2026-11-01 加日志:识别成功的最终结果。
            # 这一层是 VoiceEvent.speakers 的直接来源,之前完全没有日志 ——
            # 用户反馈"识别不出身份"时,连识别成谁都看不到。
            logger.info(
                "👤 声纹身份确认: user_id=%s confidence=%.4f is_known=True",
                match.matched_user_id,
                match.confidence,
            )
            # 🆕 2026-11-01: 带上 display_name —— LLM 需要知道"这是谁在说话",
            # 光有 speaker_xxx 它读不出来("爸爸说" vs "妈妈说")。
            display_name = ""
            profile = await self.get(match.matched_user_id)
            if profile is not None:
                display_name = profile.display_name or ""
            return SpeakerInfo(
                user_id=match.matched_user_id,
                confidence=match.confidence,
                is_known=True,
                embedding=embedding,
                display_name=display_name or None,
            )
        # 陌生人:user_id 用 "unknown:<short-hash>" 占位(7.6+ 用户可确认后 register)
        import hashlib

        if embedding is not None:
            embed_hash = hashlib.sha256(
                bytes(str(embedding[:8]).encode())
            ).hexdigest()[:8]
        else:
            embed_hash = "noembed"
        # 🐛 2026-11-01 加日志:识别失败(陌生人)的最终结果。
        logger.info(
            "👤 声纹身份未确认: user_id=unknown:%s confidence=%.4f is_known=False",
            embed_hash,
            match.confidence,
        )
        return SpeakerInfo(
            user_id=f"unknown:{embed_hash}",
            confidence=match.confidence,
            is_known=False,
            embedding=embedding,
        )


__all__ = [
    "SpeakerRegistry",
    "SpeakerProfile",
    "SpeakerMatch",
    "SPEAKER_EMBEDDING_DIM",
    "SPEAKER_MATCH_THRESHOLD",
]
