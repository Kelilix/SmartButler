"""Task 划分器(LLM judge)—— Phase 6.4 P1。

为什么是 LLM:
- 闲聊 vs 业务场景纯规则判不出来(无触发词、间隔短)
- task 切换 + 回归需要"理解两小时前订机票被电话打断"的语义
- 跨 thread 切片,LLM 拿全局视图才好决策

设计:
1. **输入**:某 thread 自上次 judge 以来的新消息 + 当前所有 active task
2. **输出**:一系列 action(continue/start_new/pause/return_to/abandon/complete)
3. **降级**:LLM 失败/未配 → 走 :class:`TaskBoundaryDetector` 规则
4. **异步**:由 ``TaskScheduler`` 周期调用,不阻塞 user 响应

Action 类型:
- ``continue`` task_id=X — 继续累加到现有 task
- ``start_new`` category=Y title=Z — 新开 task
- ``pause`` task_id=X — 暂停(被打断)
- ``return_to`` task_id=X — 切回之前 paused 的 task
- ``complete`` task_id=X — 标完成
- ``abandon`` task_id=X — 标废弃
- ``chitchat`` category=chitchat title=Z — 整段当闲聊 task(已开则 append)
"""
from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from typing import Any

from smartbutler.emotion.memory.task_boundary import (
    BoundaryResult,
    TaskBoundaryDetector,
    TurnInfo,
)
from smartbutler.emotion.memory.task_index import (
    TaskIndex,
    TaskMessageRecord,
    TaskRecord,
    new_task_id,
)
from smartbutler.utils.logging import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# 决策数据结构
# ---------------------------------------------------------------------------


@dataclass
class SegmentAction:
    """单条 judge 决策。"""

    type: str                                   # continue/start_new/pause/...
    task_id: str | None = None                  # continue/pause/return_to/complete/abandon 用
    category: str | None = None                 # start_new/chitchat 用
    title: str | None = None                    # start_new/chitchat 用
    reason: str = ""


@dataclass
class SegmentResult:
    """一轮 judge 的全部决策。"""

    actions: list[SegmentAction] = field(default_factory=list)
    summary: str = ""
    used_llm: bool = False
    fallback_reason: str = ""


# ---------------------------------------------------------------------------
# LLM judge
# ---------------------------------------------------------------------------


_JUDGE_PROMPT = """你是"智能管家"的后台分析模块,负责把短期对话流切分成"任务(task)"。

## 输入
1. **新消息**(自上次 judge 以来):
{new_messages}

2. **当前活跃 task**(ongoing/paused):
{active_tasks}

## 硬规则(优先于一切判断)
对**最新一条**消息,先判定它是否属于"已有 task":

- **A. 语义明显换话题**(新消息的意图在所有 active task 的 title/category 里都找不到直接对应)
  → **必须** `pause`(在外的 ongoing task)+ `start_new` 新 task
  → **不允许** 用 `continue` 强行挂到不相关 task 上"沾边"
  → ⚠️ **换话题时必须暂停"每一个"没被本轮继续的 active task**,
     不能只暂停你最先注意到的那一个。典型反例(实测踩过):
     有 A(网页解析)+ B(闲聊)两个 active task,用户开了新话题 C,
     只输出了 `pause(B)+start_new(C)`,A 就被永久遗忘、永远卡在 ongoing
     → 正确做法:对**所有**不再收到 `continue` 的 active task 都输出 `pause`
- **B. 语义模糊**(既像继续又像新开)
  → 倾向 `start_new category=chitchat`(宁可多建一个也别吞掉)
- **C. 明确属于某个 active task**
  → `continue` / `return_to` 即可
- **D. 闲聊/问候/情绪**(无明确目标)
  → `chitchat` 单独成 task(已开则继续 append 到那个 chitchat task)

判断 "明显换话题" 的方法:
- 新消息的关键词(天气/吃饭/订机票/查代码)与 active task 的 title/category 重合度 < 30%
- 主动词完全不同(上一条在"查网站",这条突然"吃什么",应换话题)
- 时间/场景明显跳变(上一条是工作,这条突然"今天天气")

## 你的工作
对**最新一/两条新消息**,输出 0~N 个 action。常见模式:

- 用户在**已有 task 上继续聊** → 输出一行 `continue`
- 用户**新开一个 task** → `start_new`
- 用户从 A 切到 B → `pause`(A)+ `start_new`(B)
- 用户**切回之前 paused 的 A** → `return_to A`(+ pause B)
- **闲聊/情绪/问候**(无明确目标)→ `chitchat`
- A 已经结束(用户/管家都说完成) → `complete A`

## 输出格式(严格 JSON,无任何额外文字)
```json
{{
  "actions": [
    {{"type": "continue", "task_id": "t-xxxxx", "reason": "继续聊订机票细节"}},
    {{"type": "start_new", "category": "phone_call", "title": "打电话问老婆回家时间", "reason": "用户明确换话题"}},
    {{"type": "pause", "task_id": "t-xxxxx", "reason": "被电话打断"}}
  ],
  "summary": "一句话总结当前所有 task 状态"
}}
```

注意:
- 闲聊必须 `category=chitchat`,title 简明(例 "周末闲聊", "情绪问候")
- 至少给一个 action,不能"什么都不做"(除非真的没新消息)
- task_id 必须从"当前活跃 task"列表里复制(不要瞎编)
"""


class TaskSegmenter:
    """LLM judge 主体。

    用法::

        seg = TaskSegmenter(task_index=idx, llm=my_llm, detector=det)
        result = await seg.segment(
            thread_id="dev-session-1",
            new_messages=[...],         # List[{"role":..., "content":..., "user_id":..., "spoken_at":...}]
        )
        # result.actions 给出本轮要做的变更
    """

    def __init__(
        self,
        *,
        task_index: TaskIndex,
        llm: Any | None = None,
        detector: TaskBoundaryDetector | None = None,
        # 🆕 Phase 6.5: 单次 LLM judge 调用超时(秒),超时 → fallback 规则
        # 0=不超时(老行为,可能卡死 30s+)
        llm_timeout_seconds: float = 8.0,
    ) -> None:
        if llm_timeout_seconds < 0:
            raise ValueError("llm_timeout_seconds must >= 0")
        self._index = task_index
        self._llm = llm
        self._detector = detector or TaskBoundaryDetector()
        self._llm_timeout = llm_timeout_seconds

    async def segment(
        self,
        *,
        thread_id: str,
        new_messages: list[dict[str, Any]],
        user_id: str | None = None,
    ) -> SegmentResult:
        """对一段新消息做 judge。

        Args:
            thread_id: 消息来源 thread。
            new_messages: 自上次 judge 以来的新消息。
            user_id: 限定用户,None = 看全 user。

        Returns:
            :class:`SegmentResult`。
        """
        if not new_messages:
            return SegmentResult(summary="no new messages")

        # 1. 拿当前 active tasks(看完整 user 池,不只当前 user)
        active = self._index.list_active_tasks(user_id=user_id)
        logger.info(
            "task_segmenter.segment_enter thread=%s new_msg_count=%d active_count=%d has_llm=%s",
            thread_id, len(new_messages), len(active), self._llm is not None,
        )

        # 2. 调 LLM
        if self._llm is None:
            logger.info("task_segmenter.segment_fallback reason=no_llm")
            return self._fallback_segment(
                thread_id=thread_id,
                new_messages=new_messages,
                active_tasks=active,
                reason="no_llm_configured",
            )

        try:
            prompt = self._build_prompt(new_messages, active)
            raw = await self._invoke_llm(prompt)
            actions, summary = self._parse_llm_output(raw)
            # 🆕 Phase 6.5 debug: 解析后 actions 摘要
            if actions:
                action_summary = ",".join(
                    f"{a.type}({a.task_id or a.title or ''})" for a in actions
                )
            else:
                action_summary = "(empty)"
            logger.info("task_segmenter.judge_parsed summary=%r actions=%d [%s]",
                        summary, len(actions), action_summary)
        except Exception as e:  # noqa: BLE001
            logger.warning("task_segmenter.llm_failed: %s", e)
            return self._fallback_segment(
                thread_id=thread_id,
                new_messages=new_messages,
                active_tasks=active,
                reason=f"llm_error:{type(e).__name__}",
            )

        # 3. 应用 actions 到 task_index
        self._apply_actions(
            actions=actions,
            thread_id=thread_id,
            new_messages=new_messages,
            user_id=user_id or "",
        )
        return SegmentResult(actions=actions, summary=summary, used_llm=True)

    # ---- internals ----

    def _build_prompt(
        self,
        new_messages: list[dict[str, Any]],
        active: list[TaskRecord],
    ) -> str:
        # 新消息:带 who/when
        lines: list[str] = []
        for m in new_messages:
            who = m.get("user_id") or "?"
            when = m.get("spoken_at") or "?"
            role = m.get("role", "?")
            content = (m.get("content") or "")[:300]
            lines.append(f"[{when} {who} {role}] {content}")
        msgs_block = "\n".join(lines) if lines else "(无)"

        # 活跃 task
        if active:
            at_lines = []
            for t in active:
                at_lines.append(
                    f"- task_id={t.task_id} | status={t.status} | "
                    f"category={t.category} | title={t.title!r} | "
                    f"participants={t.participants}"
                )
            at_block = "\n".join(at_lines)
        else:
            at_block = "(无活跃 task)"

        return _JUDGE_PROMPT.format(
            new_messages=msgs_block, active_tasks=at_block
        )

    async def _invoke_llm(self, prompt: str) -> str:
        """调 LLM,期望返回 JSON 字符串。

        🆕 Phase 6.5: 加内部 timeout 保护 — 超时抛 ``LLMTimeout`` 异常,
        让上层走 ``_fallback_segment``(规则边界)而不是直接 return 0。
        """
        from smartbutler.capabilities.llm.types import (
            FinishReason,
            LLMResponse,
            Message,
            Role,
        )
        prompt_preview = prompt[:600] + ("...(truncated)" if len(prompt) > 600 else "")
        logger.info("task_segmenter.judge_prompt prompt_len=%d preview=%s",
                    len(prompt), prompt_preview)
        lc_messages = [
            Message(role=Role.SYSTEM, content="你是 JSON 输出助手,严格按 schema。"),
            Message(role=Role.USER, content=prompt),
        ]
        if self._llm_timeout > 0:
            resp: LLMResponse = await asyncio.wait_for(
                self._llm.chat(messages=lc_messages, temperature=0.1),
                timeout=self._llm_timeout,
            )
        else:
            resp = await self._llm.chat(messages=lc_messages, temperature=0.1)
        raw = resp.content or ""
        raw_preview = raw[:800] + ("...(truncated)" if len(raw) > 800 else "")
        logger.info("task_segmenter.judge_response raw_len=%d raw=%s",
                    len(raw), raw_preview)
        return raw

    _JSON_BLOCK = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)

    def _parse_llm_output(self, raw: str) -> tuple[list[SegmentAction], str]:
        """从 LLM 输出解析 actions + summary。失败返空。"""
        if not raw:
            logger.warning("task_segmenter.judge_parse empty_raw")
            return [], ""
        # 抓 ```json {...}``` 块
        m = self._JSON_BLOCK.search(raw)
        candidate = m.group(1) if m else raw.strip()
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError as e:
            # 尝试抓第一个 {...} 块
            brace_start = candidate.find("{")
            brace_end = candidate.rfind("}")
            if brace_start >= 0 and brace_end > brace_start:
                try:
                    data = json.loads(candidate[brace_start:brace_end + 1])
                except json.JSONDecodeError as e2:
                    logger.warning(
                        "task_segmenter.judge_parse json_failed err=%s raw_preview=%s",
                        e2, candidate[:200],
                    )
                    return [], ""
            else:
                logger.warning(
                    "task_segmenter.judge_parse no_brace_block raw_preview=%s",
                    candidate[:200],
                )
                return [], ""
        actions: list[SegmentAction] = []
        for a in data.get("actions") or []:
            try:
                actions.append(SegmentAction(
                    type=str(a.get("type", "")).strip(),
                    task_id=a.get("task_id"),
                    category=a.get("category"),
                    title=a.get("title"),
                    reason=str(a.get("reason", "")),
                ))
            except Exception:  # noqa: BLE001
                continue
        return actions, str(data.get("summary", ""))

    def _apply_actions(
        self,
        *,
        actions: list[SegmentAction],
        thread_id: str,
        new_messages: list[dict[str, Any]],
        user_id: str,
    ) -> None:
        """把 actions 落到 task_index(创建/更新 task + 追加 message)。

        整个流程必须在同一把 Lock 下完成 —— 避免多次 Lock 之间
        被别的 asyncio 任务插入,导致 task_messages 落到错的 task。
        """
        if not actions:
            return
        # 找本轮会 active 的 task
        # 用 list 保 insert 顺序 —— target_task_ids[-1] 必须是"本轮最后涉及的 task"
        # (新开的 task 而不是被 pause 的 task),否则 message 落到错的 task
        target_task_ids: list[str] = []
        new_task_ids: list[str] = []

        # 借 TaskIndex 的 lock(防止跟其他 segmenter 实例 / direct 写入冲突)
        with self._index._lock, self._index._conn() as conn:  # type: ignore[attr-defined]
            for a in actions:
                if a.type in ("start_new", "chitchat") and a.title:
                    tid = new_task_id()
                    status = "chitchat_done" if a.type == "chitchat" else "ongoing"
                    rec = TaskRecord(
                        task_id=tid,
                        user_id=user_id or "",
                        category=a.category or ("chitchat" if a.type == "chitchat" else "other"),
                        title=a.title,
                        status=status,
                        start_ts=(new_messages[0].get("spoken_at") or _now_iso()),
                        end_ts=None,
                        summary="",
                        participants=list({m.get("user_id") for m in new_messages
                                            if m.get("user_id")}),
                        source_thread=thread_id,
                        updated_at=_now_iso(),
                    )
                    conn.execute(
                        """
                        INSERT INTO tasks
                            (task_id, user_id, category, title, status,
                             start_ts, end_ts, summary, participants, source_thread, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(task_id) DO UPDATE SET
                            user_id=excluded.user_id,
                            category=excluded.category,
                            title=excluded.title,
                            status=excluded.status,
                            end_ts=excluded.end_ts,
                            summary=excluded.summary,
                            participants=excluded.participants,
                            source_thread=excluded.source_thread,
                            updated_at=excluded.updated_at
                        """,
                        (
                            rec.task_id, rec.user_id, rec.category, rec.title, rec.status,
                            rec.start_ts, rec.end_ts, rec.summary,
                            json.dumps(rec.participants, ensure_ascii=False),
                            rec.source_thread, rec.updated_at,
                        ),
                    )
                    target_task_ids.append(tid)
                    new_task_ids.append(tid)
                elif a.task_id:
                    # ⚠️ 只有"承接消息"的 action 才是消息的归属地。
                    # pause/return_to 只是改状态,不代表本轮消息属于它 ——
                    # 实测 bug:[pause(A), start_new(B)] 时 pause 的 A 也会
                    # 进 target 列表,取 [-1] 之前要排除掉,否则消息落错 task。
                    if a.type not in ("pause",):
                        if a.task_id not in target_task_ids:
                            target_task_ids.append(a.task_id)
                    if a.type in ("complete", "abandon", "pause", "return_to"):
                        cur_row = conn.execute(
                            "SELECT * FROM tasks WHERE task_id = ?", (a.task_id,)
                        ).fetchone()
                        if cur_row is None:
                            continue
                        new_status = {
                            "complete": "completed",
                            "abandon": "abandoned",
                            "pause": "paused",
                            "return_to": "ongoing",
                        }.get(a.type, cur_row["status"])
                        # 🆕 Phase 6.5: pause / return_to 走专用方法,
                        # 更新 paused_at + paused_turn_count(代码层管生命周期)
                        if a.type == "pause":
                            conn.execute(
                                """
                                UPDATE tasks SET
                                    status = 'paused',
                                    paused_at = ?,
                                    paused_turn_count = 0,
                                    end_ts = NULL,
                                    updated_at = ?
                                WHERE task_id = ?
                                """,
                                (_now_iso(), _now_iso(), a.task_id),
                            )
                        elif a.type == "return_to":
                            conn.execute(
                                """
                                UPDATE tasks SET
                                    status = 'ongoing',
                                    paused_at = NULL,
                                    paused_turn_count = 0,
                                    end_ts = NULL,
                                    updated_at = ?
                                WHERE task_id = ?
                                """,
                                (_now_iso(), a.task_id),
                            )
                        else:
                            conn.execute(
                                """
                                UPDATE tasks SET
                                    status = ?,
                                    end_ts = ?,
                                    updated_at = ?
                                WHERE task_id = ?
                                """,
                                (
                                    new_status,
                                    (_now_iso() if new_status in ("completed", "abandoned") else cur_row["end_ts"]),
                                    _now_iso(),
                                    a.task_id,
                                ),
                            )

            # 把新消息落到"本轮承接消息的 task"(约定:本轮涉及的 task 累加 message)
            if target_task_ids:
                # ── 归属规则 ──────────────────────────────────────────
                # 多个 start_new 同时出现时,judge 没有指明"哪条消息归哪个
                # task"。实测(14 轮 replay):[start_new(起名), start_new(课程)]
                # 会把"你叫阿福吧"和课程消息**全**塞进最后一个 task(课程),
                # 导致课程 task 的 fact 变成
                #   "你叫阿福吧 | 帮我解析下网站... | 我养的小动物..."
                # 关键词被稀释,召回时 course 查询命中不了。
                #
                # 规则:new task 数 == 本轮 user 消息数 时,**按顺序一一对应**
                # (judge 按消息顺序输出 action,这是 prompt 要求的);
                # 否则退回"全部归最后一个 target"(保持旧行为,不猜)。
                n_new = len(new_task_ids)
                user_msgs = [m for m in new_messages
                             if (m.get("role") or "user") == "user"] or new_messages
                # 目标队列:新 task 优先(按 judge 输出顺序),其余排后面
                ordered_targets = list(new_task_ids) + [
                    t for t in target_task_ids if t not in new_task_ids
                ]
                one_to_one = (
                    n_new > 0
                    and len(user_msgs) == n_new
                    and len(new_task_ids) == n_new
                )
                if one_to_one:
                    pairs = list(zip(user_msgs, ordered_targets))
                    logger.info(
                        "task_segmenter.assign_messages_one_to_one n=%d",
                        len(pairs),
                    )
                else:
                    # 无法一一对应 → 全归最后一个 target
                    pairs = [(m, ordered_targets[-1]) for m in new_messages]

                msg_target_ids: list[str] = []
                for m, tgt in pairs:
                    mid = m.get("message_id") or f"{thread_id}:{m.get('spoken_at', '')}:{len(m.get('content', ''))}"
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO task_messages
                            (task_id, message_id, thread_id, role, user_id, spoken_at, content)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            tgt, mid, thread_id,
                            m.get("role", "user"), m.get("user_id"),
                            m.get("spoken_at") or _now_iso(),
                            m.get("content", "") or "",
                        ),
                    )
                    msg_target_ids.append(tgt)
                # 🆕 真正收到消息的 task 本轮活跃 → staleness 归零。
                # 没有这一步,tick_paused_staleness 只增不减,活跃 task 也会被误杀。
                # 注意只重置 msg_target_ids(实际拿到消息的),
                # 不能用 target_task_ids —— 本轮只是被 pause 而没收到消息的
                # task 若也归零,它的 staleness 会被无限推迟。
                for tid in set(msg_target_ids):
                    conn.execute(
                        "UPDATE tasks SET paused_turn_count = 0, updated_at = ? "
                        "WHERE task_id = ? AND status IN ('ongoing', 'paused')",
                        (_now_iso(), tid),
                    )
            conn.commit()
        # with 块退出 → connection 关闭;但事务已 commit,其他 connection 可见

    def _fallback_segment(
        self,
        *,
        thread_id: str,
        new_messages: list[dict[str, Any]],
        active_tasks: list[TaskRecord],
        reason: str,
    ) -> SegmentResult:
        """无 LLM 时的降级:走 :class:`TaskBoundaryDetector` 规则。

        简化策略:
        - 边界 = True → 找最近的 ongoing task 标 pause,开新 ongoing task
        - 边界 = False → 累加到最近 ongoing task(必须有 target,否则开新)
        - 没 active task → 直接开新 ongoing
        """
        actions: list[SegmentAction] = []
        # 只对"最新一条"做边界检测
        last = new_messages[-1]
        prev = new_messages[-2] if len(new_messages) >= 2 else None
        prev_turn = (
            TurnInfo(
                role=prev.get("role", "user"),
                content=prev.get("content", ""),
                timestamp=_parse_iso(prev.get("spoken_at")),
            )
            if prev else None
        )
        cur_turn = TurnInfo(
            role=last.get("role", "user"),
            content=last.get("content", ""),
            timestamp=_parse_iso(last.get("spoken_at")),
        )
        boundary: BoundaryResult = self._detector.detect(
            prev_turn=prev_turn, current_turn=cur_turn,
        )

        ongoing = next((t for t in active_tasks if t.status == "ongoing"), None)
        if boundary.is_boundary:
            if ongoing is not None:
                actions.append(SegmentAction(
                    type="pause", task_id=ongoing.task_id, reason=f"boundary:{boundary.reason}",
                ))
            title = (last.get("content") or "")[:30] or "新 task"
            actions.append(SegmentAction(
                type="start_new",
                category="other",
                title=title,
                reason=f"boundary:{boundary.reason}",
            ))
        else:
            # 🆕 Phase 6.4 P1: 即便不是 boundary,也必须 append message 到 ongoing
            # 老逻辑只 start_new(没有 ongoing 才开),导致 message 丢失
            if ongoing is None:
                title = (new_messages[0].get("content") or "")[:30] or "新 task"
                actions.append(SegmentAction(
                    type="start_new",
                    category="other",
                    title=title,
                    reason="no_active_task",
                ))
            else:
                # 显式发一个 continue 动作,让 _apply_actions 把 message 落到 ongoing
                actions.append(SegmentAction(
                    type="continue",
                    task_id=ongoing.task_id,
                    reason="accumulate",
                ))

        self._apply_actions(
            actions=actions,
            thread_id=thread_id,
            new_messages=new_messages,
            user_id=last.get("user_id") or "",
        )
        return SegmentResult(
            actions=actions, summary=boundary.reason, used_llm=False,
            fallback_reason=reason,
        )


def _now_iso() -> str:
    from datetime import datetime, UTC
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def _parse_iso(s: str | None):
    from datetime import datetime
    if not s:
        return None
    try:
        # 兼容 "2026-10-10T22:00:00+00:00" 和 "2026-10-10T22:00:00Z"
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None


__all__ = [
    "TaskSegmenter",
    "SegmentAction",
    "SegmentResult",
]
