"""🆕 Phase 6.3+: ConsolidationScheduler —— 跑 3 个定时 consolidation job。

3 个 job:
- ``daily``   —— 每天凌晨 3:00 跑 ``consolidate(level="daily")``
- ``weekly``  —— 每周一 3:00 跑 ``consolidate(level="weekly")``(周一是一周之始)
- ``monthly`` —— 每月 1 日 4:00 跑 ``consolidate(level="monthly")``

设计:
- 用 ``apscheduler.AsyncIOScheduler``(与现有 asyncio 循环共存)
- 每个 job 异步调 ``consolidator.consolidate(level=...)``
- 任何 job 抛异常被 apscheduler log 捕获,不阻塞下次
- 启动/停止幂等(可重复调 start/stop)
- 可选:外部停止时 ``wait=False`` 立即返回

不使用 cron 字符串,直接用 ``CronTrigger(hour=X, minute=Y, ...)`` 避免
额外的解析开销。
"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from smartbutler.utils.logging import get_logger

if TYPE_CHECKING:
    from smartbutler.emotion.memory.consolidation import Consolidator

logger = get_logger(__name__)


class ConsolidationScheduler:
    """🆕 Phase 6.3+: 3 个 consolidation 定时 job。

    Args:
        consolidator: 已构造好的 :class:`Consolidator` 实例
        daily_hour: 每天触发小时(0-23,默认 3)
        weekly_weekday: 周触发(0=周一,6=周日,默认 6=周日)
        monthly_day: 月触发日(1-28,默认 1)
        monthly_hour: 月触发小时(0-23,默认 4,跟 daily 错开避免撞)
    """

    def __init__(
        self,
        *,
        consolidator: "Consolidator",
        daily_hour: int = 3,
        weekly_weekday: int = 0,  # 0=周一,6=周日 —— 周一是一周之始,周日是结束,周一 3:00 跑正好汇总完上周
        monthly_day: int = 1,
        monthly_hour: int = 4,
    ) -> None:
        self._consolidator = consolidator
        self._daily_hour = daily_hour
        self._weekly_weekday = weekly_weekday
        self._monthly_day = monthly_day
        self._monthly_hour = monthly_hour
        self._scheduler: AsyncIOScheduler | None = None
        self._run_count: dict[str, int] = {"daily": 0, "weekly": 0, "monthly": 0}

    @property
    def is_running(self) -> bool:
        return self._scheduler is not None and self._scheduler.running

    @property
    def run_count(self) -> dict[str, int]:
        return dict(self._run_count)

    def start(self) -> None:
        """启动 scheduler(幂等)。"""
        if self.is_running:
            logger.debug("consolidation_scheduler.already_running")
            return
        sched = AsyncIOScheduler(timezone="Asia/Shanghai")
        # daily 每天 3:00
        sched.add_job(
            self._run_daily,
            CronTrigger(hour=self._daily_hour, minute=0),
            id="consolidate_daily",
            name="consolidate_daily",
            replace_existing=True,
            misfire_grace_time=3600,
        )
        # weekly 周日 3:00
        sched.add_job(
            self._run_weekly,
            CronTrigger(day_of_week=self._weekly_weekday, hour=self._daily_hour, minute=0),
            id="consolidate_weekly",
            name="consolidate_weekly",
            replace_existing=True,
            misfire_grace_time=3600,
        )
        # monthly 1 号 4:00
        sched.add_job(
            self._run_monthly,
            CronTrigger(day=self._monthly_day, hour=self._monthly_hour, minute=0),
            id="consolidate_monthly",
            name="consolidate_monthly",
            replace_existing=True,
            misfire_grace_time=3600,
        )
        sched.start()
        self._scheduler = sched
        logger.info(
            "consolidation_scheduler.started daily=%s:00 weekly=%s:%s:00 monthly=%s:%s:00",
            self._daily_hour, self._weekly_weekday, self._daily_hour,
            self._monthly_day, self._monthly_hour,
        )

    async def stop(self) -> None:
        """停止 scheduler(幂等)。"""
        if self._scheduler is None:
            return
        try:
            self._scheduler.shutdown(wait=False)
        except Exception as e:  # noqa: BLE001
            logger.warning("consolidation_scheduler.stop_failed: %s", e)
        self._scheduler = None
        logger.info("consolidation_scheduler.stopped")

    # ------------------- 内部方法 -------------------

    async def _run_daily(self) -> None:
        await self._run("daily")

    async def _run_weekly(self) -> None:
        await self._run("weekly")

    async def _run_monthly(self) -> None:
        await self._run("monthly")

    async def _run(self, level: str) -> None:
        """执行一次 consolidation(异常被吞,记日志)。

        Args:
            level: "daily" / "weekly" / "monthly"
        """
        try:
            report = await self._consolidator.consolidate(level=level)
            self._run_count[level] = self._run_count.get(level, 0) + 1
            logger.info(
                "consolidation_scheduler.run_done level=%s scanned=%s finished_at=%s",
                level, report.candidates_scanned, report.finished_at,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("consolidation_scheduler.run_failed level=%s err=%s", level, e)


__all__ = ["ConsolidationScheduler"]
