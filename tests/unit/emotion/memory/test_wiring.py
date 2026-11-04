"""🆕 2026-11-02: 记忆系统装配层(wiring)测试。

核心验证:**记忆系统的所有组件能被装配起来并真正工作**。

背景:Phase 6 的各组件(短库/长库/task 索引/调度器/自动抽取/升格)
各自有单测,但**从来没有被组装起来跑过** —— 线上进程里没有实例,
所以长期记忆不写不读、短期 task 摘要也不注入 prompt。
这个模块就是补上这一段。

测试:
1. 装配能成功,产出完整句柄
2. 组件之间真的连上了(facade ↔ consolidator / forgetting / view)
3. 显示名解析器绑定生效
4. scheduler 能启停
5. 端到端:装配 → 写 fact → 升格 → 召回,归属贯穿
"""

from __future__ import annotations

from pathlib import Path

import pytest

from smartbutler.emotion.memory.wiring import MemorySystem, build_memory_system


class _FakeSettings:
    """假配置(避免依赖 .env)。"""

    n_completed_tasks = 3
    n_chitchat_tasks = 30
    n_prior_tasks = 3
    recall_min_score = 0.0
    judge_interval_seconds = 3600.0
    judge_on_message = False
    judge_min_interval_seconds = 60.0
    on_message_debounce_seconds = 0.0
    paused_max_staleness = 5
    judge_window_size = 2
    auto_extract_enabled = True
    auto_extract_min_importance = 0.0
    judge_timeout_seconds = 8.0


@pytest.fixture
async def system(tmp_path: Path) -> MemorySystem:
    """装配一个记忆系统(data_dir + qdrant 都指向 tmp_path)。

    注意:嵌入式 Qdrant **不支持多进程并发访问**,所以必须给每个测试
    独立的 qdrant_path,否则报 "already accessed by another instance"。
    """
    sys_ = await build_memory_system(
        _FakeSettings(),
        llm=None,  # 不依赖 LLM,走规则降级
        data_dir=tmp_path / "memory",
        qdrant_path=tmp_path / "qdrant",
        qdrant_collection="test_memory",
    )
    yield sys_
    await sys_.stop()


# ---------------------------------------------------------------------------
# 1. 装配完整性
# ---------------------------------------------------------------------------


async def test_build_memory_system_returns_full_handle(system: MemorySystem) -> None:
    """装配产出完整句柄,各组件都在。"""
    assert system.facade is not None
    assert system.short_term is not None
    assert system.long_term is not None
    assert system.task_index is not None
    assert system.segmenter is not None
    assert system.scheduler is not None
    assert system.auto_extract is not None
    assert system.consolidator is not None
    assert system.forgetting is not None


async def test_components_are_wired_together(system: MemorySystem) -> None:
    """组件之间真的连上了(不是各建各的)。"""
    # facade ↔ consolidator:facade 能触发升格
    assert system.facade._consolidator is system.consolidator
    # facade ↔ forgetting
    assert system.facade._forgetting is system.forgetting
    # facade ↔ 短期视图(按 task 池切窗)
    assert system.facade._short_term_view is not None
    # 短期视图用同一个 task_index
    assert system.facade._short_term_view._task_index is system.task_index
    # 自动抽取 hook 注册到了 hooks
    assert system.auto_extract is not None


async def test_name_resolver_binding(system: MemorySystem) -> None:
    """显示名解析器绑定生效。"""
    system.bind_name_resolver(lambda uid: {"u_dad": "爸爸"}.get(uid, uid))
    assert system.facade._display_name("u_dad") == "爸爸"
    assert system.facade._display_name("u_x") == "u_x"
    # 空 id → "未知"
    assert system.facade._display_name("") == "未知"


async def test_name_resolver_failure_does_not_break_render(system: MemorySystem) -> None:
    """解析器抛异常不能影响召回渲染(降级显示 user_id)。"""

    def _boom(uid: str) -> str:
        raise RuntimeError("registry down")

    system.bind_name_resolver(_boom)
    assert system.facade._display_name("u_dad") == "u_dad"


async def test_scheduler_start_and_stop(system: MemorySystem) -> None:
    """scheduler 能启动和优雅停止。"""
    import asyncio

    system.start()
    await asyncio.sleep(0)  # 让 start 任务跑起来
    await system.stop()


# ---------------------------------------------------------------------------
# 2. 端到端:装配 → 写 fact → 升格 → 召回
# ---------------------------------------------------------------------------


async def test_end_to_end_attribution_survives_promotion(
    system: MemorySystem, tmp_path: Path
) -> None:
    """🆕 核心端到端:fact → 日报 → 召回,归属贯穿。

    这是整套改造的**不变式验证**:
    归属(谁说的)由代码以 tag 落库,升格时由代码重新写一遍,
    召回时由代码读出来标注 —— LLM 全程不参与。
    """
    from smartbutler.emotion.memory.facade import MemoryContext
    from smartbutler.emotion.memory.levels import MemoryLevel

    facade = system.facade

    # 1) 写两条 fact(模拟 auto_extract 的产物:共享 task,不同说话人)
    await facade.remember(
        "[user=u_dad] 我明天要去上海出差",
        tags=["user:u_dad", "task:t_trip", "category:booking"],
        importance=0.8,
    )
    await facade.remember(
        "[user=u_mom] 我也去",
        tags=["user:u_mom", "task:t_trip", "category:booking"],
        importance=0.8,
    )

    # 2) 升格(fact → 日报)。这里直接用 consolidator 内部方法,
    #    因为正式流程按"本地昨日"窗口,测试里不构造真实时间。
    facts = [
        {
            "key": "f1",
            "content": "[user=u_dad] 我明天要去上海出差",
            "importance": 0.8,
            "created_at": "2026-11-01T10:00:00+00:00",
            "tags": ["user:u_dad", "task:t_trip"],
        },
        {
            "key": "f2",
            "content": "[user=u_mom] 我也去",
            "importance": 0.8,
            "created_at": "2026-11-01T11:00:00+00:00",
            "tags": ["user:u_mom", "task:t_trip"],
        },
    ]
    groups = system.consolidator._group_by_subject(facts)
    assert len(groups) == 1, "共享 task 应归为一个主体组(主说话人)"

    key = await system.consolidator._write_summary(
        summary="爸爸明天要去上海出差,妈妈也一起去。",
        target_level=MemoryLevel.DAILY,
        source_keys=["f1", "f2"],
        importance=0.8,
        period="昨天",
        source_count=2,
        group=groups[0],
    )
    assert key is not None

    # 3) 读回日报的 tags —— 归属必须还在
    written = system.long_term.list_keys_by_level(level="daily")
    assert len(written) == 1
    tags = written[0]["tags"]
    assert "user:u_dad" in tags, f"日报丢了主归属: {tags}"
    assert "task:t_trip" in tags, f"日报丢了 task 锚点: {tags}"
    assert any(t.startswith("participants:") for t in tags), (
        f"日报丢了参与者: {tags}"
    )
    assert "level:daily" in tags

    # 4) 召回渲染 —— 按 task 分组 + 显示名
    system.bind_name_resolver(
        lambda uid: {"u_dad": "爸爸", "u_mom": "妈妈"}.get(uid, uid)
    )
    block = await facade.format_for_prompt(
        query="上海 出差",
        ctx=MemoryContext(user_id="u_dad", source="u_dad", session_id="s1"),
        k=5,
    )
    assert "t_trip" in block, f"召回块未按 task 分组:\n{block}"
    assert "爸爸" in block or "妈妈" in block, f"归属未解析成显示名:\n{block}"


async def test_grouping_is_by_subject_not_task(system: MemorySystem) -> None:
    """产出量 = 时间窗 × 主体数,不是 × task 数。

    类比:员工一天做 2 个项目,汇报的是**一条**日报,
    分别讲两个项目的进展 —— 而不是两条日报。
    """
    facts = [
        {
            "key": "f1", "content": "A", "importance": 0.7,
            "created_at": "2026-11-01T10:00:00+00:00",
            "tags": ["user:u_dad", "task:t1"],
        },
        {
            "key": "f2", "content": "B", "importance": 0.7,
            "created_at": "2026-11-01T11:00:00+00:00",
            "tags": ["user:u_dad", "task:t2"],
        },
        {
            "key": "f3", "content": "C", "importance": 0.7,
            "created_at": "2026-11-01T12:00:00+00:00",
            "tags": ["user:u_mom", "task:t3"],
        },
    ]
    groups = system.consolidator._group_by_subject(facts)

    # 2 个 task 在爸爸名下 + 1 个 task 在妈妈名下 → 2 条日报
    assert len(groups) == 2
    dad = next(g for g in groups if g.primary_user == "u_dad")
    assert set(dad.task_ids) == {"t1", "t2"}
    assert len(dad.segments) == 2, "日报内部按项目分段"
