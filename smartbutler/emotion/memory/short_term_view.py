"""短期记忆视图(Phase 6.3 P2 + Phase 6.4 P1)。

按文档 §2.5.1.2 实施 "task 边界 + 滑窗" 短期切分。

设计原则(2026-10-10):
1. **view 不写 SqliteSaver** —— SqliteSaver 照常存所有 thread,本模块只读不写
2. **task 边界双轨** —— 老路径用 :class:`TaskBoundaryDetector` 实时算规则边界;
   新路径从 :class:`TaskIndex` 读 LLM judge 的结果(默认路径)
3. **完整保留当前 task 全部对话** —— 不砍中间步骤
4. **跨 task 只带"上一 task 最终答复 + 关键事实"摘要锚点** —— 避免 context 爆炸
5. **不阻塞 caller** —— 任何 I/O 失败快速降级返空串

模块边界:
- :class:`ShortTermView`: 给 facade 用的"读视图",负责:列 thread / 读 messages / 按 task 切窗
- :class:`TaskWindow`: 切窗结果(当前 task 完整 + 上 N 个 task 摘要)
- :func:`build_short_term_prompt_block`: 把 :class:`TaskWindow` 格式化成可注入 system prompt 的 markdown

Phase 6.4 P1 升级(用户决策):
- **完成态 task 不再注入 prompt**(completed/abandoned/chitchat_done 不进)
- **当前 task 完整保留,不加截断**(用户最终决定:不设上限)
- **注入字段含 who/when**:prompt 里每条带 `[22:00 男主人] 内容`
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, UTC
from typing import Any

from smartbutler.emotion.memory.task_boundary import (
    BoundaryResult,
    TaskBoundaryDetector,
    TurnInfo,
)
from smartbutler.emotion.memory.task_index import TaskIndex
from smartbutler.utils.logging import get_logger

logger = get_logger(__name__)


@dataclass
class ThreadSummary:
    """单 thread 的最终状态摘要(view 层算,不持久化)。"""

    thread_id: str
    user_id: str                  # 从 state.values 读出(短期库本身不存 user 维度)
    last_user_message: str        # 最后一条 user 发言
    last_assistant_message: str   # 最后一条 assistant 发言
    last_timestamp: datetime | None
    total_turns: int              # 消息数(过滤 system 后)
    last_task_summary: str = ""   # 上一个 task 的摘要(由 view 算)


@dataclass
class TaskWindow:
    """滑窗结果(给 facade 注入 prompt 用)。"""

    current_task_messages: list[dict[str, Any]] = field(default_factory=list)
    """当前 task 完整对话 — 每条:{"role", "content", "timestamp", "user_id"}"""

    prior_task_summaries: list[ThreadSummary] = field(default_factory=list)
    """上 N 个 task 的最终答复摘要(跨 task 锚点)"""

    unfinished_task_messages: list[dict[str, Any]] = field(default_factory=list)
    """未完成但非当前的 task(paused)—— 注入简短锚点,不在老路径上。"""

    # 🆕 Phase 6.4 P2: 完成态摘要(用户决策 2026-10-11)
    completed_task_summaries: list[ThreadSummary] = field(default_factory=list)
    """最近 N 个 completed/abandoned task 的最终答复摘要(给 LLM 看"之前做过啥")"""

    chitchat_task_summaries: list[ThreadSummary] = field(default_factory=list)
    """最近 N 个 chitchat_done task 摘要(闲聊上下文)"""

    boundary_reason: str = "none"
    """当前 task 切出来的原因(interval/butler_done/trigger/none/llm_judge)"""

    total_threads_scanned: int = 0
    """扫了多少 thread(只读统计,不入 prompt)"""

    source: str = "raw_saver"
    """窗口来源:raw_saver / task_index(供测试/审计)"""


class ShortTermView:
    """短期记忆只读视图(Phase 6.3 P2)。

    不持 SqliteSaver 句柄 —— 每次调用由 caller 注入(让 facade 复用已有 connection)。

    典型用法::

        saver = await short_term.get_async_saver()
        view = ShortTermView(saver=saver, detector=TaskBoundaryDetector())
        window = await view.build_window(
            current_session_id=session_id,
            current_user_input=user_input,
            n_prior_tasks=3,
        )
        block = build_short_term_prompt_block(window)
    """

    def __init__(
        self,
        *,
        saver: Any,
        detector: TaskBoundaryDetector | None = None,
        task_index: TaskIndex | None = None,
        n_completed_tasks: int = 3,
        n_chitchat_tasks: int = 30,
        n_prior_tasks: int = 3,
    ) -> None:
        """构造视图。

        Args:
            saver: ``AsyncSqliteSaver`` 实例(短期库)。
            detector: task 边界检测器,默认 30 分钟阈值。
            task_index: 🆕 Phase 6.4 P1,LLM judge 写出的 task 索引。
                注入后,``build_window`` 走新路径(按 task 池切窗)。
                None → 走老路径(实时算规则边界 + 扫所有 thread)。
            n_completed_tasks: 🆕 Phase 6.4 P2,完成态 task 摘要条数
                (completed + abandoned,不含 chitchat_done)。
            n_chitchat_tasks: 🆕 Phase 6.4 P2,闲聊 task 摘要条数。
            n_prior_tasks: paused/未完成 task 摘要条数。
        """
        self._saver = saver
        self._detector = detector or TaskBoundaryDetector()
        self._task_index = task_index
        self._n_completed = max(0, n_completed_tasks)
        self._n_chitchat = max(0, n_chitchat_tasks)
        self._n_prior = max(0, n_prior_tasks)

    async def list_threads(
        self,
        *,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """列所有 thread(短期库 metadata)。

        Args:
            limit: 最多返回多少 thread。

        Returns:
            [{"thread_id": ..., "checkpoint_id": ..., "ts": ...}, ...]
        """
        try:
            out: list[dict[str, Any]] = []
            # alist 接受 config 过滤,无 config = 全部 thread
            async for cp in self._saver.alist(
                config=None, limit=limit,
            ):
                cfg = cp.config or {}
                configurable = cfg.get("configurable", {}) or {}
                thread_id = configurable.get("thread_id", "")
                if not thread_id:
                    continue
                # CheckpointTuple 是 NamedTuple,字段在 .checkpoint dict 里
                # checkpoint["id"] = str(checkpoint_id), checkpoint["ts"] = timestamp
                cp_dict = cp.checkpoint if hasattr(cp, "checkpoint") else {}
                if not isinstance(cp_dict, dict):
                    cp_dict = {}
                cp_id = cp_dict.get("id", "")
                cp_ts = cp_dict.get("ts", "")
                out.append({
                    "thread_id": thread_id,
                    "checkpoint_id": cp_id,
                    "ts": cp_ts,
                })
            return out
        except Exception as e:  # noqa: BLE001
            logger.warning("short_term_view.list_threads failed: %s", e)
            return []

    async def read_thread_messages(
        self,
        *,
        thread_id: str,
    ) -> list[dict[str, Any]]:
        """读指定 thread 全部 message(转成简化 dict,不返回 vector)。

        Args:
            thread_id: LangGraph thread_id(就是 session_id)。

        Returns:
            [{"role": "user"/"assistant", "content": ..., "timestamp": ...}, ...]
        """
        try:
            config = {"configurable": {"thread_id": thread_id}}
            state = await self._saver.aget(config)
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "short_term_view.read_thread_messages failed thread=%s err=%s",
                thread_id, e,
            )
            return []
        if state is None:
            return []
        # saver.aget 在 langgraph-checkpoint-sqlite 里返 dict,
        # 结构是 {"channel_values": {"messages": [...]}, "ts": ..., "id": ...}
        # 兼容两种:dict 形式 和 StateSnapshot 对象形式
        if isinstance(state, dict):
            state_dict = state
        else:
            state_dict = state.values if hasattr(state, "values") and not callable(state.values) else {}
        if not state_dict:
            return []
        # 拿 messages:优先 channel_values,直接 state_dict 也兜底
        if "channel_values" in state_dict and isinstance(state_dict["channel_values"], dict):
            state_values = state_dict["channel_values"]
        else:
            state_values = state_dict
        try:
            messages = state_values.get("messages", []) or []
        except Exception:  # noqa: BLE001
            return []

        out: list[dict[str, Any]] = []
        for m in messages:
            try:
                # BaseMessage:role (str) + content + additional_kwargs
                # dict:role + content
                if isinstance(m, dict):
                    role = m.get("role", m.get("type", ""))
                    content = m.get("content", "")
                    add_kwargs = m.get("additional_kwargs") or {}
                else:
                    role_obj = getattr(m, "role", None) or getattr(m, "type", "")
                    role = getattr(role_obj, "value", role_obj) or ""
                    content = getattr(m, "content", "") or ""
                    add_kwargs = getattr(m, "additional_kwargs", None) or {}
                # 跳过 system/ToolMessage
                if not role or role not in ("user", "assistant", "human"):
                    continue
                if role == "human":
                    role = "user"
                # 🆕 Phase 6.4 P1: per-message 维度 who/when
                # 优先用 additional_kwargs 里的 spoken_at;其次 BaseMessage.id
                # 都拿不到 → None(不强行用 checkpoint.ts,因为那是按 turn 写的)
                per_msg_user_id = add_kwargs.get("user_id") or ""
                per_msg_ts = add_kwargs.get("spoken_at") or ""
                out.append({
                    "role": role,
                    "content": str(content),
                    "timestamp": per_msg_ts or None,
                    "user_id": per_msg_user_id or None,
                })
            except Exception:  # noqa: BLE001
                continue
        return out

    async def build_window(
        self,
        *,
        current_session_id: str,
        current_user_input: str,
        n_prior_tasks: int | None = None,
        n_completed_tasks: int | None = None,
        n_chitchat_tasks: int | None = None,
        thread_limit: int = 100,
        user_id: str | None = None,
    ) -> TaskWindow:
        """构建当前 task 的"完整对话 + 上 N 个 task 摘要"窗口。

        Args:
            current_session_id: 本次 ainvoke 的 session_id(thread_id)。
            current_user_input: 本次 user 输入(用于算 task boundary)。
            n_prior_tasks: 跨 task 锚点(paused)取几个,None=用构造参数(默认 3)。
            n_completed_tasks: 🆕 Phase 6.4 P2,完成态摘要条数,None=用构造参数。
            n_chitchat_tasks: 🆕 Phase 6.4 P2,闲聊摘要条数,None=用构造参数。
            thread_limit: 扫描 thread 上限(防止爆库)。
            user_id: 🆕 Phase 6.4 P1,本轮 user,用于 task_index 过滤。

        Returns:
            :class:`TaskWindow`(任何失败都返回空 TaskWindow,不抛)。
        """
        logger.info(
            "short_term_view.build_window start session=%s user=%s query=%r",
            current_session_id, user_id, current_user_input[:40],
        )
        # 优先用 call-time 覆盖构造参数
        n_prior = self._n_prior if n_prior_tasks is None else max(0, n_prior_tasks)
        n_completed = self._n_completed if n_completed_tasks is None else max(0, n_completed_tasks)
        n_chitchat = self._n_chitchat if n_chitchat_tasks is None else max(0, n_chitchat_tasks)

        window = TaskWindow()
        try:
            # 🆕 Phase 6.4 P1: 优先走 task_index 路径
            if self._task_index is not None:
                return await self._build_window_from_index(
                    current_session_id=current_session_id,
                    current_user_input=current_user_input,
                    n_prior_tasks=n_prior,
                    n_completed_tasks=n_completed,
                    n_chitchat_tasks=n_chitchat,
                    user_id=user_id,
                )
            # 老路径:直接读 SqliteSaver + 规则 boundary
            return await self._build_window_from_saver(
                current_session_id=current_session_id,
                current_user_input=current_user_input,
                n_prior_tasks=n_prior,
                thread_limit=thread_limit,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("short_term_view.build_window failed: %s", e)
            return window

    # ---- Phase 6.4 P1: 新路径(task_index) ----

    async def _build_window_from_index(
        self,
        *,
        current_session_id: str,
        current_user_input: str,
        n_prior_tasks: int,
        n_completed_tasks: int,
        n_chitchat_tasks: int,
        user_id: str | None,
    ) -> TaskWindow:
        """从 task_index 切窗。

        逻辑(Phase 6.4 P2):
        1. 查 user 的 active task(ongoing + paused)
        2. 找"本 thread 最近的 ongoing task"作 current_task
        3. 其他 paused task 给短锚点(prior_task_summaries)
        4. **completed/abandoned** → completed_task_summaries(最近 N 个)
        5. **chitchat_done** → chitchat_task_summaries(最近 N 个)
        """
        window = TaskWindow(source="task_index")
        active = self._task_index.list_active_tasks(user_id=user_id)  # type: ignore[union-attr]
        if not active:
            window.boundary_reason = "no_active"
        else:
            # 找本 thread 相关的 ongoing task
            current_task = next(
                (t for t in active
                 if t.status == "ongoing" and t.source_thread == current_session_id),
                None,
            )
            # 兜底:本 thread 没有 ongoing → 用 user 维度的第一个 ongoing
            if current_task is None:
                current_task = next((t for t in active if t.status == "ongoing"), None)

            # 拼 current_task_messages(从 task_messages 表)
            if current_task is not None:
                for m in self._task_index.iter_task_messages(current_task.task_id):  # type: ignore[union-attr]
                    window.current_task_messages.append({
                        "role": m.role,
                        "content": m.content,
                        "timestamp": m.spoken_at,
                        "user_id": m.user_id,
                    })
                window.boundary_reason = "llm_judge" if current_task.source_thread == current_session_id else "fallback_thread"

            # paused task 简短锚点
            paused = [t for t in active if t.status == "paused" and t.task_id != (current_task.task_id if current_task else None)]
            for t in paused[:n_prior_tasks]:
                window.prior_task_summaries.append(
                    self._task_to_summary(t)
                )

        # 🆕 Phase 6.4 P2: 完成态摘要(用户决策)
        if n_completed_tasks > 0:
            completed = self._task_index.list_completed_tasks(  # type: ignore[union-attr]
                limit=n_completed_tasks, user_id=user_id, include_abandoned=True,
            )
            for t in completed:
                window.completed_task_summaries.append(self._task_to_summary(t))

        # 🆕 Phase 6.4 P2: 闲聊摘要(用户决策:固定条数,默认 30)
        if n_chitchat_tasks > 0:
            chitchat = self._task_index.list_chitchat_tasks(  # type: ignore[union-attr]
                limit=n_chitchat_tasks, user_id=user_id,
            )
            for t in chitchat:
                window.chitchat_task_summaries.append(self._task_to_summary(t))

        logger.info(
            "short_term_view.build_window done current_msgs=%d prior=%d "
            "completed=%d chitchat=%d boundary=%s",
            len(window.current_task_messages),
            len(window.prior_task_summaries),
            len(window.completed_task_summaries),
            len(window.chitchat_task_summaries),
            window.boundary_reason,
        )
        return window

    def _task_to_summary(self, t: Any) -> ThreadSummary:
        """把 :class:`TaskRecord` 转成 :class:`ThreadSummary`(注入 prompt 用)。

        title 当作"用户最后发言"前缀;
        summary 字段(若已写)当管家最后答复;
        都为空时回退到 title。
        """
        return ThreadSummary(
            thread_id=t.source_thread or "",
            user_id=t.user_id,
            last_user_message=t.title[:200],
            last_assistant_message=(t.summary or t.title)[:200],
            last_timestamp=t.end_ts or t.start_ts,
            total_turns=self._task_index.count_messages(t.task_id),  # type: ignore[union-attr]
        )

    # ---- 老路径(无 task_index) ----

    async def _build_window_from_saver(
        self,
        *,
        current_session_id: str,
        current_user_input: str,
        n_prior_tasks: int,
        thread_limit: int,
    ) -> TaskWindow:
        """原版:实时读 SqliteSaver + 规则 boundary(保留兼容老调用)。"""
        window = TaskWindow(source="raw_saver")
        # 1. 读当前 session 全部消息
        current_msgs = await self.read_thread_messages(
            thread_id=current_session_id,
        )
        window.total_threads_scanned = 1

        # 2. 算当前 user message 的 task 边界
        now = datetime.now(UTC)
        prev_turn: TurnInfo | None = None
        if current_msgs:
            last = current_msgs[-1]
            prev_turn = TurnInfo(
                role=last.get("role", "user"),
                content=last.get("content", ""),
                timestamp=last.get("timestamp"),
            )
        boundary: BoundaryResult = self._detector.detect(
            prev_turn=prev_turn,
            current_turn=TurnInfo(
                role="user",
                content=current_user_input,
                timestamp=now,
            ),
        )
        window.boundary_reason = boundary.reason

        # 3. 当前 task 完整保留(不加截断,符合文档 2.5.1.2)
        window.current_task_messages = list(current_msgs)

        # 4. 如果是 boundary,扫别的 thread 拿"上 N 个 task 摘要"
        if boundary.is_boundary:
            threads = await self.list_threads(limit=thread_limit)
            window.total_threads_scanned = len(threads)
            other_threads = [
                t for t in threads
                if t.get("thread_id") and t.get("thread_id") != current_session_id
            ]
            other_threads.sort(
                key=lambda t: t.get("ts") or "",
                reverse=True,
            )
            for t in other_threads[:n_prior_tasks]:
                tid = t.get("thread_id", "")
                if not tid:
                    continue
                msgs = await self.read_thread_messages(thread_id=tid)
                if not msgs:
                    continue
                last_user = next(
                    (m for m in reversed(msgs) if m["role"] == "user"),
                    None,
                )
                last_asst = next(
                    (m for m in reversed(msgs) if m["role"] == "assistant"),
                    None,
                )
                summary = ThreadSummary(
                    thread_id=tid,
                    user_id="",
                    last_user_message=(last_user or {}).get("content", "")[:200],
                    last_assistant_message=(last_asst or {}).get("content", "")[:200],
                    last_timestamp=t.get("ts"),
                    total_turns=len(msgs),
                )
                window.prior_task_summaries.append(summary)
        return window


def build_short_term_prompt_block(window: TaskWindow) -> str:
    """把 :class:`TaskWindow` 格式化成可注入 system prompt 的 markdown。

    格式(Phase 6.4 P2)::
        ### 短期记忆(当前 task)
        [22:00:00 男主人 user] 明天出差
        [22:00:05 阿福 assistant] 好的,我帮您看机票
        [22:01:00 男主人 user] 上海的

        ### 跨 task 锚点(最近的 N 个 task)
        - thread=session-A · 最后:"好的,已完成"

        ### 已完成 task 摘要(最近 N 个)
        - thread=session-B · 标题:"订机票" · 最终答复:"已订好 10/11"

        ### 闲聊记录(最近 N 条)
        - thread=session-C · '周末闲聊'

    返回 "" 表示"无短库内容",上层 facade 据此跳过。

    Args:
        window: :class:`ShortTermView.build_window` 返回的窗口。

    Returns:
        markdown 字符串(注入 system prompt),无内容时返 ""。
    """
    has_content = (
        window.current_task_messages
        or window.prior_task_summaries
        or window.completed_task_summaries
        or window.chitchat_task_summaries
    )
    if not has_content:
        return ""
    parts: list[str] = []
    if window.current_task_messages:
        parts.append(
            f"### 短期记忆(当前 task,共 {len(window.current_task_messages)} 条)"
        )
        for m in window.current_task_messages:
            content = (m.get("content") or "").strip()
            if not content:
                continue
            if len(content) > 300:
                content = content[:297] + "..."
            role = m.get("role", "?")
            who = m.get("user_id") or ""
            # 🆕 Phase 6.4 P1: 拼 who/when
            ts = m.get("timestamp") or ""
            if ts:
                # ISO 缩短到 HH:MM:SS
                hh = ts[11:19] if len(ts) >= 19 else ts
            else:
                hh = ""
            label_parts = [p for p in (hh, who, role) if p]
            label = " ".join(label_parts) or role
            parts.append(f"- [{label}] {content}")
    if window.prior_task_summaries:
        parts.append(
            f"\n### 跨 task 锚点(最近 {len(window.prior_task_summaries)} 个 task)"
        )
        for s in window.prior_task_summaries:
            asst = s.last_assistant_message
            if len(asst) > 120:
                asst = asst[:117] + "..."
            parts.append(f"- thread={s.thread_id[:16]} · 最后答复:{asst!r}")
    # 🆕 Phase 6.4 P2: 完成态 task 摘要(用户决策:不能丢)
    if window.completed_task_summaries:
        parts.append(
            f"\n### 已完成 task 摘要(最近 {len(window.completed_task_summaries)} 个)"
        )
        for s in window.completed_task_summaries:
            asst = s.last_assistant_message
            if len(asst) > 120:
                asst = asst[:117] + "..."
            user = s.last_user_message
            if len(user) > 80:
                user = user[:77] + "..."
            parts.append(
                f"- thread={s.thread_id[:16]} · 标题:{user!r} · 最终答复:{asst!r}"
            )
    # 🆕 Phase 6.4 P2: 闲聊 task 摘要(用户决策:固定条数,默认 30)
    if window.chitchat_task_summaries:
        parts.append(
            f"\n### 闲聊记录(最近 {len(window.chitchat_task_summaries)} 条)"
        )
        for s in window.chitchat_task_summaries:
            user = s.last_user_message
            if len(user) > 120:
                user = user[:117] + "..."
            parts.append(
                f"- thread={s.thread_id[:16]} · {user!r}"
            )
    return "\n".join(parts)


__all__ = [
    "ShortTermView",
    "TaskWindow",
    "ThreadSummary",
    "build_short_term_prompt_block",
]
