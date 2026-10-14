"""重要性评分 单测(Phase 6.2 P0)。"""

from __future__ import annotations

import pytest

from smartbutler.emotion.memory.importance import (
    HeuristicScorer,
    ImportanceScorer,
)


class TestHeuristicScorer:
    def test_empty_content_gets_default(self) -> None:
        """空内容得 0.5。"""
        assert HeuristicScorer().score("") == 0.5
        assert HeuristicScorer().score("   ") == 0.5

    def test_content_without_signals_gets_base(self) -> None:
        """普通内容起步 0.5。"""
        s = HeuristicScorer().score("你好,今天吃饭了吗")
        # 0.5 起步,可能因为长度 100 字以下没 +0.2,所以是 0.5
        assert 0.5 <= s <= 0.7

    def test_content_with_number_gets_bonus(self) -> None:
        """含数字加分。"""
        s_no = HeuristicScorer().score("今天吃了饭")
        s_with = HeuristicScorer().score("明天 10 月 8 日有活动")
        # 有数字/日期的应该 >= 没的
        assert s_with >= s_no

    def test_long_content_gets_bonus(self) -> None:
        """>100 字加分。"""
        s_short = HeuristicScorer().score("短")
        long_text = "管家你好,我今天想跟你说说我们家最近的情况。" * 5  # > 100 字
        s_long = HeuristicScorer().score(long_text)
        assert s_long > s_short

    def test_emotion_word_gets_bonus(self) -> None:
        """含情绪词加分。"""
        s_no = HeuristicScorer().score("今天吃饭了")
        s_emotion = HeuristicScorer().score("我今天喜欢喝咖啡")
        assert s_emotion > s_no

    def test_score_in_range(self) -> None:
        """所有输入得分都在 [0, 1]。"""
        h = HeuristicScorer()
        for txt in ["", "hi", "我喜欢住在上海,10月8日去迪士尼,这是我的偏好,非常重要!" * 10]:
            s = h.score(txt)
            assert 0.0 <= s <= 1.0


class TestImportanceScorer:
    @pytest.mark.asyncio
    async def test_returns_heuristic(self) -> None:
        """P0 阶段:ImportanceScorer 返回启发式结果。"""
        s = await ImportanceScorer().score("我喜欢喝咖啡")
        # 启发式会加情绪词 +0.2,所以 0.7
        assert 0.5 <= s <= 1.0

    @pytest.mark.asyncio
    async def test_accepts_llm_kwarg(self) -> None:
        """接受 llm=None 参数(P0 不使用)。"""
        s = await ImportanceScorer(llm=None).score("hi")
        assert 0.0 <= s <= 1.0
