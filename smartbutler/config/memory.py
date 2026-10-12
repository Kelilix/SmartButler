"""记忆子模块配置(Phase 6.4 P2)。

不放在 ``storage.py`` 是因为职责单一:StorageSettings 管"数据存哪",
MemorySettings 管"记忆策略参数"(完成态摘要数、闲聊数、judge 频率等)。

环境变量前缀:``SMARTBUTLER_MEMORY_``
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from smartbutler.config.base import SmartButlerBaseSettings


class MemorySettings(SmartButlerBaseSettings, BaseSettings):
    """记忆子模块配置。

    设计原则:
    1. **配置改动可热生效**(重新读 settings)—— 运行时调度器读 snapshot
    2. **默认值不依赖 .env** —— 直接调 load_memory_settings() 也能跑
    3. **范围限制** —— 防止误填 0 / 负数把 judge 搞挂
    """

    model_config = SettingsConfigDict(
        env_prefix="SMARTBUTLER_MEMORY_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---- 短期窗(task 切窗) ----

    # 注入 prompt 的"已完成 task 摘要"条数(包含 abandoned,不含 chitchat_done)
    # 用户决策 2026-10-11:完成态不能丢,得给 LLM 看"之前做过啥"
    n_completed_tasks: int = Field(
        default=3, ge=0, le=20,
        description="注入 prompt 的完成态 task 摘要条数(包含 abandoned)",
    )

    # 注入 prompt 的"最近闲聊 task 摘要"条数
    # 用户决策 2026-10-11:闲聊也有关系/语气价值,保留固定条数
    n_chitchat_tasks: int = Field(
        default=30, ge=0, le=200,
        description="注入 prompt 的最近闲聊 task 摘要条数",
    )

    # 跨 task 锚点(未完成 paused task 摘要)条数 — 沿用旧值,后续可暴露配置
    n_prior_tasks: int = Field(
        default=3, ge=0, le=20,
        description="注入 prompt 的 paused/未完成 task 摘要条数",
    )

    # ---- TaskScheduler 行为 ----

    # 周期扫描间隔(秒) — 默认 1h,生产可调长,dev 可调短
    judge_interval_seconds: float = Field(
        default=3600.0, gt=0.0,
        description="TaskScheduler 周期扫描间隔(秒)",
    )

    # 🆕 用户决策 2026-10-11:每轮对话是否触发 judge,降低完成态判定延迟
    # True:每次 user ainvoke 调一次 scheduler.trigger_judge() + 频率限制防抖
    # False:仅周期扫描(老行为)
    judge_on_message: bool = Field(
        default=False,
        description="每轮 user 消息是否触发一次 judge(有频率限制)",
    )

    # 频率限制:两次触发最小间隔(秒)
    # 即便 judge_on_message=True,也至少间隔这么久才再次触发
    # 默认 60s:1 分钟内 100 条消息也只触发 1 次
    judge_min_interval_seconds: float = Field(
        default=60.0, ge=0.0,
        description="judge 触发最小间隔(秒),0=不限制",
    )

    # 🆕 Phase 6.5: paused task 生命周期
    # 已被 pause 的 task 在多少轮 user 消息之后自动 complete(避免 paused 无限堆积)
    # <=0 关闭自动 complete(只计数,不入土)
    paused_max_staleness: int = Field(
        default=5, ge=0, le=100,
        description="paused task 超过此轮数自动 complete,<=0 关闭",
    )

    # 🆕 Phase 6.5: judge 触发后延迟 N 秒再执行
    # 期间新消息会重置定时器(保留最后一次未 judge 的消息,避免快速切话题漏掉)
    # 0=老行为(节流不丢);>0=debounce
    on_message_debounce_seconds: float = Field(
        default=0.0, ge=0.0,
        description="on_message debounce 延迟(秒),0=关闭",
    )

    # 🆕 Phase 6.5: 单次 judge LLM 调用超时(秒),超时降级到 fallback(走规则 boundary)
    # 防止 LLM 卡住 30s+ 把 _scan_once 拖死
    judge_timeout_seconds: float = Field(
        default=8.0, ge=0.0,
        description="judge LLM 调用超时(秒),0=不超时",
    )

    # 🆕 Phase 6.5: 喂给 segmenter 的最近消息窗口大小
    # 切归类只需要"最新一条"+"上一条"做对照
    # 给太大 prompt 膨胀+LLM 慢+贵;太小判不出上下文
    judge_window_size: int = Field(
        default=2, ge=1, le=10,
        description="judge 喂给 segmenter 的最近消息条数",
    )

    # ---- Phase 6.3+: 短期→长期自动抽取 (流水线 A) ----

    # 开关:Task 完成/chitchat_done 时是否自动 extract 抽 fact 写 Qdrant
    # False:只靠 /remember 手动;True:自动抽(默认)
    auto_extract_enabled: bool = Field(
        default=True,
        description="Task 完成/chitchat_done 时自动 extract 写 Qdrant",
    )

    # importance 门控:低于该值的 fact 丢弃(避免短闲聊污染长期)
    # 0.0-0.2 琐事;0.2-0.5 事实;0.5-0.8 任务/约定;0.8-1.0 持久偏好
    auto_extract_min_importance: float = Field(
        default=0.3, ge=0.0, le=1.0,
        description="auto-extract 写 Qdrant 的 importance 下限",
    )

    # recall 相似度下限:归一化 raw_norm 低于此值的候选直接丢弃。
    # 没有这个门槛时,库里只剩弱相关条目,弱相关噪声也会被当结果返回
    # 并注入 prompt(实测:"uml 课程报名费用"召回"给管家起名阿福")。
    # 调高 → 更严(宁缺毋滥);调低/设 0 → 关闭(向后兼容)
    #
    # 0.2 的实测依据(2026-10-11,replay v3b):
    #   query="uml 课程报名费用 5000" 的真实归一化分布
    #     正确内容(课程解析)  raw_norm=0.409  → 保留
    #     无关内容("你叫阿福吧") raw_norm=0.118 / 0.114 → 丢掉
    #   → 0.2 落在两者之间,约 2 倍余量。
    #   ⚠️ 0.55(初版默认)会把正确答案一起滤掉,导致 recall 命中 0。
    recall_min_score: float = Field(
        default=0.2, ge=0.0, le=1.0,
        description="recall 相似度下限(归一化 raw_norm),0=关闭",
    )

    # ---- 注入 prompt 的字符预算 ----
    #
    # 🆕 2026-10-12 用户决策:**召回优先于 prompt 预算**。
    #
    # 背景(三轮实测踩坑,facade.py 里也有完整记录):
    #   1. 单条 fact 上限 200 太短 —— 课程 fact 的 `[assistant]` 结论在
    #      第 189 字符,被砍成 `[assista...`,LLM 答"没链接请发我"。
    #   2. 提到 1000 仍不够 —— 宠物 task fact 总长 2678(8 轮),关键事实
    #      "养了几个月,蜕过一次皮,足展 5-6 厘米"在位置 1279,被砍掉,
    #      用户问"我的金直间多大了",LLM 答"没记录"。
    #   3. 试"保首尾砍中间"(head=tail=700),1279 仍在中间,照样丢。
    #
    # 根因:问答式对话信息密度不均匀 —— user 平均 16 字符、assistant 平均
    # 440 字符(科普长文),高价值事实永远是被长文包裹的少数短语,
    # 按字符位置截断必然砍到它们。
    #
    # 所以默认 0 = **不截断**,召回到什么原样注入什么。
    # 将来若要控制 prompt 体积/成本,调大这个值即可(见 facade._truncate_middle)。
    max_fact_chars: int = Field(
        default=0, ge=0, le=100_000,
        description="单条 fact 注入上限(字符),0=不截断(召回优先)",
    )

    # 短库 block 占总预算的比例(0.4 = 短库 40% / 长期 60%)
    # 🆕 当前 recall_prompt_budget_chars=0(不截断)时该值不生效,
    # 保留是为了将来重新引入预算时不用改代码。
    short_term_ratio: float = Field(
        default=0.4, ge=0.0, le=1.0,
        description="短库 block 占总预算比例(仅在启用预算时生效)",
    )

    # ---- Phase 6.3+: 定时 consolidate (流水线 B) ----

    # 是否启动定时 consolidate(daily/weekly/monthly 三个 cron job)
    # 生产 True;dev 默认 True,可 --no-scheduler 关
    scheduler_enabled: bool = Field(
        default=True,
        description="是否启动 APScheduler 自动 consolidate",
    )

    # daily 触发时刻(小时, 0-23) — 默认凌晨 3 点
    consolidate_daily_hour: int = Field(
        default=3, ge=0, le=23,
        description="consolidate(daily) 触发时刻(小时)",
    )

    # weekly 触发星期(0=周一, 6=周日) — 默认周日
    consolidate_weekly_weekday: int = Field(
        default=6, ge=0, le=6,
        description="consolidate(weekly) 触发星期(0=周一,6=周日)",
    )

    # monthly 触发日(1-28) — 默认每月 1 号
    consolidate_monthly_day: int = Field(
        default=1, ge=1, le=28,
        description="consolidate(monthly) 触发日(1-28)",
    )


def load_memory_settings() -> MemorySettings:
    """加载记忆子模块配置。"""
    return MemorySettings()


__all__ = ["MemorySettings", "load_memory_settings"]
