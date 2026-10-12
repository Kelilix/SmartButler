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
    # 🆕 消息索引(可选):本 action 对应 user_messages 中的哪些下标。
    # 用于解决"一轮创建多个新 task 时,哪个 user 消息归哪个 task"的问题。
    # 例如:action=[start_new(title="询问身份"), start_new(title="解析网站")],
    # message_ref=[0, 2] 表示第 0 条 user 归第一个 start_new,第 2 条归第二个。
    # 无此字段时退回按顺序一一对应或全归最后一个(向后兼容旧 judge 输出)。
    message_ref: list[int] | None = None


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
1. **新消息**(自上次 judge 以来,仅 user 消息):
{new_messages}

    注意:本列表只含 user 消息,assistant 回复在存储时会自动跟随其前一条 user。

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
    {{"type": "start_new", "category": "phone_call", "title": "打电话问老婆回家时间", "reason": "用户明确换话题", "message_ref": [0]}},
    {{"type": "start_new", "category": "web_parse", "title": "查询机票价格", "reason": "新任务", "message_ref": [2, 3]}},
    {{"type": "pause", "task_id": "t-xxxxx", "reason": "被电话打断"}}
  ],
  "summary": "一句话总结当前所有 task 状态"
}}
```

注意:
- **`message_ref` 是关键字段**:当本轮 action 列表中有**多个** `start_new` 或 `chitchat`(新开 task)时,**每个**都必须带 `message_ref`,指明它承接哪些 `[msg#i]` 消息(用 i 的数字值,不要加 "#"),例如:
  - `message_ref: [0, 1]` = 本 action 承接 [msg#0] 和 [msg#1]
  - 错误输出:两个 action 都没有 message_ref → 系统只能把"查机票"全塞进第二个 task
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
        all_messages: list[dict[str, Any]] | None = None,
        user_id: str | None = None,
    ) -> SegmentResult:
        """对一段新消息做 judge。

        Args:
            thread_id: 消息来源 thread。
            new_messages: **仅 user 消息**,供 judge LLM 做切分决策。
                (judge prompt 依赖"最新一/两条"做切分,assistant 会干扰判断)
            all_messages: 完整对话流(含 assistant/tool),写入侧按位置跟随。
                None = 退化成 new_messages(向后兼容 fallback_segment)。
            user_id: 限定用户,None = 看全 user。

        Returns:
            :class:`SegmentResult`。
        """
        if not new_messages:
            return SegmentResult(summary="no new messages")

        # all_messages 未传入时退化为 new_messages(向后兼容测试)
        if all_messages is None:
            all_messages = new_messages

        # 1. 拿当前 active tasks(看完整 user 池,不只当前 user)
        active = self._index.list_active_tasks(user_id=user_id)
        logger.info(
            "task_segmenter.segment_enter thread=%s new_msg_count=%d all_count=%s "
            "active_count=%d has_llm=%s",
            thread_id, len(new_messages),
            len(all_messages) if all_messages is not None else "same",
            len(active), self._llm is not None,
        )

        # 2. 调 LLM
        if self._llm is None:
            logger.info("task_segmenter.segment_fallback reason=no_llm")
            return self._fallback_segment(
                thread_id=thread_id,
                new_messages=new_messages,
                active_tasks=active,
                all_messages=all_messages,
                reason="no_llm_configured",
            )

        try:
            prompt = self._build_prompt(new_messages, active)
            raw = await self._invoke_llm(prompt)
            actions, summary = self._parse_llm_output(raw)
            # 🆕 Phase 6.5 debug: 解析后 actions 摘要
            if actions:
                parts = []
                for a in actions:
                    base = f"{a.type}({a.task_id or a.title or ''})"
                    if a.message_ref:
                        base = f"{base[:-1]}@{a.message_ref})"
                    parts.append(base)
                action_summary = ",".join(parts)
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
                all_messages=all_messages,
                reason=f"llm_error:{type(e).__name__}",
            )

        # 3. 应用 actions 到 task_index
        self._apply_actions(
            actions=actions,
            thread_id=thread_id,
            all_messages=(all_messages if all_messages is not None else new_messages),
            user_id=user_id or "",
        )
        return SegmentResult(actions=actions, summary=summary, used_llm=True)

    # ---- internals ----

    def _build_prompt(
        self,
        new_messages: list[dict[str, Any]],
        active: list[TaskRecord],
    ) -> str:
        # 新消息:带索引(供 message_ref 直接引用)
        lines: list[str] = []
        for i, m in enumerate(new_messages):
            who = m.get("user_id") or "?"
            when = m.get("spoken_at") or "?"
            role = m.get("role", "?")
            content = (m.get("content") or "")[:300]
            lines.append(f"[msg#{i}] [{when} {who} {role}] {content}")
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
                mr_raw = a.get("message_ref")
                message_ref: list[int] | None = None
                if isinstance(mr_raw, list):
                    message_ref = [int(x) for x in mr_raw if isinstance(x, (int, str)) and str(x).isdigit()]
                    if not message_ref:
                        message_ref = None
                actions.append(SegmentAction(
                    type=str(a.get("type", "")).strip(),
                    task_id=a.get("task_id"),
                    category=a.get("category"),
                    title=a.get("title"),
                    reason=str(a.get("reason", "")),
                    message_ref=message_ref,
                ))
            except Exception:  # noqa: BLE001
                continue
        return actions, str(data.get("summary", ""))

    def _apply_actions(
        self,
        *,
        actions: list[SegmentAction],
        thread_id: str,
        all_messages: list[dict[str, Any]],
        user_id: str,
        new_messages: list[dict[str, Any]] | None = None,
    ) -> None:
        """把 actions 落到 task_index(创建/更新 task + 追加 message)。

        整个流程必须在同一把 Lock 下完成 —— 避免多次 Lock 之间
        被别的 asyncio 任务插入,导致 task_messages 落到错的 task。

        设计原则:
        - judge 只看 user 消息序列,`new_messages` 即该序列(仅 user)。
        - `all_messages` 是完整对话流,写入时 assistant 按位置跟随:
          "A 是谁"→[assistant] 我是管家 → "帮我解析网站"→[assistant] 已解析
          → assistant[1] 归 A 是谁 的 task, assistant[3] 归帮我解析网站 的 task。
        - 用 message_id 在 `all_messages` 里定位 `new_messages` 里各 user 消息的
          实际位置,再向后延伸到下一个 user 或对话结尾 —— 中间的 assistant 跟随。
        """
        if not actions:
            return
        # all_messages 未传入时退化为 new_messages(向后兼容 fallback_segment 路径)
        if all_messages is None:
            all_messages = new_messages or []
        # 保持 new_messages 可用(供 task 记录 participants / start_ts 使用)
        if new_messages is None:
            new_messages = all_messages
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

            # 把新消息落到"本轮承接消息的 task"
            # 设计:
            # - new_messages = 仅 user 消息（供 judge 做切分决策）
            # - all_messages = 完整对话流（含 assistant/tool）
            # - 用 new_messages 里各 user 消息的 message_id 作为锚点,
            #   在 all_messages 里重建完整配对:锚点 user 归对应 task,
            #   其余 assistant/tool 沿用当前锚点。
            if not target_task_ids:
                return

            ordered_targets = list(new_task_ids) + [
                t for t in target_task_ids if t not in new_task_ids
            ]

            # ── 用 message_ref 建立锚点(若有) ──
            # 若 judge 输出里 start_new 带 message_ref,直接按索引映射;
            # 否则退回 one_to_one(仅当 user 数 == new task 数时);
            # 再否则全归最后一个 target。
            uid_to_target: dict[str, str] = {}
            user_msgs_in_new = [m for m in new_messages
                               if (m.get("role") or "user") == "user"]
            ref_mapped = False

            # 🆕 message_ref:把 action.message_ref 映射到 new_task_ids
            # action.task_id 是 None(judge 输出没有 task_id),tid 是后面
            # new_task_id() 才生成的。按 action 顺序 zip:
            #   action[i].message_ref → new_task_ids[i]
            new_actions = [
                act for act in actions
                if act.type in ("start_new", "chitchat") and act.title
            ]
            for a, tid in zip(new_actions, new_task_ids):
                if a.message_ref:
                    for idx in a.message_ref:
                        if 0 <= idx < len(user_msgs_in_new):
                            uid_to_target[user_msgs_in_new[idx]["message_id"]] = tid
                            ref_mapped = True

            if ref_mapped:
                logger.info(
                    "task_segmenter.assign_by_ref n=%d",
                    len(uid_to_target),
                )
            else:
                # 无 message_ref → 退回 one_to_one 或全归最后一个
                n_new = len(new_task_ids)
                one_to_one = (
                    n_new > 0
                    and len(user_msgs_in_new) == n_new
                    and len(new_task_ids) == n_new
                )
                if one_to_one:
                    for m, tgt in zip(user_msgs_in_new, ordered_targets):
                        uid_to_target[m["message_id"]] = tgt
                    logger.info(
                        "task_segmenter.assign_one_to_one n=%d",
                        len(user_msgs_in_new),
                    )
                else:
                    last_tgt = ordered_targets[-1]
                    for m in user_msgs_in_new:
                        uid_to_target[m["message_id"]] = last_tgt
                    logger.info(
                        "task_segmenter.assign_fallback n_user=%d n_new=%d last=%s",
                        len(user_msgs_in_new), n_new, ordered_targets[-1],
                    )

            # ── 第二步:遍历完整对话流,重建配对 ──
            # all_messages 包含 assistant/tool,它们不在 uid_to_target 里,
            # 用 message_id 定位:锚点 user 更新锚点,其余沿用当前锚点。
            pairs: list[tuple[dict[str, Any], str]] = []
            last_target = ordered_targets[-1]  # 兜底
            for m in all_messages:
                msg_id = m.get("message_id") or ""
                role = (m.get("role") or "").lower()
                if msg_id in uid_to_target:
                    last_target = uid_to_target[msg_id]
                    pairs.append((m, last_target))  # 锚点 user 也写入
                elif role in ("assistant", "ai", "tool"):
                    pairs.append((m, last_target))  # assistant 跟随锚点

                msg_target_ids: list[str] = []
                has_seq = "seq" in {
                    row[1] for row in conn.execute(
                        "PRAGMA table_info(task_messages)"
                    ).fetchall()
                }
                seq_cols = ", seq" if has_seq else ""
                seq_ph = ", ?" if has_seq else ""
                insert_sql = f"""
                        INSERT OR IGNORE INTO task_messages
                            (task_id, message_id, thread_id, role, user_id,
                             spoken_at, content{seq_cols})
                        VALUES (?, ?, ?, ?, ?, ?, ?{seq_ph})
                        """
                for m, tgt in pairs:
                    mid = m.get("message_id") or _fallback_message_id(
                        thread_id, m, len(pairs)
                    )
                    row_vals = (
                        tgt, mid, thread_id,
                        m.get("role", "user"), m.get("user_id"),
                        m.get("spoken_at") or _now_iso(),
                        m.get("content", "") or "",
                    )
                    if has_seq:
                        # seq 必须在 task 内全局递增,不能每轮 judge 从 0 重开 ——
                        # 同一个 task 会被多轮 judge 追加消息,每轮都从 0 开始
                        # 会让不同轮的消息撞到相同 seq,排序就乱了。
                        # 偏移量 = 该 task 现有最大 seq + 1。
                        row_vals = row_vals + (
                            _next_seq(conn, tgt),  # type: ignore[arg-type]
                        )
                    conn.execute(insert_sql, row_vals)
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
        all_messages: list[dict[str, Any]] | None = None,
        reason: str = "",
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
            all_messages=(all_messages if all_messages is not None else new_messages),
            user_id=last.get("user_id") or "",
        )
        return SegmentResult(
            actions=actions, summary=boundary.reason, used_llm=False,
            fallback_reason=reason,
        )


def _now_iso() -> str:
    from datetime import datetime, UTC
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def _fallback_message_id(
    thread_id: str, msg: dict[str, Any], idx: int,
) -> str:
    """消息缺 message_id 时的回退主键。

    ⚠️ 不能只用 ``(spoken_at, len(content))``:
    - assistant / tool 消息的 ``spoken_at`` 是 ``None``(只有 HumanMessage
      在 orchestrator.ainvoke 里带 additional_kwargs['spoken_at'])
    - 多条 assistant 就会得到相同键 → ``INSERT OR IGNORE`` 静默吞掉,
      message_id 是主键的一部分,吞掉等于丢消息。

    所以把数组下标 idx 拼进去,保证同一批次内唯一。
    """
    spoken = msg.get("spoken_at") or ""
    content = msg.get("content") or ""
    return f"{thread_id}:{spoken}:{len(content)}:{idx}"


def _next_seq(conn: Any, task_id: str) -> int:
    """该 task 下一个可用 seq(= 现有最大 seq + 1,空表则 0)。

    必须在 task 内全局递增:同一个 task 会被多轮 judge 追加消息,
    每轮都从 0 开始会让不同轮的消息撞到相同 seq,排序错乱。
    """
    row = conn.execute(
        "SELECT COALESCE(MAX(COALESCE(seq, rowid - 1)), -1) AS mx "
        "FROM task_messages WHERE task_id = ?",
        (task_id,),
    ).fetchone()
    mx = row["mx"] if row is not None and row["mx"] is not None else -1
    return int(mx) + 1


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
