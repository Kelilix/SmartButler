"""Task 划分调度器(Phase 6.4 P1 + P2 增强)。

按用户决策:
- **触发 1:周期**:1 小时扫一次活跃 thread(后台)
- **触发 2:每轮对话**(Phase 6.4 P2 用户决策 2026-10-11):
  - ``on_message_received()`` 被 orchestrator 在 user ainvoke 后调
  - 频率限制 ``judge_min_interval_seconds``(默认 60s)防爆
  - 开关 ``judge_on_message``(默认 False,默认走老行为,token 友好)
- **不阻塞** user ainvoke
- **降级**:无 LLM 走规则 segmenter

典型用法::

    sched = TaskScheduler(
        short_term=stm, task_index=idx, segmenter=seg,
        interval_seconds=3600,
    )
    await sched.start()
    # ... 进程退出前
    await sched.stop()
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, UTC
from typing import Any

from smartbutler.emotion.memory.task_index import TaskIndex
from smartbutler.emotion.memory.task_segmenter import TaskSegmenter
from smartbutler.emotion.memory.short_term import ShortTermMemory
from smartbutler.utils.logging import get_logger

logger = get_logger(__name__)


class TaskScheduler:
    """周期跑 TaskSegmenter + 每轮触发(可选)。

    Args:
        short_term: :class:`ShortTermMemory`(用来列活跃 thread + 读新消息)
        task_index: :class:`TaskIndex`
        segmenter: :class:`TaskSegmenter`
        interval_seconds: 周期扫描间隔(秒),默认 3600(1 小时)
        on_message_enabled: 🆕 P2,每轮 user 消息是否触发一次 judge
        min_interval_seconds: 🆕 P2,触发最小间隔(防抖)
        on_task_closed: 🆕 Phase 6.3+,可选回调。``segmenter`` 返回
            ``complete``/``chitchat`` 类型 action 时被调用。
            签名: ``def cb(*, task_id: str, category: str, content: str, status: str) -> None``
            通常是包装一下触发 :data:`MemoryEventType.ON_TASK_COMPLETE`。
    """

    def __init__(
        self,
        *,
        short_term: ShortTermMemory,
        task_index: TaskIndex,
        segmenter: TaskSegmenter,
        interval_seconds: float = 3600.0,
        on_message_enabled: bool = False,
        min_interval_seconds: float = 60.0,
        on_task_closed: Any | None = None,
        # 🆕 Phase 6.5: debounce 配置
        # on_message 触发后延迟 N 秒执行,期间再来消息则重置定时器
        # 0=老行为(节流不丢任务);>0=新行为(节流且保留最后一次未 judge 的消息)
        on_message_debounce_seconds: float = 0.0,
        # 🆕 Phase 6.5: paused staleness 配置(透传给 task_index.tick_paused_staleness)
        paused_max_staleness: int = 5,
        # 🆕 Phase 6.5: 喂给 segmenter 的最近消息窗口大小
        # 切归类只需要"最新一条"+"上一条"做对照,2 条足够
        # 给太大 prompt 膨胀+LLM 慢+贵;太小判不出上下文
        judge_window_size: int = 2,
    ) -> None:
        if interval_seconds < 1:
            raise ValueError("interval_seconds must >= 1")
        if min_interval_seconds < 0:
            raise ValueError("min_interval_seconds must >= 0")
        if on_message_debounce_seconds < 0:
            raise ValueError("on_message_debounce_seconds must >= 0")
        if judge_window_size < 1:
            raise ValueError("judge_window_size must >= 1")
        self._short_term = short_term
        self._task_index = task_index
        self._segmenter = segmenter
        self._interval = interval_seconds
        self._on_message_enabled = on_message_enabled
        self._min_interval = min_interval_seconds
        self._on_task_closed = on_task_closed
        self._debounce = on_message_debounce_seconds
        self._paused_max_staleness = paused_max_staleness
        self._judge_window_size = judge_window_size
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._last_run_ts: float = 0.0
        self._last_trigger_ts: float = 0.0
        self._run_count: int = 0
        self._skipped_by_throttle: int = 0
        # 🆕 Phase 6.4 P2: 跟踪 fire-and-forget 的 on_message scan task,保证 stop 时 await
        self._inflight_scans: set[asyncio.Task] = set()
        # 🆕 Phase 6.3+: 已通知的 task_id(防止同次/多次扫描重复触发回调)
        self._notified_task_ids: set[str] = set()
        # 🆕 Phase 6.5: debounce 状态(保留最后一次未执行的"延后"扫描)
        self._debounce_handle: asyncio.TimerHandle | None = None
        self._debounce_pending_thread_id: str | None = None
        # 🆕 Phase 6.5: judge 失败计数(可观测)
        self._judge_failures: int = 0
        # 🐛 修复(2026-10-11):judge 游标 —— 已 judge 过的 message_id(按 thread)。
        #
        # 背景:_scan_once 原本直接把「最后 judge_window_size 条」当成 new_messages
        # 传给 segmenter.segment()。但 segment() 的契约(见其 docstring)是
        # "自上次 judge 以来的新消息"。两者不一致导致:
        #   1. 同一批消息被反复 judge —— 每次都判出 start_new,建出新 task;
        #   2. 窗口里夹带的旧消息又被 task_segmenter 的 one-to-one 规则塞进新 task,
        #      使同一个 message_id 落进多个 task,各自抽出一条 fact
        #      → Qdrant 里出现内容重复的 fact。
        #
        # 实测(2026-10-11, replay v3b):14 轮跑完库里 4 条 fact,
        # 「你叫阿福吧」/「帮我解析下网站…」各出现 2 次,
        # 对应 4 个 task 而非预期的 2 个。
        #
        # 修复:按 thread 维护已 judge 过的 message_id,只把新消息喂给 segmenter。
        # message_id 从 LangGraph message.id 提取,稳定且唯一。
        # 只增不减 —— thread 消息只追加,无需回退。
        # 🆕 Phase 6.5: 初始化为空 dict;在 start() 里从磁盘 load_processed_cursor() 恢复。
        self._processed_ids: dict[str, set[str]] = {}
        # 🔒 修复(2026-10-11):scan 互斥锁 —— 消除并发 judge 导致的重复 task。
        #
        # 背景:``_scan_once`` 是 fire-and-forget(``_fire_scan_now`` 用 create_task),
        # 两次触发若重叠,会同时读到**同一个未推进的游标** ``_processed_ids``,
        # 把同一批消息喂给两次 judge → 每次都判出 ``start_new`` → 建出两个 task,
        # 且同一 ``message_id`` 落进两个 task(各自抽出一条内容重复的 fact)。
        # 实测(replay v3b 手动并发调 scan_now):本该 2 个 task 建出 4 个。
        #
        # 为什么不用同步:单次 judge 实测 5.2s~17s(llm_timeout 默认 8s),
        # 同步化会让每条用户消息都等 5~17s 才出回复,代价远大于收益。
        # 互斥锁能提供**同样的顺序保证**(同一时刻只有一个 scan 在跑,
        # 后一个必然读到前一个已推进的游标),但不阻塞用户响应。
        #
        # 锁必须在 **running loop 内**创建:TaskScheduler 可能在 __init__ 之后
        # 才被拿到 loop,反过来在 __init__ 建锁也不安全(非 loop 线程 + 跨 loop
        # 复用)。懒初始化(``_ensure_scan_lock``)最稳。
        self._scan_lock: asyncio.Lock | None = None
        # 跳过时记一笔,当前 scan 结束后立刻补跑 —— 否则被跳过的消息要等
        # 下一个周期(默认 1 小时!)才被判。用户最后一条消息往往最重要
        #(交代完需求就停),让它滞留 1 小时等于没生效。
        self._scan_pending: bool = False
        # 观测:被互斥跳过的次数
        self._skipped_by_lock: int = 0
        # stop 闸门:stop() 期间禁止 fire 补跑 scan(否则逃出 drain 窗口)
        self._stopping: bool = False

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def last_run_ts(self) -> float:
        return self._last_run_ts

    @property
    def last_trigger_ts(self) -> float:
        return self._last_trigger_ts

    @property
    def run_count(self) -> int:
        return self._run_count

    @property
    def skipped_by_throttle(self) -> int:
        """被频率限制吃掉的触发次数(可观测性)。"""
        return self._skipped_by_throttle

    @property
    def judge_failures(self) -> int:
        """🆕 Phase 6.5: judge LLM 失败/超时累计(可观测)。"""
        return self._judge_failures

    @property
    def skipped_by_lock(self) -> int:
        """🔒 被 scan 互斥锁跳过的次数(可观测)。

        正常情况下应该很小:每个被跳过的触发都会在当前 scan 结束后补跑一次。
        如果持续增长,说明单次 judge 太慢(> 消息间隔),补跑也追不上。
        """
        return self._skipped_by_lock

    def _ensure_scan_lock(self) -> asyncio.Lock:
        """懒创建 scan 锁(必须在 running loop 内调用)。"""
        if self._scan_lock is None:
            self._scan_lock = asyncio.Lock()
        return self._scan_lock

    async def start(self) -> None:
        if self.is_running:
            logger.warning("task_scheduler.already_running")
            return
        self._stopping = False
        self._stop.clear()
        # 🆕 Phase 6.5: 从磁盘加载 judge 游标(解决 dev_chat 重启后旧 thread 被重复 judge 的 bug)。
        # _processed_ids 持久化到 task_index.db,进程启动时读回。
        # 这样 dev_chat 重启后,之前已 judge 过的 message_id 仍然有效,
        # 旧 thread 的旧消息不会被重新喂给 segmenter。
        self._processed_ids = self._task_index.load_processed_cursor()
        logger.info(
            "task_scheduler.started (cursor loaded threads=%d)",
            len(self._processed_ids),
            interval=self._interval,
            on_message=self._on_message_enabled,
            min_interval=self._min_interval,
        )
        self._task = asyncio.create_task(self._loop(), name="task-scheduler-loop")

    async def stop(self) -> None:
        # 🔒 先置"停止中"闸门:**在等任何东西之前**。
        # 否则 drain 期间持锁方退出临界区会再 fire 一个补跑 task ——
        # 那个 task 不在 drain 开始时的快照里,逃出等待,又在新一轮 gather
        # 之后才置 pending → stop 返回时 _scan_pending 仍为 True,
        # 会话已经结束,尾消息永远没被补判。
        self._stopping = True
        self._stop.set()
        if not self.is_running and not self._inflight_scans:
            self._stopping = False
            return
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()
        self._task = None
        # 🆕 Phase 6.5: 取消未触发的 debounce 定时器
        if self._debounce_handle is not None:
            self._debounce_handle.cancel()
            self._debounce_handle = None
            self._debounce_pending_thread_id = None
            logger.info("task_scheduler.stop debounce_cancelled")

        # 🆕 Phase 6.5: stop 前最后一次保存游标(确保优雅退出时不丢进度)
        try:
            self._task_index.save_processed_cursor(self._processed_ids)
            logger.info("task_scheduler.stop cursor_saved")
        except Exception as e:
            logger.warning("task_scheduler.stop.save_cursor_failed: %s", e)

        # 🆕 Phase 6.4 P2: 等所有 fire-and-forget 的 on_message scan 跑完(避免退出会话丢日志)
        #
        # 🔒 2026-10-11: 改成**循环 drain**。原实现只等一次快照,但 scan 持锁
        # 期间被跳过的触发会置 ``_scan_pending``,由持锁方在**退出临界区后**
        # 才 fire 补跑 —— 那个补跑 task 在上面的 gather 之后才被创建,
        # 单次快照会漏掉它,导致最后的补判丢失(会话结束 → 尾消息永远没进记忆)。
        # 循环条件用 ``_scan_pending`` 而非"集合非空":锁是懒建的,且补跑 task
        # 入集合发生在 fire 的同一同步段,所以集合判空有微小竞态窗口。
        deadline = time.monotonic() + 15.0
        while True:
            pending = list(self._inflight_scans)
            if not pending and not self._scan_pending:
                break
            if time.monotonic() > deadline:
                logger.warning(
                    "task_scheduler.stop inflight_drain_timeout pending=%d flag=%s",
                    len(pending), self._scan_pending,
                )
                for t in pending:
                    if not t.done():
                        t.cancel()
                break
            logger.info("task_scheduler.stop draining_inflight count=%d", len(pending))
            await asyncio.wait_for(
                asyncio.gather(*pending, return_exceptions=True),
                timeout=5.0,
            )
        self._stopping = False
        logger.info("task_scheduler.stopped")

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self._scan_once()
            except Exception as e:  # noqa: BLE001
                logger.warning("task_scheduler.scan_failed: %s", e)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
            except asyncio.TimeoutError:
                continue
            else:
                break

    async def scan_now(self) -> dict[str, Any]:
        """手动触发一次扫描(测试用)。"""
        return await self._scan_once()

    async def on_message_received(self, *, thread_id: str | None = None) -> dict[str, Any] | None:
        """🆕 Phase 6.4 P2: 每轮 user ainvoke 后调用(可关闭)。

        行为(Phase 6.5 升级):
        1. 开关关 → 直接返 None(零开销)
        2. **先 tick paused staleness** —— 把所有 paused task 的 turn_count +1,
           超过阈值的自动 complete。无视任何开关,user msg 进来就该算账。
        3. 然后判断 judge 触发:
           - ``on_message_debounce_seconds == 0`` → 老行为(频率限制 + fire-and-forget)
           - ``on_message_debounce_seconds > 0`` → 新行为(debounce):
             * 距上次触发 < ``min_interval_seconds`` → 节流
             * 否则排一个 N 秒后执行的定时器;期间再来消息则重置定时器
             * 定时器到点 → fire-and-forget 跑 ``_scan_once``
             * 保留最后一次未 judge 的 thread_id(就算用不到,日志也要有)

        Args:
            thread_id: 可选,指定 thread 时只扫该 thread(优化);None = 扫全部。

        Returns:
            跳过时返 ``{"skipped": "disabled" / "throttle"}``;
            接受触发时返 ``None``(实际结果在后台 task 完成时通过 logger 报)。
        """
        # 🆕 Phase 6.5: 任何 user 消息都触发 tick(staleness 累计)
        # 这步与 judge 开关解耦 —— 即便 on_message=False,paused task 也得老去
        if self._paused_max_staleness > 0:
            try:
                completed = self._task_index.tick_paused_staleness(
                    max_staleness=self._paused_max_staleness,
                )
                if completed:
                    logger.info(
                        "task_scheduler.tick_paused_staleness auto_completed=%d",
                        len(completed),
                    )
                    # auto-complete 触发 on_task_closed 回调(若有)
                    for tid in completed:
                        try:
                            self._notify_auto_completed(tid)
                        except Exception:  # noqa: BLE001
                            pass
            except Exception as e:  # noqa: BLE001
                logger.warning("task_scheduler.tick_paused_staleness_failed: %s", e)

        if not self._on_message_enabled:
            return {"skipped": "disabled"}
        now = time.monotonic()
        if self._min_interval > 0 and (now - self._last_trigger_ts) < self._min_interval:
            self._skipped_by_throttle += 1
            logger.info(
                "task_scheduler.on_message throttled skipped_count=%d",
                self._skipped_by_throttle,
            )
            return {"skipped": "throttle"}

        # 🆕 Phase 6.5: debounce 分支
        if self._debounce > 0:
            self._schedule_debounced_scan(thread_id=thread_id)
            return None

        # 老行为:立即 fire-and-forget
        self._last_trigger_ts = now
        logger.info("task_scheduler.on_message fire thread_id=%s", thread_id)
        self._fire_scan_now()
        return None

    def _fire_scan_now(self) -> None:
        """🆕 Phase 6.5: fire-and-forget 跑 _scan_once,提取出来给立即/延后两条路径复用。"""
        try:
            loop = asyncio.get_running_loop()
            task = loop.create_task(self._scan_once(), name="task-scheduler-on-message")
            self._inflight_scans.add(task)
            task.add_done_callback(self._inflight_scans.discard)
        except RuntimeError:
            # 没有 running loop(测试场景),降级为同步等待
            pass

    def _schedule_debounced_scan(self, *, thread_id: str | None) -> None:
        """🆕 Phase 6.5: 排一个 debounce 定时器,期间反复调用会被重置。"""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # 没有 running loop → 退化到 fire-now
            self._fire_scan_now()
            return
        # 取消之前的待执行(保留最后一次)
        if self._debounce_handle is not None:
            self._debounce_handle.cancel()
        self._debounce_pending_thread_id = thread_id
        self._debounce_handle = loop.call_later(
            self._debounce,
            self._on_debounce_fire,
        )
        logger.info(
            "task_scheduler.on_message debounced thread_id=%s delay=%.2fs",
            thread_id, self._debounce,
        )

    def _on_debounce_fire(self) -> None:
        """🆕 Phase 6.5: 定时器到点,实际 fire 扫描。"""
        tid = self._debounce_pending_thread_id
        self._debounce_handle = None
        self._debounce_pending_thread_id = None
        self._last_trigger_ts = time.monotonic()
        logger.info("task_scheduler.debounce_fire thread_id=%s", tid)
        self._fire_scan_now()

    def _notify_auto_completed(self, task_id: str) -> None:
        """🆕 Phase 6.5: tick 出来的 auto-completed 任务也走 on_task_closed 回调。"""
        if not self._on_task_closed:
            return
        if task_id in self._notified_task_ids:
            return
        # 拿一次 task 元数据(回调签名需要 category/content)
        rec = self._task_index.get_task(task_id)
        if rec is None:
            return
        try:
            self._on_task_closed(
                task_id=rec.task_id,
                category=rec.category,
                content=rec.title,
                status="completed",
                thread_id=rec.source_thread,
            )
            self._notified_task_ids.add(rec.task_id)
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "task_scheduler.notify_auto_completed_failed task_id=%s err=%s",
                task_id, e,
            )

    async def _scan_once(self) -> dict[str, Any]:
        """扫所有 thread → 喂给 segmenter(**带互斥**)。

        🔒 并发保护(2026-10-11):
        - 已有 scan 在跑 → **不排队**,只置 ``_scan_pending`` 并立即返回。
          排队会积压:judge 单次 5~17s,连发 5 条消息 = 排队 25~85s 才轮到最后一条。
          正确做法是跳过本轮,由当前 scan 的 finally 补跑一次(那一次会把
          期间累积的所有消息一次性喂给 judge)。
        - 当前 scan 结束 → 若有 pending,fire-and-forget 补跑。

        ``_processed_ids`` 游标的读写全部落在临界区内,所以后一个 scan
        必然读到前一个已推进的游标 —— 顺序保证 == 同步化的效果,但不阻塞用户。
        """
        lock = self._ensure_scan_lock()
        if lock.locked():
            self._scan_pending = True
            self._skipped_by_lock += 1
            logger.info(
                "task_scheduler.scan_skipped_inflight pending_count=%d",
                self._skipped_by_lock,
            )
            return {"skipped": "scan_in_flight"}
        try:
            async with lock:
                return await self._scan_inner()
        finally:
            # ⚠️ 在锁**外**决定是否补跑。若在 `async with lock` 内部 fire,
            # 补跑的 scan 是在当前协程让出控制权后才执行的,那时锁已释放 ——
            # 能跑通,但正确性依赖 asyncio 的调度时序(哪天 create_task 改成
            # 立即执行就会撞锁→ 又被跳过 → 白等一个周期)。放锁外是显式的。
            if self._scan_pending:
                self._scan_pending = False
                if self._stopping:
                    # stop 期间不再起新 task —— 起���的逃出 stop 的 drain 窗口,
                    # 会在 stop 返回后才跑,导致尾消息丢失。
                    # 清掉标记(不留给下一轮):会话都要结束了,没有下一轮。
                    logger.info("task_scheduler.rescan_suppressed_during_stop")
                else:
                    logger.info("task_scheduler.scan_pending_rescan_fired")
                    self._fire_scan_now()

    async def _scan_inner(self) -> dict[str, Any]:
        """扫所有 thread → 喂给 segmenter。

        🔒 调用方**必须**已持有 scan 锁(见 :meth:`_scan_once`)——
        游标 ``_processed_ids`` 的读写与 judge 必须在同一临界区内,
        否则并发 scan 会读到同一份旧游标,重复建 task。
        """
        t0 = time.monotonic()
        self._last_run_ts = time.time()
        self._run_count += 1
        logger.info("task_scheduler.scan_start run_count=%d", self._run_count)
        try:
            saver = await self._short_term.get_async_saver()
        except Exception as e:  # noqa: BLE001
            logger.warning("task_scheduler.saver_failed: %s", e)
            return {"scanned": 0, "actions": 0, "duration_ms": 0, "error": str(e)}

        # 列活跃 thread
        # 🐛 修复:saver.alist() 返回 AsyncIterator[CheckpointTuple](每个 tuple 是 1 个 checkpoint),
        # 同一 thread 可能有多个历史 checkpoint,直接 append 会重复处理。
        # 修复:用 seen_tids 去重,只保留每个 thread 的最新一次。
        scanned = 0
        actions_total = 0
        try:
            threads_iter = saver.alist(config=None, limit=500)
            seen_tids: set[str] = set()
            threads: list[str] = []
            async for cp in threads_iter:
                cfg = getattr(cp, "config", None) or {}
                configurable = cfg.get("configurable", {}) if isinstance(cfg, dict) else {}
                tid = configurable.get("thread_id", "")
                if tid and tid not in seen_tids:
                    seen_tids.add(tid)
                    threads.append(tid)
        except Exception as e:  # noqa: BLE001
            logger.warning("task_scheduler.alist_failed: %s", e)
            threads = []

        # 🐛 修复:即使去重后还有 N 个 thread,LLM judge 也要并发限流,
        # 否则 10 个 thread × 1 次 LLM judge × 5s = 50s,会卡住主协程 ainvoke 写 saver。
        # 用 semaphore 限制并发数(默认 3),保证 _scan_once 尽快结束。
        sem = asyncio.Semaphore(3)

        async def _process_one(tid: str) -> int:
            async with sem:
                try:
                    config = {"configurable": {"thread_id": tid}}
                    state = await saver.aget(config)
                    if state is None:
                        return 0
                    if isinstance(state, dict):
                        cv = state.get("channel_values", {})
                    else:
                        cv = getattr(state, "values", {}) or {}
                    msgs_raw = cv.get("messages", []) if isinstance(cv, dict) else []
                    if not msgs_raw:
                        return 0
                    # 转 view 形态
                    msgs_view: list[dict[str, Any]] = []
                    for m in msgs_raw:
                        role_obj = getattr(m, "role", None) or getattr(m, "type", "")
                        role = getattr(role_obj, "value", role_obj) or ""
                        # 🐛 修复(2026-10-12):LangGraph 里 role 的实际取值是
                        # human / ai / tool(实测 checkpoint 分布),不是 user/assistant。
                        # 原代码只映射 human→user,导致 ai / tool 被下面的白名单
                        # 直接 continue 掉 → judge 窗口里从来没有 assistant 消息,
                        # task_messages 表只落 user 行(实测 14 条全 user),
                        # auto_extract 抽出的 fact 丢失管家回复上下文。
                        role = {
                            "human": "user",
                            "ai": "assistant",
                        }.get(str(role), str(role))
                        if role not in ("user", "assistant", "tool"):
                            continue
                        add_kwargs = getattr(m, "additional_kwargs", None) or {}
                        mid = getattr(m, "id", None) or f"{tid}:{len(msgs_view)}"
                        msgs_view.append({
                            "role": role,
                            "content": getattr(m, "content", "") or "",
                            "user_id": add_kwargs.get("user_id"),
                            "spoken_at": add_kwargs.get("spoken_at"),
                            "message_id": mid,
                        })
                    if not msgs_view:
                        return 0
                    # 🐛 修复:只把"未见过的新消息"喂给 segmenter,而不是"最后 N 条"。
                    # 详见 self._processed_ids 处的说明(重复 task/重复 fact 的根因)。
                    seen_ids = self._processed_ids.setdefault(tid, set())
                    fresh_msgs = [
                        m for m in msgs_view if m["message_id"] not in seen_ids
                    ]
                    if not fresh_msgs:
                        logger.info(
                            "task_scheduler.no_new_messages thread=%s total=%d",
                            tid, len(msgs_view),
                        )
                        return 0
                    # 窗口:给 judge 的上下文上限(默认最后 2 条),
                    # 但**至少包含全部新消息** —— 新消息多于窗口时全给,
                    # 避免又出现"新消息被截掉、只剩旧消息"的错位。
                    # 🆕 judge 只看 user 消息,assistant 由写入侧按位置跟随。
                    # 原因:judge 的 prompt / 硬规则依赖"最新一/两条"做切分,
                    # 若窗口里夹杂 assistant 回复,"最新一条"经常是 AI 的长文,
                    # 导致 judge 判断基准偏移,在错误时点开 task。
                    # 实测(2026-10-12):加入 assistant 后 prompt 行数 4→7,
                    # judge 对"你是谁"的分类从"chitchat"变成"web_parse 的一部分",
                    # task 划分完全错乱。
                    # 正确设计:judge 只看 user 序列("你是谁|你叫阿福吧|帮我解析…"),
                    # assistant 按完整对话流位置跟随——"我是智能助手"归属"你是谁",
                    # "已解析"归属"帮我解析网站"。
                    fresh_user = [m for m in fresh_msgs if m["role"] == "user"]
                    # 窗口:给 judge 的上下文上限(默认最后 2 条),
                    # 但**至少包含全部新消息** —— 新消息多于窗口时全给,
                    # 避免又出现"新消息被截掉、只剩旧消息"的错位。
                    judge_window = (
                        fresh_user[-self._judge_window_size:]
                        if len(fresh_user) <= self._judge_window_size
                        else fresh_user
                    )
                    logger.info(
                        "task_scheduler.segment_call thread=%s total=%d fresh=%d "
                        "user_fresh=%d window=%d",
                        tid, len(msgs_view), len(fresh_msgs),
                        len(fresh_user), len(judge_window),
                    )
                    last_user = next(
                        (m for m in reversed(judge_window) if m["role"] == "user"),
                        None,
                    )
                    seg_user = (last_user or {}).get("user_id")
                    # 🆕 Phase 6.5: 不在外层包 wait_for —— 让 segmenter._invoke_llm 内部
                    # 走 timeout → fallback_segment(规则边界),保留 task 落地能力
                    # 老的外层 timeout 直接 return 0 → 所有 task 都没了,是 bug
                    try:
                        result = await self._segmenter.segment(  # type: ignore[attr-defined]
                            thread_id=tid,
                            new_messages=judge_window,
                            all_messages=fresh_msgs,
                            user_id=seg_user,
                        )
                    except Exception as e:  # noqa: BLE001
                        self._judge_failures += 1
                        logger.warning(
                            "task_scheduler.segment_failed tid=%s err=%s", tid, e,
                        )
                        return 0
                    # 游标推进:只在本轮 judge **成功之后**才标记为已处理。
                    # judge 失败(fallback 抛异常/超时)时保留未标记,
                    # 下一轮 scan 会重试这批消息,避免"判失败就永久丢消息"。
                    # 游标推进:用 fresh_msgs(全量),不是 user-only 的 judge_window。
                    # assistant 也要推进游标,否则下一轮会重复处理,insert 时
                    # INSERT OR IGNORE 不报错但 message_id 冲突,assistant 丢消息。
                    seen_ids.update(m["message_id"] for m in fresh_msgs)
                    # 通知 close 类 action
                    self._notify_closed_actions(tid, result.actions)
                    return len(result.actions)
                except Exception as e:  # noqa: BLE001
                    self._judge_failures += 1
                    logger.warning("task_scheduler.thread_failed tid=%s err=%s", tid, e)
                    return 0

        # 并发跑(限流 3 个)
        results = await asyncio.gather(
            *[_process_one(tid) for tid in threads],
            return_exceptions=False,
        )
        scanned = len(threads)
        actions_total = sum(results)
        duration_ms = int((time.monotonic() - t0) * 1000)
        logger.info(
            "task_scheduler.scan_done",
            scanned=scanned, actions=actions_total, duration_ms=duration_ms,
            ts=datetime.now(UTC).isoformat(),
        )
        # 🆕 Phase 6.5: 扫描结束后把游标写回磁盘。
        # 这样 dev_chat 重启后能恢复上次的进度。
        # 代价:每轮 scan 多一次 SQLite 写,约等于毫秒级,可接受。
        # 进程 crash 时最多丢失最近一轮的游标(重判代价可接受)。
        try:
            self._task_index.save_processed_cursor(self._processed_ids)
        except Exception as e:
            logger.warning("task_scheduler.save_cursor_failed: %s", e)

        return {
            "scanned": scanned,
            "actions": actions_total,
            "duration_ms": duration_ms,
        }

    def _notify_closed_actions(
        self, thread_id: str, actions: list[Any]
    ) -> None:
        """🆕 Phase 6.3+: 对 close 类 action(complete/chitchat)调 ``on_task_closed`` 回调。

        - 已通知过的 task_id 不重复触发(``_notified_task_ids``)。
        - 回调抛异常被吞掉(warn)。
        """
        if not self._on_task_closed:
            return
        for a in actions:
            atype = getattr(a, "type", "")
            # 只关心完成/闲聊创建(都是事实可生成的状态)
            if atype not in ("complete", "chitchat"):
                continue
            task_id = getattr(a, "task_id", None) or f"{thread_id}:{atype}"
            if not task_id or task_id in self._notified_task_ids:
                continue
            status = "completed" if atype == "complete" else "chitchat_done"
            category = getattr(a, "category", None) or (
                "chitchat" if atype == "chitchat" else "other"
            )
            content = getattr(a, "title", "") or ""
            try:
                self._on_task_closed(
                    task_id=task_id,
                    category=category,
                    content=content,
                    status=status,
                    thread_id=thread_id,
                )
                self._notified_task_ids.add(task_id)
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "task_scheduler.on_task_closed_failed task_id=%s err=%s",
                    task_id, e,
                )


__all__ = ["TaskScheduler"]
