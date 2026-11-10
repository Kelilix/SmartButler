"""L1/L2 打断机制单元测试(2026-11-10)。

## 覆盖的问题

用户连续说话时,管家会"把之前说的依次说完":

    用户:"帮我查下明天天气"      → seq=1 → LLM 处理中(10s)
    用户:"对了顺便订张机票"       → seq=2 → 排队
    用户:"对了我的身份证号是..."   → seq=3 → 排队
                                       ↓
    LLM 算完 seq=1 → 播报 → 用户:"我不想听这个"
    LLM 算完 seq=2 → 播报 → 用户:"不是这个"

## 覆盖内容

1. ``GenerationTracker`` —— 代次分配与过期判断
2. **L1**:EventBus 队列层丢弃过期事件(不进 LLM)
3. **L2**:``TTSOutputAdapter`` 播报前丢弃过期回复(不播)
4. 回归:无代次信息的事件行为不变(非语音路径)
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from smartbutler.events.core.event import BaseEvent, EventPriority, EventSource
from smartbutler.events.core.reply_metadata import event_metadata
from smartbutler.events.protocol.voice_event import VoiceAction, VoiceEvent
from smartbutler.server.bus import InMemoryEventBus
from smartbutler.voice.generation import GenerationTracker

# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


def _voice_event(generation: int, text: str = "测试") -> VoiceEvent:
    """造一个带代次的语音事件。

    ⚠️ ``generation`` **必须**来自 :meth:`GenerationTracker.issue` 的返回值。
    手写死数字会让代次号与 tracker 内部计数脱节,过期判断失准。
    """
    return VoiceEvent.make(
        action=VoiceAction.COMMAND,
        device_id="dev1",
        user_id="u1",
        asr_text=text,
        generation=generation,
    )


async def _issue_event(tracker: GenerationTracker, text: str = "测试") -> VoiceEvent:
    """用 tracker 签发代次号并造事件(推荐用法)。"""
    seq = await tracker.issue()
    return _voice_event(seq, text)


def _plain_event() -> BaseEvent:
    """造一个非语音事件(无代次)。"""
    from datetime import datetime

    return BaseEvent(
        source=EventSource.DEVICE,
        topic="device.door.opened",
        user_id="u1",
        priority=EventPriority.NORMAL,
        payload={},
        timestamp=datetime.now(),
    )


class TestGenerationTracker:
    """代次追踪器本身。"""

    @pytest.mark.asyncio
    async def test_issue_increments(self) -> None:
        """代次号严格单调递增。

        ⚠️ 代次是 **monotonic 毫秒时间戳**(不是简单计数器),
        所以不能断言具体数值,只断言"严格递增"。
        """
        tracker = GenerationTracker()
        g1 = await tracker.issue()
        g2 = await tracker.issue()
        g3 = await tracker.issue()
        assert g1 < g2 < g3, f"代次必须严格递增: {g1}, {g2}, {g3}"
        assert tracker.latest_seq() == g3
        # 必须是真实时间戳量级(毫秒),不是 1/2/3 这种计数器
        assert g1 > 1000, f"代次应为毫秒时间戳量级,实际 {g1}"

    @pytest.mark.asyncio
    async def test_issue_strictly_increments_even_same_millisecond(self) -> None:
        """同一毫秒内连续签发也必须严格递增(方案 C 的核心不变量)。

        ⚠️ ``time.monotonic()`` 在精度不足时可能返回相同值,
        这会破坏"代次大小 == 发言先后"的等价关系 → 过期判断失效。
        签发时用 ``max(now_ms, previous + 1)`` 保证严格递增。
        """
        tracker = GenerationTracker()
        g1 = await tracker.issue()
        g2 = await tracker.issue()
        g3 = await tracker.issue()
        assert g1 < g2 < g3
        # 最小间隔 1ms(兜底值,真人说话不会这么快)
        assert g2 - g1 >= 1
        assert g3 - g2 >= 1

    @pytest.mark.asyncio
    async def test_generation_is_timestamp_scale(self) -> None:
        """代次是 monotonic 毫秒时间戳,量级远大于计数器。

        这保证了两件事:
        - 日志里能直接看出"这次发言距今多久"
        - 跨进程/重启可比(计数器会归零)
        """
        import time as _time

        tracker = GenerationTracker()
        gen = await tracker.issue()
        now_ms = int(_time.monotonic() * 1000)
        # 与当前时间相差不超过 1 秒(允许时钟精度误差)
        assert abs(now_ms - gen) <= 1000, f"代次 {gen} 应接近当前毫秒时间戳 {now_ms}"

    @pytest.mark.asyncio
    async def test_is_stale_detects_superseded(self) -> None:
        """有过更新的输入 → 旧代次判过期。"""
        tracker = GenerationTracker()
        seq1 = await tracker.issue()
        assert not tracker.is_stale(seq1)  # 还是最新的

        await tracker.issue()  # 用户又说话了
        assert tracker.is_stale(seq1)  # seq1 过期了

    @pytest.mark.asyncio
    async def test_latest_is_never_stale(self) -> None:
        """最新代次永远不过期。"""
        tracker = GenerationTracker()
        seq = await tracker.issue()
        assert not tracker.is_stale(seq)

    @pytest.mark.asyncio
    async def test_zero_generation_never_stale(self) -> None:
        """代次 0(未知)不判过期 —— 兼容非语音路径。"""
        tracker = GenerationTracker()
        await tracker.issue()
        assert not tracker.is_stale(0)

    @pytest.mark.asyncio
    async def test_stale_window_loose_mode(self) -> None:
        """宽松模式:最近的更新仍算过期。

        场景:gen1 发出 → gen2 发出(间隔 < window) → 现在立刻检查
        → gen1 应该算过期(因为 gen2 是"刚刚"发生的)。
        """
        tracker = GenerationTracker(stale_window_seconds=60.0)
        seq1 = await tracker.issue()
        await tracker.issue()
        assert tracker.is_stale(seq1)

    @pytest.mark.asyncio
    async def test_stale_window_ignores_old_update(self) -> None:
        """宽松模式:**很久以前**的更新不算过期(关键语义)。

        场景:
            T=0   用户:"帮我查天气"(gen1)
            T=5   用户:"嗯"(gen2)
            T=100 LLM 算完 gen1

        宽松判定看的是"最新更新距今多久"(95s > 60s window),
        而不是"两个代次间隔了多久"(5s ≤ 60s)。
        后者会把一条等了 100 秒的回复错误地丢弃掉。

        ⚠️ 通过 `now` 参数注入时间,不真的 sleep。
        """
        import time as _time

        tracker = GenerationTracker(stale_window_seconds=60.0)
        seq1 = await tracker.issue()
        await tracker.issue()  # gen2

        # 现在 = gen2 发出后 95 秒
        future_now = _time.monotonic() + 95.0
        assert not tracker.is_stale(seq1, now=future_now), (
            "95 秒前的更新不该让 gen1 过期"
        )

    @pytest.mark.asyncio
    async def test_stale_window_strict_mode_ignores_now(self) -> None:
        """严格模式:任何新输入都让旧回复过期(与 now 无关)。"""
        import time as _time

        tracker = GenerationTracker(stale_window_seconds=0.0)
        seq1 = await tracker.issue()
        await tracker.issue()

        # 即使过了很久,严格模式仍然判过期
        assert tracker.is_stale(seq1, now=_time.monotonic() + 9999.0)

    @pytest.mark.asyncio
    async def test_stats_tracked(self) -> None:
        """丢弃统计可观测。"""
        tracker = GenerationTracker()
        await tracker.issue()
        tracker.note_dropped_stale()
        tracker.note_dropped_speech()
        snap = tracker.snapshot()
        assert snap["stats"]["dropped_stale"] == 1
        assert snap["stats"]["dropped_speech"] == 1
        assert snap["stats"]["issued"] == 1


class TestL1QueueLevelDrop:
    """L1:过期事件不进 LLM。"""

    @pytest.mark.asyncio
    async def test_stale_event_not_delivered(self) -> None:
        """队列里还没被取出的过期事件被丢弃,不进 handler。

        ⚠️ 保证边界(重要):
            - **队列中**的过期事件 → 丢弃 ✅(本测试覆盖)
            - **正在处理中**的事件 → 无法丢弃(handler 已经在跑了)
              → 只能靠 L2「不播它的结果」兜底(见 TestL2StaleReplyDrop)
        """
        tracker = GenerationTracker()
        bus = InMemoryEventBus(generation_tracker=tracker)
        received: list[int] = []

        async def handler(event: BaseEvent) -> None:
            received.append(getattr(event, "generation", 0))
            # 模拟 LLM 慢
            await asyncio.sleep(0.05)

        await bus.start()
        await bus.subscribe("voice.*", handler)

        # gen1 先进队列
        gen1 = await tracker.issue()
        await bus.publish(_voice_event(gen1))
        await asyncio.sleep(0.01)  # 让 consumer 取出 gen1 并开始处理

        # 用户连说两句 → gen2, gen3
        gen2 = await tracker.issue()
        await bus.publish(_voice_event(gen2))
        gen3 = await tracker.issue()
        await bus.publish(_voice_event(gen3))

        await asyncio.sleep(0.3)
        await bus.stop()

        # gen1 会处理(它是当时最新的)
        # gen2 必须被丢弃 —— 它在队列里,用户已经说了 gen3
        assert gen2 not in received, f"过期的 gen2 不该进 handler,实际收到 {received}"
        assert gen3 in received, f"最新的 gen3 应被处理,实际收到 {received}"
        assert gen1 in received, f"gen1 在被取出时是最新,应该被处理,实际收到 {received}"

    @pytest.mark.asyncio
    async def test_pending_stale_dropped_at_publish(self) -> None:
        """publish 时清理队列里的过期事件(队列层清理)。

        构造:consumer 被 seq=1 占住 → 队列里堆了 seq=2 →
        用户说新话(seq=3)→ publish(seq=3) 时把队列里的 seq=2 清掉。
        """
        tracker = GenerationTracker()
        bus = InMemoryEventBus(generation_tracker=tracker)
        received: list[int] = []

        async def handler(event: BaseEvent) -> None:
            received.append(getattr(event, "generation", 0))
            await asyncio.sleep(0.2)  # 占住 consumer,让事件堆队列

        await bus.start()
        await bus.subscribe("voice.*", handler)

        gen1 = await tracker.issue()
        await bus.publish(_voice_event(gen1))
        await asyncio.sleep(0.01)  # consumer 取出 gen1,开始处理(0.2s)

        # 用户说了 gen2 → 入队
        gen2 = await tracker.issue()
        await bus.publish(_voice_event(gen2))
        await asyncio.sleep(0.01)  # 队列里现在有 gen2(consumer 还忙)

        # 用户又说新话 → gen3 → publish 时应清掉队列里的 gen2
        gen3 = await tracker.issue()
        await bus.publish(_voice_event(gen3))

        await asyncio.sleep(0.8)
        await bus.stop()

        assert gen2 not in received, f"gen2 应在队列层被清掉,实际收到 {received}"
        assert gen3 in received, f"gen3 应被处理,实际收到 {received}"

    @pytest.mark.asyncio
    async def test_no_tracker_keeps_old_behavior(self) -> None:
        """没配 tracker 时行为不变(全部投递)。"""
        bus = InMemoryEventBus()
        received: list[int] = []

        async def handler(event: BaseEvent) -> None:
            received.append(getattr(event, "generation", 0))

        await bus.start()
        await bus.subscribe("voice.*", handler)
        await bus.publish(_voice_event(1))
        await bus.publish(_voice_event(2))
        await asyncio.sleep(0.1)
        await bus.stop()

        assert received == [1, 2]
    @pytest.mark.asyncio
    async def test_non_voice_event_never_dropped(self) -> None:
        """非语音事件(无代次)永不被丢弃。"""
        tracker = GenerationTracker()
        bus = InMemoryEventBus(generation_tracker=tracker)
        received: list[str] = []

        async def handler(event: BaseEvent) -> None:
            received.append(event.topic)

        await bus.start()
        await bus.subscribe("*", handler)

        await tracker.issue()
        await tracker.issue()
        await bus.publish(_plain_event())
        await asyncio.sleep(0.1)
        await bus.stop()

        assert received == ["device.door.opened"]

    @pytest.mark.asyncio
    async def test_drop_stale_pending_returns_count(self) -> None:
        """手动清理接口返回丢弃数量。"""
        tracker = GenerationTracker()
        bus = InMemoryEventBus(generation_tracker=tracker)
        await bus.start()

        # 直接塞队列(绕过 consumer)
        await bus._queue.put(await _issue_event(tracker))  # noqa: SLF001
        await tracker.issue()
        await tracker.issue()
        await bus._queue.put(await _issue_event(tracker))  # noqa: SLF001

        dropped = await bus.drop_stale_pending()
        assert dropped == 1
        assert bus._queue.qsize() == 1  # noqa: SLF001
        await bus.stop()


class TestL2StaleReplyDrop:
    """L2:过期回复不播。"""

    @pytest.mark.asyncio
    async def test_stale_reply_not_spoken(self) -> None:
        """LLM 算完后发现已过期 → 不播。"""
        from smartbutler.server.adapters import ButlerReply
        from smartbutler.voice.adapter.tts_output_adapter import TTSOutputAdapter

        tracker = GenerationTracker()
        spoken: list[str] = []

        class _FakeTTS:
            def speak(self, text: str, voice: Any = None) -> Any:
                async def _gen() -> Any:
                    yield b"audio"
                    spoken.append(text)

                return _gen()

            async def close(self) -> None:
                pass

        class _FakePlayer:
            async def play_gated(self, stream: Any) -> None:
                async for _ in stream:
                    pass

            async def close(self) -> None:
                pass

        adapter = TTSOutputAdapter(
            tts=_FakeTTS(),  # type: ignore[arg-type]
            player=_FakePlayer(),  # type: ignore[arg-type]
            generation_tracker=tracker,
        )

        # gen1 的回复
        gen1 = await tracker.issue()
        stale_reply = ButlerReply(
            content="这是旧回复",
            metadata={"generation": gen1},
        )

        # 期间用户又说了话
        await tracker.issue()

        await adapter.push(stale_reply)
        assert spoken == [], f"过期回复不该播,实际播了 {spoken}"

    @pytest.mark.asyncio
    async def test_fresh_reply_spoken(self) -> None:
        """最新代次的回复正常播。"""
        from smartbutler.server.adapters import ButlerReply
        from smartbutler.voice.adapter.tts_output_adapter import TTSOutputAdapter

        tracker = GenerationTracker()
        spoken: list[str] = []

        class _FakeTTS:
            def speak(self, text: str, voice: Any = None) -> Any:
                async def _gen() -> Any:
                    yield b"audio"
                    spoken.append(text)

                return _gen()

            async def close(self) -> None:
                pass

        class _FakePlayer:
            async def play_gated(self, stream: Any) -> None:
                async for _ in stream:
                    pass

            async def close(self) -> None:
                pass

        adapter = TTSOutputAdapter(
            tts=_FakeTTS(),  # type: ignore[arg-type]
            player=_FakePlayer(),  # type: ignore[arg-type]
            generation_tracker=tracker,
        )

        seq = await tracker.issue()
        reply = ButlerReply(content="最新回复", metadata={"generation": seq})
        await adapter.push(reply)
        assert spoken == ["最新回复"]

    @pytest.mark.asyncio
    async def test_reply_without_generation_spoken(self) -> None:
        """没有代次信息的回复正常播(非语音路径回归)。"""
        from smartbutler.server.adapters import ButlerReply
        from smartbutler.voice.adapter.tts_output_adapter import TTSOutputAdapter

        tracker = GenerationTracker()
        spoken: list[str] = []

        class _FakeTTS:
            def speak(self, text: str, voice: Any = None) -> Any:
                async def _gen() -> Any:
                    yield b"audio"
                    spoken.append(text)

                return _gen()

            async def close(self) -> None:
                pass

        class _FakePlayer:
            async def play_gated(self, stream: Any) -> None:
                async for _ in stream:
                    pass

            async def close(self) -> None:
                pass

        adapter = TTSOutputAdapter(
            tts=_FakeTTS(),  # type: ignore[arg-type]
            player=_FakePlayer(),  # type: ignore[arg-type]
            generation_tracker=tracker,
        )

        await tracker.issue()
        await tracker.issue()
        # 无 generation 的 reply(老路径)
        await adapter.push(ButlerReply(content="无代次回复"))
        assert spoken == ["无代次回复"]

    @pytest.mark.asyncio
    async def test_no_tracker_spoken(self) -> None:
        """没配 tracker → 行为不变(总是播)。"""
        from smartbutler.server.adapters import ButlerReply
        from smartbutler.voice.adapter.tts_output_adapter import TTSOutputAdapter

        spoken: list[str] = []

        class _FakeTTS:
            def speak(self, text: str, voice: Any = None) -> Any:
                async def _gen() -> Any:
                    yield b"audio"
                    spoken.append(text)

                return _gen()

            async def close(self) -> None:
                pass

        class _FakePlayer:
            async def play_gated(self, stream: Any) -> None:
                async for _ in stream:
                    pass

            async def close(self) -> None:
                pass

        adapter = TTSOutputAdapter(
            tts=_FakeTTS(),  # type: ignore[arg-type]
            player=_FakePlayer(),  # type: ignore[arg-type]
        )
        await adapter.push(ButlerReply(content="照播"))
        assert spoken == ["照播"]


class TestMetadataPropagation:
    """代次号从事件透传到 reply。"""

    def test_event_metadata_extracts_generation(self) -> None:
        evt = _voice_event(7)
        assert event_metadata(evt) == {"generation": 7}

    def test_event_metadata_empty_for_non_voice(self) -> None:
        assert event_metadata(_plain_event()) == {}

    def test_butler_reply_generation_property(self) -> None:
        from smartbutler.server.adapters import ButlerReply

        reply = ButlerReply(content="x", metadata={"generation": 5})
        assert reply.generation == 5

    def test_butler_reply_generation_default(self) -> None:
        from smartbutler.server.adapters import ButlerReply

        assert ButlerReply(content="x").generation == 0

    def test_butler_reply_generation_invalid(self) -> None:
        from smartbutler.server.adapters import ButlerReply

        reply = ButlerReply(content="x", metadata={"generation": "abc"})
        assert reply.generation == 0

    def test_voice_event_carries_generation(self) -> None:
        evt = _voice_event(3)
        assert evt.generation == 3

    def test_voice_event_default_generation_zero(self) -> None:
        evt = VoiceEvent.make(
            action=VoiceAction.COMMAND,
            device_id="d",
            user_id="u",
            asr_text="hi",
        )
        assert evt.generation == 0
