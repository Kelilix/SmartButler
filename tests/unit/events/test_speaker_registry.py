"""声纹识别扩展位测试(Phase 7.6 占位 — 2026-10-15 决策)。

## 覆盖范围

1. SpeakerInfo 模型:frozen + 必填字段 + 防御性校验
2. VoiceEvent.speakers 字段:默认空 list,显式赋值,to_user_input 拼接
3. BaseEvent 透传:NonVoiceEvent(speakers=[]) 也 OK,Normalizer/Trigger 看到非空
   speakers 不修改
4. SimpleSpeakerRegistry:register / get / list / unregister / identify(None) / identify(embedding)
5. SpeakerRegistry.identify_speaker_info():陌生人 → unknown:<hash>,已知 user 走精确匹配
6. TOTHINK 1.1 场景 fixture:男主人/女主人/孩子 3 个家庭成员交替说话

## 7.5 阶段约束(本轮)

- SpeakerRegistry.identify(embedding=非 None) 永远返 is_known=False(7.6 真正声纹)
- VoiceEvent.speakers 永远空 list(7.6 VoiceAdapter 落地点)
- 老测试零修改(向后兼容)

## 7.6 真正声纹模型启动时,本测试要补

- EmbeddingSpeakerRegistry 余弦相似度匹配
- 同一 embedding 跨多次 identify 一致性
- 陌生人 → 用户确认 → register 流程
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from smartbutler.events.core.event import (
    SPEAKER_EMBEDDING_DIM,
    SPEAKER_MATCH_THRESHOLD,
    BaseEvent,
    EventPriority,
    EventSource,
    SpeakerInfo,
)
from smartbutler.events.protocol.voice_event import VoiceAction, VoiceEvent
from smartbutler.events.routing.speaker_registry import (
    SpeakerMatch,
    SpeakerProfile,
    SpeakerRegistry,
)
from smartbutler.events.routing.speaker_registry_simple import (
    SimpleSpeakerRegistry,
)


# ===========================================================================
# SpeakerInfo 模型
# ===========================================================================


class TestSpeakerInfoModel:
    def test_minimal_construct(self) -> None:
        """最小构造:user_id + confidence 必填。"""
        sp = SpeakerInfo(user_id="gaotianyu", confidence=0.95)
        assert sp.user_id == "gaotianyu"
        assert sp.confidence == 0.95
        assert sp.is_known is False  # 默认 False
        assert sp.embedding is None  # 默认 None(7.5 占位)
        assert sp.voice_segment_id == 0
        assert sp.language is None

    def test_confidence_must_be_in_range(self) -> None:
        """confidence 超出 [0, 1] 范围 → ValidationError。"""
        with pytest.raises(ValidationError):
            SpeakerInfo(user_id="u1", confidence=1.5)
        with pytest.raises(ValidationError):
            SpeakerInfo(user_id="u1", confidence=-0.1)

    def test_user_id_must_be_nonempty(self) -> None:
        """user_id 空字符串 → ValidationError。"""
        with pytest.raises(ValidationError):
            SpeakerInfo(user_id="", confidence=0.5)

    def test_voice_segment_id_must_be_nonneg(self) -> None:
        """voice_segment_id 负数 → ValidationError。"""
        with pytest.raises(ValidationError):
            SpeakerInfo(user_id="u1", confidence=0.5, voice_segment_id=-1)

    def test_is_known_explicit_true(self) -> None:
        """显式 is_known=True 保留(7.6 SpeakerRegistry 真正匹配后填 True)。"""
        sp = SpeakerInfo(user_id="u1", confidence=0.9, is_known=True)
        assert sp.is_known is True

    def test_frozen(self) -> None:
        """frozen 模型:赋值 → ValidationError。"""
        sp = SpeakerInfo(user_id="u1", confidence=0.5)
        with pytest.raises(ValidationError):
            sp.user_id = "hacker"  # type: ignore[misc]

    def test_extra_forbid(self) -> None:
        """额外字段被 forbid(类似 BaseEvent 风格)。"""
        with pytest.raises(ValidationError):
            SpeakerInfo(  # type: ignore[call-arg]
                user_id="u1", confidence=0.5, evil_attr="x"
            )

    def test_embedding_can_be_set(self) -> None:
        """embedding 可为 512 维向量(7.6 真正声纹)。"""
        emb = [0.0] * SPEAKER_EMBEDDING_DIM
        sp = SpeakerInfo(
            user_id="u1", confidence=0.9, is_known=True, embedding=emb
        )
        assert sp.embedding is not None
        assert len(sp.embedding) == SPEAKER_EMBEDDING_DIM


# ===========================================================================
# BaseEvent.speakers 字段
# ===========================================================================


class TestBaseEventSpeakersField:
    def test_default_empty_list(self) -> None:
        """不传 speakers → 默认空 list(向后兼容)。"""
        e = BaseEvent(
            source=EventSource.DEVICE,
            topic="device.door.opened",
            user_id="u1",
            timestamp=__import__("datetime").datetime.now(),
        )
        assert e.speakers == []
        assert e.media_refs == {}  # 顺便验证 7.4 钩子

    def test_explicit_speakers(self) -> None:
        """显式传 speakers(7.6 真声纹场景)。"""
        sp = SpeakerInfo(user_id="gaotianyu", confidence=0.95, is_known=True)
        e = BaseEvent(
            source=EventSource.VOICE,
            topic="voice.command",
            user_id="u1",  # 设备主
            timestamp=__import__("datetime").datetime.now(),
            speakers=[sp],
        )
        assert len(e.speakers) == 1
        assert e.speakers[0].user_id == "gaotianyu"

    def test_frozen_cannot_modify_speakers(self) -> None:
        """frozen:不可改 speakers(只能构造新 BaseEvent)。"""
        e = BaseEvent(
            source=EventSource.DEVICE,
            topic="x",
            user_id="u1",
            timestamp=__import__("datetime").datetime.now(),
        )
        with pytest.raises(ValidationError):
            e.speakers = [SpeakerInfo(user_id="x", confidence=0.5)]  # type: ignore[misc]


# ===========================================================================
# VoiceEvent.speakers + to_user_input 拼接
# ===========================================================================


class TestVoiceEventSpeakers:
    def test_make_default_no_speakers(self) -> None:
        """make() 不传 speakers → 空 list,to_user_input 无参与者片段。"""
        e = VoiceEvent.make(
            action=VoiceAction.COMMAND,
            device_id="speaker.living_room",
            user_id="u1",
            asr_text="今天周几了",
        )
        assert e.speakers == []
        # to_user_input 不应该包含 [会话参与者:...]
        text = e.to_user_input()
        assert "今天周几了" in text
        assert "[会话参与者:" not in text

    def test_make_with_speakers(self) -> None:
        """make() 传 speakers → to_user_input 拼参与者列表。"""
        sp_dad = SpeakerInfo(user_id="dad", confidence=0.95, is_known=True)
        sp_mom = SpeakerInfo(user_id="mom", confidence=0.72, is_known=True)
        e = VoiceEvent.make(
            action=VoiceAction.COMMAND,
            device_id="speaker.living_room",
            user_id="dad",  # 设备主
            asr_text="今天周几了",
            speakers=[sp_dad, sp_mom],
        )
        assert len(e.speakers) == 2
        text = e.to_user_input()
        assert "今天周几了" in text
        assert "[会话参与者:dad(95%)+mom(72%)]" in text

    def test_make_with_unknown_speaker(self) -> None:
        """陌生人(is_known=False)→ 拼 "陌生人(...)"。"""
        sp = SpeakerInfo(user_id="unknown:abc12345", confidence=0.5, is_known=False)
        e = VoiceEvent.make(
            action=VoiceAction.COMMAND,
            device_id="speaker.living_room",
            user_id="u1",
            asr_text="hello",
            speakers=[sp],
        )
        text = e.to_user_input()
        assert "陌生人(unknown:abc12345)(50%)" in text

    def test_to_user_input_wake_word_no_speakers(self) -> None:
        """非 COMMAND 动作 + 无 speakers → 走标准事件格式。"""
        e = VoiceEvent.make(
            action=VoiceAction.WAKE_WORD,
            device_id="speaker.living_room",
            user_id="u1",
            wake_word="阿福",
        )
        text = e.to_user_input()
        assert "[系统事件] 语音事件: wake_word" in text
        assert "wake_word=阿福" in text
        # 无 speakers → 不拼
        assert "speakers=" not in text


# ===========================================================================
# DeviceEvent 不变(向后兼容)
# ===========================================================================


class TestDeviceEventBackwardCompat:
    def test_device_event_no_speakers(self) -> None:
        """BaseEvent(speakers=[]) 默认 OK,7.5 现有代码零修改。

        用 BaseEvent 直接验证,避开 DeviceEvent 的额外必填字段(action/source/topic)——
        本测试只关心 speakers 字段向后兼容。
        """
        import datetime as _dt

        e = BaseEvent(
            source=EventSource.DEVICE,
            topic="device.door.opened",
            user_id="u1",
            timestamp=_dt.datetime.now(),
        )
        assert e.speakers == []  # 默认空 list
        assert e.media_refs == {}  # 7.4 钩子也默认空 dict
        # frozen 验证
        with pytest.raises(ValidationError):
            e.speakers = [SpeakerInfo(user_id="x", confidence=0.5)]  # type: ignore[misc]


# ===========================================================================
# SimpleSpeakerRegistry
# ===========================================================================


class TestSimpleSpeakerRegistry:
    @pytest.mark.asyncio
    async def test_register_and_get(self) -> None:
        reg = SimpleSpeakerRegistry()
        profile = SpeakerProfile(user_id="gaotianyu", display_name="高天禹")
        await reg.register(profile)
        got = await reg.get("gaotianyu")
        assert got is not None
        assert got.display_name == "高天禹"

    @pytest.mark.asyncio
    async def test_register_duplicate_raises(self) -> None:
        reg = SimpleSpeakerRegistry()
        await reg.register(SpeakerProfile(user_id="u1", display_name="User 1"))
        with pytest.raises(ValueError, match="u1.*已注册"):
            await reg.register(SpeakerProfile(user_id="u1", display_name="User 1"))

    @pytest.mark.asyncio
    async def test_list_members(self) -> None:
        reg = SimpleSpeakerRegistry()
        await reg.register(SpeakerProfile(user_id="dad", display_name="Dad"))
        await reg.register(SpeakerProfile(user_id="mom", display_name="Mom"))
        members = await reg.list_members()
        assert len(members) == 2
        user_ids = {m.user_id for m in members}
        assert user_ids == {"dad", "mom"}

    @pytest.mark.asyncio
    async def test_unregister(self) -> None:
        reg = SimpleSpeakerRegistry()
        await reg.register(SpeakerProfile(user_id="u1", display_name="U1"))
        assert await reg.unregister("u1") is True
        assert await reg.unregister("u1") is False  # 第二次返回 False
        assert await reg.get("u1") is None

    @pytest.mark.asyncio
    async def test_identify_with_none_embedding_7_5(self) -> None:
        """7.5 阶段:identify(embedding=None) 永远返 is_known=False(没真声纹)。"""
        reg = SimpleSpeakerRegistry()
        await reg.register(SpeakerProfile(user_id="u1", display_name="U1"))
        match = await reg.identify(None)
        assert match.matched_user_id is None
        assert match.is_known is False
        assert match.confidence == 0.0
        assert match.is_above_threshold is False

    @pytest.mark.asyncio
    async def test_identify_with_embedding_7_5_placeholder(self) -> None:
        """7.5 阶段:identify(embedding=非 None) 仍返 is_known=False(占位)。

        7.6 真正声纹启动时,本测试要改成期望匹配成功。
        """
        reg = SimpleSpeakerRegistry()
        await reg.register(SpeakerProfile(user_id="u1", display_name="U1"))
        match = await reg.identify([0.1] * SPEAKER_EMBEDDING_DIM)
        assert match.matched_user_id is None
        assert match.is_known is False

    @pytest.mark.asyncio
    async def test_identify_speaker_info_known_user(self) -> None:
        """identify_speaker_info:已知 user → SpeakerInfo(is_known=True)。

        但 7.5 阶段永远不返回 known(因 identify 永远 unknown)。
        7.6 真正声纹启动时,本测试要补 known=True 场景。
        """
        reg = SimpleSpeakerRegistry()
        await reg.register(SpeakerProfile(user_id="u1", display_name="U1"))
        info = await reg.identify_speaker_info([0.1] * SPEAKER_EMBEDDING_DIM)
        assert info.user_id.startswith("unknown:")  # 7.5 永远 unknown
        assert info.is_known is False

    @pytest.mark.asyncio
    async def test_identify_speaker_info_unknown_hash_stable(self) -> None:
        """同一 embedding 多次识别 → 同一 unknown hash(可重现)。"""
        reg = SimpleSpeakerRegistry()
        emb = [0.5, 0.3] + [0.0] * (SPEAKER_EMBEDDING_DIM - 2)
        info1 = await reg.identify_speaker_info(emb)
        info2 = await reg.identify_speaker_info(emb)
        assert info1.user_id == info2.user_id
        assert info1.user_id.startswith("unknown:")

    def test_sync_register_and_count(self) -> None:
        reg = SimpleSpeakerRegistry()
        reg.register_sync(SpeakerProfile(user_id="u1", display_name="U1"))
        assert reg.count() == 1
        reg.clear_all()
        assert reg.count() == 0

    def test_sync_register_duplicate_raises(self) -> None:
        reg = SimpleSpeakerRegistry()
        reg.register_sync(SpeakerProfile(user_id="u1", display_name="U1"))
        with pytest.raises(ValueError):
            reg.register_sync(SpeakerProfile(user_id="u1", display_name="U1"))


# ===========================================================================
# TOTHINK 1.1 场景 fixture
# ===========================================================================


class TestTothinkScenario11:
    """TOTHINK.md §1.1 场景:

    - 男主人"今天周几了" → VoiceEvent(speakers=[dad])
    - 女主人"明天怎么又要上班了" → VoiceEvent(speakers=[mom])
    - 男主人"哦对了,明天我要出差,帮我订票" → VoiceEvent(speakers=[dad])

    验证:
    1. speakers 字段正确传递(每人每次说话带自己的声纹)
    2. to_user_input 拼出 "[会话参与者:dad(95%)]" 等
    3. MemoryFacade / TaskBoundary 后续能按 user_ids 拆情绪(task 维度优先)
    """

    def test_scenario_11_dad_asks_date(self) -> None:
        sp_dad = SpeakerInfo(user_id="dad", confidence=0.95, is_known=True)
        e = VoiceEvent.make(
            action=VoiceAction.COMMAND,
            device_id="speaker.living_room",
            user_id="dad",  # 设备主(触发说话的人)
            asr_text="今天周几了",
            speakers=[sp_dad],
        )
        assert e.speakers[0].user_id == "dad"
        text = e.to_user_input()
        assert "今天周几了" in text
        assert "[会话参与者:dad(95%)]" in text

    def test_scenario_11_mom_complains(self) -> None:
        sp_mom = SpeakerInfo(user_id="mom", confidence=0.88, is_known=True)
        e = VoiceEvent.make(
            action=VoiceAction.COMMAND,
            device_id="speaker.living_room",
            user_id="mom",
            asr_text="明天怎么又要上班了",
            speakers=[sp_mom],
        )
        text = e.to_user_input()
        assert "[会话参与者:mom(88%)]" in text

    def test_scenario_12_task_switching(self) -> None:
        """TOTHINK §1.2:订票中途切到打电话(中间插入不同 user)→ 后续切回订票。

        当前测试只验证协议层能正确表示"当前发言者",task 切分交给 Phase 6.5/6b。
        """
        # task1 订票:男主人
        sp_dad1 = SpeakerInfo(user_id="dad", confidence=0.92, is_known=True)
        e1 = VoiceEvent.make(
            action=VoiceAction.COMMAND,
            device_id="speaker.living_room",
            user_id="dad",
            asr_text="我明天出差,帮我订票",
            speakers=[sp_dad1],
        )
        assert e1.speakers[0].user_id == "dad"

        # task2 打电话:女主人
        sp_mom = SpeakerInfo(user_id="mom", confidence=0.85, is_known=True)
        e2 = VoiceEvent.make(
            action=VoiceAction.COMMAND,
            device_id="speaker.living_room",
            user_id="dad",  # 设备主不变(男主人那台设备)
            asr_text="帮我打电话给我老婆",
            speakers=[sp_dad1, sp_mom],  # 男主人说 + 系统识别到老婆插话
        )
        assert len(e2.speakers) == 2

        # task1 切回订票:男主人
        e3 = VoiceEvent.make(
            action=VoiceAction.COMMAND,
            device_id="speaker.living_room",
            user_id="dad",
            asr_text="继续订票",
            speakers=[sp_dad1],
        )
        assert e3.speakers[0].user_id == "dad"
