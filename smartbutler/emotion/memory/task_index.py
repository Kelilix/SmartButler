"""Task 索引层(Phase 6.4 P1)。

按"管家大记忆池"模型,把"短期库里的对话"按 task 维度组织。

关键设计:
1. **view 层** —— 不改 SqliteSaver schema(老数据兼容)
2. **SQLite 单独文件** ``data/task_index.db``,跟 ``short_term.db`` 错开
3. **两张表**:
   - ``tasks``:一个 task 一行(category/title/status/who/when/...)
   - ``task_messages``:task ↔ message 多对多(含说话人/时间/内容)
4. **仅存 view 数据** —— 真实 message 仍由 SqliteSaver 持有,本表只是索引 + 维度信息

完成态语义(用户决策 2026-10-10):
- ``status in (ongoing, paused)`` → 注入 prompt
- ``status in (completed, abandoned, chitchat_done)`` → 不再注入(除非显式查询)
- chitchat 同样有 task 实体,但默认 completed
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from smartbutler.utils.logging import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# 数据类
# ---------------------------------------------------------------------------


@dataclass
class TaskRecord:
    """单条 task 元数据(对应 tasks 表一行)。"""

    task_id: str
    user_id: str
    category: str                   # booking/phone_call/chitchat/...
    title: str                      # LLM 命名短句
    status: str                     # ongoing/paused/completed/abandoned/chitchat_done
    start_ts: str                   # ISO
    end_ts: str | None = None
    summary: str = ""
    participants: list[str] = field(default_factory=list)   # 参与 user_id 列表
    source_thread: str = ""         # 首次识别时的 thread_id
    updated_at: str = ""
    # 🆕 Phase 6.5: 暂停生命周期追踪
    # - paused_at: 最近一次进入 paused 状态的时刻(ISO,None=未暂停过)
    # - paused_turn_count: paused 状态下累计错过的 user 消息轮数
    #   超过 MemorySettings.paused_max_staleness 时自动 complete
    paused_at: str | None = None
    paused_turn_count: int = 0


@dataclass
class TaskMessageRecord:
    """单条 message 落 task 的关联记录。"""

    task_id: str
    message_id: str
    thread_id: str
    role: str                       # user/assistant
    user_id: str | None             # 说话人
    spoken_at: str                  # ISO
    content: str


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


class TaskIndex:
    """Task 索引访问层(线程安全,单连接 + Lock)。

    用法::

        idx = TaskIndex(Path("data/task_index.db"))
        idx.upsert_task(TaskRecord(...))
        for m in idx.iter_task_messages(task_id):
            ...
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_schema()

    @property
    def path(self) -> Path:
        return self._path

    def _conn(self) -> sqlite3.Connection:
        """每次返回新连接(简单可靠,SQLite 写并发由 Lock 串行化)。"""
        conn = sqlite3.connect(str(self._path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._lock, self._conn() as conn:
            conn.executescript(_DDL)
            # 🆕 Phase 6.5: 给已存在的 tasks 表补 paused_at / paused_turn_count 列
            # (Phase 6.4 P1 引入 task_index.db 时只有旧列)
            # CREATE TABLE IF NOT EXISTS 不会给老表加新列,必须显式 ALTER
            cur_cols = {row[1] for row in conn.execute("PRAGMA table_info(tasks)").fetchall()}
            if "paused_at" not in cur_cols:
                conn.execute("ALTER TABLE tasks ADD COLUMN paused_at TEXT")
            if "paused_turn_count" not in cur_cols:
                conn.execute(
                    "ALTER TABLE tasks ADD COLUMN paused_turn_count INTEGER NOT NULL DEFAULT 0"
                )
            conn.commit()

    # ---- task CRUD ----

    def upsert_task(self, task: TaskRecord) -> None:
        """插入或更新 task(按 task_id)。"""
        now = _now_iso()
        with self._lock, self._conn() as conn:
            conn.execute(
                """
                INSERT INTO tasks
                    (task_id, user_id, category, title, status,
                     start_ts, end_ts, summary, participants, source_thread, updated_at,
                     paused_at, paused_turn_count)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    user_id=excluded.user_id,
                    category=excluded.category,
                    title=excluded.title,
                    status=excluded.status,
                    end_ts=excluded.end_ts,
                    summary=excluded.summary,
                    participants=excluded.participants,
                    source_thread=excluded.source_thread,
                    updated_at=excluded.updated_at,
                    paused_at=excluded.paused_at,
                    paused_turn_count=excluded.paused_turn_count
                """,
                (
                    task.task_id, task.user_id, task.category, task.title, task.status,
                    task.start_ts, task.end_ts, task.summary,
                    json.dumps(task.participants, ensure_ascii=False),
                    task.source_thread, now,
                    task.paused_at, task.paused_turn_count,
                ),
            )
            conn.commit()

    def get_task(self, task_id: str) -> TaskRecord | None:
        with self._lock, self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
        return _row_to_task(row) if row else None

    def list_active_tasks(self, user_id: str | None = None) -> list[TaskRecord]:
        """返回 ongoing/paused 的 task(给 prompt 注入用)。

        Args:
            user_id: 限定 user,None = 不限定。

        Returns:
            按 start_ts 倒序(最新在前)。
        """
        sql = (
            "SELECT * FROM tasks "
            "WHERE status IN ('ongoing', 'paused') "
        )
        params: tuple = ()
        if user_id:
            sql += "AND user_id = ? "
            params = (user_id,)
        sql += "ORDER BY start_ts DESC"
        with self._lock, self._conn() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_row_to_task(r) for r in rows if r]

    def list_recent_tasks(
        self, *, limit: int = 20, user_id: str | None = None,
    ) -> list[TaskRecord]:
        """返回最近 N 条 task(任何状态,审计/调试用)。"""
        sql = "SELECT * FROM tasks "
        params: tuple = ()
        if user_id:
            sql += "WHERE user_id = ? "
            params = (user_id,)
        sql += "ORDER BY start_ts DESC LIMIT ?"
        with self._lock, self._conn() as conn:
            rows = conn.execute(sql, (*params, limit)).fetchall()
        return [_row_to_task(r) for r in rows if r]

    def list_completed_tasks(
        self,
        *,
        limit: int = 3,
        user_id: str | None = None,
        include_abandoned: bool = True,
    ) -> list[TaskRecord]:
        """🆕 Phase 6.4 P2: 返回"完成态" task 摘要(给 prompt 注入用)。

        完成态 = ``completed``(∪ ``abandoned`` 若 ``include_abandoned=True``)
        **不包含** ``chitchat_done``(闲聊走 ``list_chitchat_tasks`` 单独配)。

        排序:按 ``end_ts`` 倒序(刚完成的优先)。

        Args:
            limit: 最多返回多少条。
            user_id: 限定 user,None = 不限定。
            include_abandoned: True=把 abandoned 也算完成态(用户决策 2026-10-11)。

        Returns:
            按 end_ts 倒序的 TaskRecord 列表。
        """
        statuses = ["completed"]
        if include_abandoned:
            statuses.append("abandoned")
        placeholders = ",".join("?" for _ in statuses)
        sql = f"SELECT * FROM tasks WHERE status IN ({placeholders}) "
        params: list[Any] = list(statuses)
        if user_id:
            sql += "AND user_id = ? "
            params.append(user_id)
        # 没 end_ts 的 fallback 到 start_ts
        sql += "ORDER BY COALESCE(end_ts, start_ts) DESC LIMIT ?"
        params.append(limit)
        with self._lock, self._conn() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [_row_to_task(r) for r in rows if r]

    def list_chitchat_tasks(
        self,
        *,
        limit: int = 30,
        user_id: str | None = None,
    ) -> list[TaskRecord]:
        """🆕 Phase 6.4 P2: 返回"闲聊" task 摘要(给 prompt 注入用)。

        chitchat_done 状态的 task 单独配,默认取最近 30 条(用户决策 2026-10-11)。

        排序:按 ``end_ts`` 倒序(最近的闲聊优先)。

        Args:
            limit: 最多返回多少条。
            user_id: 限定 user,None = 不限定。

        Returns:
            按 end_ts 倒序的 TaskRecord 列表。
        """
        sql = "SELECT * FROM tasks WHERE status = 'chitchat_done' "
        params: list[Any] = []
        if user_id:
            sql += "AND user_id = ? "
            params.append(user_id)
        sql += "ORDER BY COALESCE(end_ts, start_ts) DESC LIMIT ?"
        params.append(limit)
        with self._lock, self._conn() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [_row_to_task(r) for r in rows if r]

    # ---- message CRUD ----

    def append_message(self, msg: TaskMessageRecord) -> None:
        """往 task 追加一条 message(去重由 (task_id, message_id) 主键保证)。"""
        with self._lock, self._conn() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO task_messages
                    (task_id, message_id, thread_id, role, user_id, spoken_at, content)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    msg.task_id, msg.message_id, msg.thread_id,
                    msg.role, msg.user_id, msg.spoken_at, msg.content,
                ),
            )
            conn.commit()

    def append_messages(self, msgs: Iterable[TaskMessageRecord]) -> None:
        """批量追加。"""
        with self._lock, self._conn() as conn:
            conn.executemany(
                """
                INSERT OR IGNORE INTO task_messages
                    (task_id, message_id, thread_id, role, user_id, spoken_at, content)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (m.task_id, m.message_id, m.thread_id, m.role, m.user_id,
                     m.spoken_at, m.content)
                    for m in msgs
                ],
            )
            conn.commit()

    def iter_task_messages(self, task_id: str) -> list[TaskMessageRecord]:
        """返回 task 下的全部 message(按 spoken_at 升序)。"""
        with self._lock, self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM task_messages WHERE task_id = ? ORDER BY spoken_at ASC",
                (task_id,),
            ).fetchall()
        return [
            TaskMessageRecord(
                task_id=r["task_id"],
                message_id=r["message_id"],
                thread_id=r["thread_id"],
                role=r["role"],
                user_id=r["user_id"],
                spoken_at=r["spoken_at"],
                content=r["content"],
            )
            for r in rows
        ]

    def count_messages(self, task_id: str) -> int:
        with self._lock, self._conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM task_messages WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        return int(row["c"]) if row else 0

    # ---- 🆕 Phase 6.5: paused task 生命周期 ----

    def tick_paused_staleness(
        self, *, max_staleness: int, now: str | None = None,
    ) -> list[str]:
        """对所有 paused/ongoing task:把 ``paused_turn_count`` +1,超过阈值的 auto-complete。

        调用时机:
        - 每个 user 消息进来时(由 :class:`TaskScheduler.tick_on_user_message` 触发)
        - 周期兜底(由 consolidation_scheduler daily 触发)

        Args:
            max_staleness: paused_turn_count 超过此值自动 complete(<=0 = 关闭,只 +1)
            now: ISO 时间戳(测试用,可注入固定值)。

        Returns:
            被自动 complete 的 task_id 列表(供上层 logger 报)。
        """
        if max_staleness <= 0:
            # 配置关闭,只做计数不打入 complete
            return []
        ts = now or _now_iso()
        completed: list[str] = []
        with self._lock, self._conn() as conn:
            rows = conn.execute(
                "SELECT task_id, paused_turn_count FROM tasks "
                "WHERE status IN ('paused', 'ongoing')",
            ).fetchall()
            for row in rows:
                new_count = int(row["paused_turn_count"] or 0) + 1
                if new_count > max_staleness:
                    conn.execute(
                        """
                        UPDATE tasks SET
                            status = 'completed',
                            end_ts = ?,
                            updated_at = ?,
                            paused_turn_count = ?
                        WHERE task_id = ?
                        """,
                        (ts, ts, new_count, row["task_id"]),
                    )
                    completed.append(row["task_id"])
                    logger.info(
                        "task_index.paused_auto_complete task_id=%s turns=%d",
                        row["task_id"], new_count,
                    )
                else:
                    conn.execute(
                        "UPDATE tasks SET paused_turn_count = ?, updated_at = ? "
                        "WHERE task_id = ?",
                        (new_count, ts, row["task_id"]),
                    )
            conn.commit()
        return completed

    def set_paused(self, task_id: str, *, now: str | None = None) -> None:
        """把 task 标 paused,记录 paused_at 并重置 paused_turn_count=0。

        由 segmenter 判定 pause 时调(替代直接 ``UPDATE status='paused'``)。
        """
        ts = now or _now_iso()
        with self._lock, self._conn() as conn:
            conn.execute(
                """
                UPDATE tasks SET
                    status = 'paused',
                    paused_at = ?,
                    paused_turn_count = 0,
                    updated_at = ?
                WHERE task_id = ?
                """,
                (ts, ts, task_id),
            )
            conn.commit()

    def clear_paused(self, task_id: str, *, now: str | None = None) -> None:
        """task 离开 paused 状态(return_to)时调,清空 paused_at + paused_turn_count。"""
        ts = now or _now_iso()
        with self._lock, self._conn() as conn:
            conn.execute(
                """
                UPDATE tasks SET
                    paused_at = NULL,
                    paused_turn_count = 0,
                    updated_at = ?
                WHERE task_id = ?
                """,
                (ts, task_id),
            )
            conn.commit()

    # ---- 工具 ----

    def stats(self) -> dict[str, Any]:
        with self._lock, self._conn() as conn:
            tasks_row = conn.execute("SELECT COUNT(*) AS c FROM tasks").fetchone()
            msg_row = conn.execute("SELECT COUNT(*) AS c FROM task_messages").fetchone()
            active_row = conn.execute(
                "SELECT COUNT(*) AS c FROM tasks "
                "WHERE status IN ('ongoing', 'paused')"
            ).fetchone()
        return {
            "tasks_total": int(tasks_row["c"]) if tasks_row else 0,
            "messages_total": int(msg_row["c"]) if msg_row else 0,
            "active_tasks": int(active_row["c"]) if active_row else 0,
            "db_path": str(self._path),
        }

    def close(self) -> None:
        """关闭 SQLite 连接(测试用,清理 file lock)。

        正常业务路径用不到(每次 _conn() 都用 with 块关)。
        测试场景连续跑多个 TaskIndex 实例时,Windows 上 Python 3.14 的 GC
        不一定及时回收 sqlite3.Connection,导致下一个 test 的 tmp_path 删除失败。
        测试可以 ``with TaskIndex(path) as idx: ...`` 显式释放。
        """
        # 触发 GC 回收
        import gc
        gc.collect()

    def __enter__(self) -> "TaskIndex":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def new_task_id(prefix: str = "t") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _now_iso() -> str:
    """ISO 8601 UTC(精度到秒,本表都是日期粒度)。"""
    from datetime import datetime, UTC
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def _row_to_task(row: sqlite3.Row) -> TaskRecord:
    participants_raw = row["participants"] or "[]"
    try:
        participants = json.loads(participants_raw)
    except (TypeError, ValueError):
        participants = []
    return TaskRecord(
        task_id=row["task_id"],
        user_id=row["user_id"],
        category=row["category"],
        title=row["title"],
        status=row["status"],
        start_ts=row["start_ts"],
        end_ts=row["end_ts"],
        summary=row["summary"] or "",
        participants=participants,
        source_thread=row["source_thread"] or "",
        updated_at=row["updated_at"] or "",
        paused_at=row["paused_at"] if "paused_at" in row.keys() else None,
        paused_turn_count=int(row["paused_turn_count"] or 0)
        if "paused_turn_count" in row.keys() else 0,
    )


_DDL = """
CREATE TABLE IF NOT EXISTS tasks (
    task_id        TEXT PRIMARY KEY,
    user_id        TEXT NOT NULL,
    category       TEXT NOT NULL,
    title          TEXT NOT NULL,
    status         TEXT NOT NULL,
    start_ts       TEXT NOT NULL,
    end_ts         TEXT,
    summary        TEXT NOT NULL DEFAULT '',
    participants   TEXT NOT NULL DEFAULT '[]',
    source_thread  TEXT NOT NULL DEFAULT '',
    updated_at     TEXT NOT NULL,
    paused_at      TEXT,
    paused_turn_count INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_tasks_user_status
    ON tasks (user_id, status);
CREATE INDEX IF NOT EXISTS idx_tasks_start_ts
    ON tasks (start_ts);
CREATE INDEX IF NOT EXISTS idx_tasks_paused
    ON tasks (status, paused_turn_count)
    WHERE status = 'paused';

CREATE TABLE IF NOT EXISTS task_messages (
    task_id    TEXT NOT NULL,
    message_id TEXT NOT NULL,
    thread_id  TEXT NOT NULL,
    role       TEXT NOT NULL,
    user_id    TEXT,
    spoken_at  TEXT NOT NULL,
    content    TEXT NOT NULL,
    PRIMARY KEY (task_id, message_id)
);

CREATE INDEX IF NOT EXISTS idx_msg_thread
    ON task_messages (thread_id);
CREATE INDEX IF NOT EXISTS idx_msg_spoken_at
    ON task_messages (spoken_at);
"""


__all__ = [
    "TaskIndex",
    "TaskRecord",
    "TaskMessageRecord",
    "new_task_id",
]
