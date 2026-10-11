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
        self._processed_ids: dict[str, set[str]] = {}

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

    async def start(self) -> None:
        if self.is_running:
            logger.warning("task_scheduler.already_running")
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="task-scheduler-loop")
        logger.info(
            "task_scheduler.started",
            interval=self._interval,
            on_message=self._on_message_enabled,
            min_interval=self._min_interval,
        )

    async def stop(self) -> None:
        if not self.is_running:
            return
        self._stop.set()
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
        # 🆕 Phase 6.4 P2: 等所有 fire-and-forget 的 on_message scan 跑完(避免退出会话丢日志)
        if self._inflight_scans:
            pending = list(self._inflight_scans)
            logger.info(
                "task_scheduler.stop awaiting_inflight count=%d", len(pending),
            )
            try:
                await asyncio.wait_for(
                    asyncio.gather(*pending, return_exceptions=True),
                    timeout=10.0,
                )
            except asyncio.TimeoutError:
                logger.warning("task_scheduler.stop inflight_timeout")
                for t in pending:
                    if not t.done():
                        t.cancel()
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
        """扫所有 thread → 喂给 segmenter。"""
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
                        if role == "human":
                            role = "user"
                        if role not in ("user", "assistant"):
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
                    judge_window = (
                        fresh_msgs[-self._judge_window_size:]
                        if len(fresh_msgs) <= self._judge_window_size
                        else fresh_msgs
                    )
                    logger.info(
                        "task_scheduler.segment_call thread=%s total=%d fresh=%d window=%d",
                        tid, len(msgs_view), len(fresh_msgs), len(judge_window),
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
                    # 标记的是 judge_window 里的 id —— 窗口外更早的新消息
                    # 会留给下一轮,不会被这一轮误标。
                    seen_ids.update(m["message_id"] for m in judge_window)
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
