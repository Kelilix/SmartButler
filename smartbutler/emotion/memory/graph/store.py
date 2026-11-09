"""🆕 Phase 9:记忆图的存储层(SQLite)。

设计要点(见 ``docs/PHASE_9_GRAPH_MEMORY_DESIGN.md`` §6):

- **独立文件** ``data/graph.db`` —— 写负载(图高频批写)与 task 索引(低频更新)不同
- **不引入图数据库** —— SQLite + 递归 CTE 足够(个人管家场景,节点几百、边几千)
- **单连接 + Lock** —— 与 ``task_index.py`` 同样的简单可靠模式
- **边 id 哈希幂等** —— 同源同目标同事实 → 同 id → 判重命中不新增,只追加溯源

三张表:

- ``graph_entities`` —— 人物节点(带 aliases / user_id / voice_bound / summary)
- ``graph_edges``    —— 人物关系边(带四时间戳 + task_ids 溯源)
- ``graph_task_log`` —— 幂等日志(同一 task 不重复抽图)
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from smartbutler.emotion.memory.graph.models import GraphEdge, GraphNode
from smartbutler.utils.logging import get_logger

logger = get_logger(__name__)


def _now_iso() -> str:
    """当前时间(ISO 8601,带时区)。"""
    return datetime.now(UTC).isoformat()


_DDL = """
CREATE TABLE IF NOT EXISTS graph_entities (
    node_id      TEXT PRIMARY KEY,   -- "person:<user_id>" 或 "person:name:<hash>"
    name         TEXT NOT NULL,      -- 主显示名
    aliases      TEXT NOT NULL DEFAULT '[]',  -- JSON 数组:见过的所有称呼
    type         TEXT NOT NULL DEFAULT 'Person',
    user_id      TEXT,               -- 声纹锚点,未绑定时为 NULL
    voice_bound  INTEGER NOT NULL DEFAULT 0,
    summary      TEXT NOT NULL DEFAULT '',  -- 活跃边 fact 句聚合(用于向用户描述这个人)
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_entity_user  ON graph_entities(user_id);
CREATE INDEX IF NOT EXISTS idx_entity_name  ON graph_entities(name);
CREATE INDEX IF NOT EXISTS idx_entity_bound ON graph_entities(voice_bound);

CREATE TABLE IF NOT EXISTS graph_edges (
    edge_id       TEXT PRIMARY KEY,
    source_id     TEXT NOT NULL,
    target_id     TEXT NOT NULL,
    relation_type TEXT NOT NULL,
    fact          TEXT NOT NULL,
    speaker       TEXT NOT NULL DEFAULT '',   -- 说话的 user_id
    valid_at      TEXT,
    invalid_at    TEXT,
    created_at    TEXT NOT NULL,
    expired_at    TEXT,
    task_ids      TEXT NOT NULL DEFAULT '[]', -- 溯源(可多)
    embedding     BLOB
);
CREATE INDEX IF NOT EXISTS idx_edge_src  ON graph_edges(source_id);
CREATE INDEX IF NOT EXISTS idx_edge_tgt  ON graph_edges(target_id);
CREATE INDEX IF NOT EXISTS idx_edge_pair ON graph_edges(source_id, target_id);
CREATE INDEX IF NOT EXISTS idx_edge_live ON graph_edges(invalid_at);

CREATE TABLE IF NOT EXISTS graph_task_log (
    task_id      TEXT PRIMARY KEY,
    extracted_at TEXT NOT NULL,
    node_count   INTEGER NOT NULL DEFAULT 0,
    edge_count   INTEGER NOT NULL DEFAULT 0,
    status       TEXT NOT NULL DEFAULT 'ok'
);
"""


class GraphStore:
    """记忆图存储访问层(线程安全,单连接 + Lock)。

    用法::

        store = GraphStore(Path("data/graph.db"))
        store.upsert_node(GraphNode(...))
        for e in store.find_edges_by_pair("person:a", "person:b"):
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
        """每次返回新连接(SQLite 写并发由 Lock 串行化)。"""
        conn = sqlite3.connect(str(self._path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._lock, self._conn() as conn:
            conn.executescript(_DDL)
            conn.commit()

    # ------------------------------------------------------------------
    # 节点 CRUD
    # ------------------------------------------------------------------

    def upsert_node(self, node: GraphNode) -> None:
        """插入或更新节点(按 node_id)。

        ⚠️ **不覆盖** ``user_id`` / ``voice_bound`` —— 绑定声纹是**专门**的
        业务动作(见 ``binding.py``),普通 upsert 意外清掉声纹锚点会造成
        节点降级(已绑定的节点变回"未绑定"),后续同名询问会反复打扰用户。

        同理 ``aliases`` 是**合并**而非替换 —— 不同来源可能提到同一个人的
        不同称呼,替换会丢历史。
        """
        now = _now_iso()
        with self._lock, self._conn() as conn:
            existing = conn.execute(
                "SELECT aliases, user_id, voice_bound, created_at FROM graph_entities "
                "WHERE node_id = ?",
                (node.node_id,),
            ).fetchone()

            if existing is None:
                conn.execute(
                    """
                    INSERT INTO graph_entities
                        (node_id, name, aliases, type, user_id, voice_bound,
                         summary, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    node.to_row(),
                )
            else:
                old_aliases = _load_json_list(existing["aliases"])
                merged = _merge_names(old_aliases, [node.name, *node.aliases])
                user_id = node.user_id or existing["user_id"]
                voice_bound = 1 if (node.voice_bound or existing["voice_bound"]) else 0
                conn.execute(
                    """
                    UPDATE graph_entities SET
                        name = ?, aliases = ?, summary = ?,
                        user_id = ?, voice_bound = ?, updated_at = ?
                    WHERE node_id = ?
                    """,
                    (
                        node.name or existing["name"],
                        json.dumps(merged, ensure_ascii=False),
                        node.summary or "",
                        user_id,
                        voice_bound,
                        now,
                        node.node_id,
                    ),
                )
            conn.commit()

    def get_node(self, node_id: str) -> GraphNode | None:
        """按 node_id 取节点。"""
        with self._lock, self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM graph_entities WHERE node_id = ?", (node_id,)
            ).fetchone()
        return _row_to_node(row) if row else None

    def find_node_by_user_id(self, user_id: str) -> GraphNode | None:
        """按声纹 user_id 找节点(声纹绑定时用)。"""
        if not user_id:
            return None
        with self._lock, self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM graph_entities WHERE user_id = ? LIMIT 1", (user_id,)
            ).fetchone()
        return _row_to_node(row) if row else None

    def find_nodes_by_name(self, name: str) -> list[GraphNode]:
        """按名字找节点(查 ``name`` + ``aliases``)。

        匹配是**归一化后**的精确匹配 —— 保守策略,宁可漏合不误合。

        Args:
            name: 原始人名。

        Returns:
            命中的节点列表(可能为空)。
        """
        from smartbutler.emotion.memory.graph.models import normalize_person_name

        target = normalize_person_name(name)
        if not target:
            return []
        with self._lock, self._conn() as conn:
            rows = conn.execute("SELECT * FROM graph_entities").fetchall()

        out: list[GraphNode] = []
        for row in rows:
            node = _row_to_node(row)
            if node is None:
                continue
            for candidate in node.all_names():
                if normalize_person_name(candidate) == target:
                    out.append(node)
                    break
        return out

    def list_nodes(self) -> list[GraphNode]:
        """列出所有节点(召回时按名字匹配 query 用)。

        图规模小(个人管家场景几百个节点),全扫最简单可靠 ——
        不需要额外维护名字索引。
        """
        with self._lock, self._conn() as conn:
            rows = conn.execute("SELECT * FROM graph_entities").fetchall()
        out = [_row_to_node(r) for r in rows]
        return [n for n in out if n is not None]

    def add_alias(self, node_id: str, alias: str) -> None:
        """给节点追加一个别名(保序去重)。"""
        if not alias:
            return
        with self._lock, self._conn() as conn:
            row = conn.execute(
                "SELECT aliases FROM graph_entities WHERE node_id = ?", (node_id,)
            ).fetchone()
            if row is None:
                return
            merged = _merge_names(_load_json_list(row["aliases"]), [alias])
            conn.execute(
                "UPDATE graph_entities SET aliases = ?, updated_at = ? WHERE node_id = ?",
                (json.dumps(merged, ensure_ascii=False), _now_iso(), node_id),
            )
            conn.commit()

    def bind_voice(self, node_id: str, user_id: str) -> bool:
        """给节点绑定声纹 user_id。

        ⚠️ **node_id 永不变** —— 这是整个方案的技术基石(design §2.3)。
        绑定只补字段,所有已有边无需重连。

        Args:
            node_id: 目标节点。
            user_id: 声纹 user_id。

        Returns:
            True = 绑定成功;False = 节点不存在。
        """
        if not node_id or not user_id:
            return False
        with self._lock, self._conn() as conn:
            cur = conn.execute(
                "UPDATE graph_entities SET user_id = ?, voice_bound = 1, updated_at = ? "
                "WHERE node_id = ?",
                (user_id, _now_iso(), node_id),
            )
            conn.commit()
            return cur.rowcount > 0

    def merge_nodes(self, main_id: str, dup_id: str) -> bool:
        """合并两个节点:把 dup 的边和别名都转到 main,dup 删除。

        **主节点选择由调用方决定**(见 ``binding.py`` 的规则:
        优先 ``voice_bound=1`` → 其次边多 → 再次建得早)。
        本方法只负责执行合并,**main_id 保持不变**。

        Args:
            main_id: 主节点 id(保留)。
            dup_id: 被合并节点 id(删除)。

        Returns:
            True = 合并成功;False = 参数无效或节点不存在。
        """
        if not main_id or not dup_id or main_id == dup_id:
            return False
        with self._lock, self._conn() as conn:
            main = conn.execute(
                "SELECT * FROM graph_entities WHERE node_id = ?", (main_id,)
            ).fetchone()
            dup = conn.execute(
                "SELECT * FROM graph_entities WHERE node_id = ?", (dup_id,)
            ).fetchone()
            if main is None or dup is None:
                return False

            conn.execute(
                "UPDATE graph_edges SET source_id = ? WHERE source_id = ?",
                (main_id, dup_id),
            )
            conn.execute(
                "UPDATE graph_edges SET target_id = ? WHERE target_id = ?",
                (main_id, dup_id),
            )
            merged = _merge_names(
                _load_json_list(main["aliases"]),
                [main["name"], dup["name"], *_load_json_list(dup["aliases"])],
            )
            user_id = main["user_id"] or dup["user_id"]
            voice_bound = 1 if (main["voice_bound"] or dup["voice_bound"]) else 0
            conn.execute(
                "UPDATE graph_entities SET aliases = ?, user_id = ?, voice_bound = ?, "
                "updated_at = ? WHERE node_id = ?",
                (
                    json.dumps(merged, ensure_ascii=False),
                    user_id,
                    voice_bound,
                    _now_iso(),
                    main_id,
                ),
            )
            conn.execute("DELETE FROM graph_entities WHERE node_id = ?", (dup_id,))
            conn.commit()
            return True

    def count_edges(self, node_id: str) -> int:
        """节点关联的边数(合并时选主节点用)。"""
        with self._lock, self._conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM graph_edges "
                "WHERE source_id = ? OR target_id = ?",
                (node_id, node_id),
            ).fetchone()
        return int(row["n"]) if row else 0

    # ------------------------------------------------------------------
    # 边 CRUD
    # ------------------------------------------------------------------

    def upsert_edge(self, edge: GraphEdge) -> bool:
        """插入或追加边。

        - ``edge_id`` 已存在 → **只追加 task_ids**,不覆盖内容(零写压力)
        - ``edge_id`` 不存在 → 插入

        Returns:
            True = 新增;False = 已存在(仅追加溯源)。
        """
        with self._lock, self._conn() as conn:
            row = conn.execute(
                "SELECT task_ids FROM graph_edges WHERE edge_id = ?", (edge.edge_id,)
            ).fetchone()
            if row is None:
                conn.execute(
                    """
                    INSERT INTO graph_edges
                        (edge_id, source_id, target_id, relation_type, fact, speaker,
                         valid_at, invalid_at, created_at, expired_at, task_ids, embedding)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    edge.to_row(),
                )
                conn.commit()
                return True

            merged = _merge_names(_load_json_list(row["task_ids"]), edge.task_ids)
            conn.execute(
                "UPDATE graph_edges SET task_ids = ? WHERE edge_id = ?",
                (json.dumps(merged, ensure_ascii=False), edge.edge_id),
            )
            conn.commit()
            return False

    def get_edge(self, edge_id: str) -> GraphEdge | None:
        """按 edge_id 取边。"""
        with self._lock, self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM graph_edges WHERE edge_id = ?", (edge_id,)
            ).fetchone()
        return _row_to_edge(row) if row else None

    def find_edges_by_pair(
        self,
        source_id: str,
        target_id: str,
        *,
        only_active: bool = True,
    ) -> list[GraphEdge]:
        """查同端点的边(判重的核心操作)。

        对称关系的两个方向都要查 —— 我们只存一条有向边(design §3.4),
        但 "张伟是我朋友" 和 "我是张伟的朋友" 是同一条边。

        Args:
            source_id: 端点 A。
            target_id: 端点 B。
            only_active: 只返回未失效的边。

        Returns:
            命中的边列表。
        """
        with self._lock, self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM graph_edges "
                "WHERE (source_id = ? AND target_id = ?) "
                "   OR (source_id = ? AND target_id = ?) "
                "ORDER BY created_at",
                (source_id, target_id, target_id, source_id),
            ).fetchall()
        out = [_row_to_edge(r) for r in rows]
        edges = [e for e in out if e is not None]
        if only_active:
            edges = [e for e in edges if e.is_active]
        return edges

    def invalidate_edge(
        self,
        edge_id: str,
        *,
        invalid_at: str,
        expired_at: str,
    ) -> bool:
        """把边标记为失效(矛盾消解)。

        ⚠️ **只加时间戳,绝不 DELETE** —— 历史事实必须保留。
        "张女士以前是我朋友吗" 这类问题需要历史边。

        Args:
            edge_id: 目标边。
            invalid_at: 世界时间 —— 关系在现实中终止的时间。
            expired_at: 系统时间 —— 边在系统中被替换的时间。

        Returns:
            True = 成功;False = 边不存在。
        """
        with self._lock, self._conn() as conn:
            cur = conn.execute(
                "UPDATE graph_edges SET invalid_at = ?, expired_at = ? WHERE edge_id = ?",
                (invalid_at, expired_at, edge_id),
            )
            conn.commit()
            return cur.rowcount > 0

    def neighbors(
        self,
        node_id: str,
        *,
        depth: int = 1,
        only_active: bool = True,
    ) -> list[str]:
        """查 N 跳邻居节点 id(递归 CTE)。

        Args:
            node_id: 起点节点。
            depth: 跳数(1 = 直接邻居)。
            only_active: 只走活跃边(``invalid_at IS NULL``)。

        Returns:
            邻居节点 id 列表(不含起点)。
        """
        if depth < 1:
            return []
        active_clause = "AND e.invalid_at IS NULL" if only_active else ""
        # ⚠️ 外层必须排除起点:递归会把起点以更大 depth 重新加入
        # (UNION 只按整行去重,(a,0) 与 (a,2) 是不同的行)。
        sql = f"""
            WITH RECURSIVE reach(node_id, depth) AS (
                SELECT ?, 0
                UNION
                SELECT CASE WHEN e.source_id = r.node_id
                            THEN e.target_id ELSE e.source_id END,
                       r.depth + 1
                FROM graph_edges e
                JOIN reach r ON e.source_id = r.node_id OR e.target_id = r.node_id
                WHERE r.depth < ? {active_clause}
            )
            SELECT DISTINCT node_id FROM reach
            WHERE depth > 0 AND node_id != ?
        """
        with self._lock, self._conn() as conn:
            rows = conn.execute(sql, (node_id, depth, node_id)).fetchall()
        return [r["node_id"] for r in rows]

    def find_edges_for_node(
        self,
        node_id: str,
        *,
        only_active: bool = True,
        limit: int = 100,
    ) -> list[GraphEdge]:
        """查与某节点相关的所有边(召回时用)。

        Args:
            node_id: 节点 id。
            only_active: 只返回活跃边。
            limit: 最大返回条数。

        Returns:
            边列表。
        """
        sql = "SELECT * FROM graph_edges WHERE (source_id = ? OR target_id = ?) "
        params: list[Any] = [node_id, node_id]
        if only_active:
            sql += "AND invalid_at IS NULL "
        sql += "ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self._lock, self._conn() as conn:
            rows = conn.execute(sql, params).fetchall()
        out = [_row_to_edge(r) for r in rows]
        return [e for e in out if e is not None]

    def append_edge_task(self, edge_id: str, task_id: str) -> None:
        """给边追加一个溯源 task_id(保序去重)。"""
        if not edge_id or not task_id:
            return
        with self._lock, self._conn() as conn:
            row = conn.execute(
                "SELECT task_ids FROM graph_edges WHERE edge_id = ?", (edge_id,)
            ).fetchone()
            if row is None:
                return
            merged = _merge_names(_load_json_list(row["task_ids"]), [task_id])
            conn.execute(
                "UPDATE graph_edges SET task_ids = ? WHERE edge_id = ?",
                (json.dumps(merged, ensure_ascii=False), edge_id),
            )
            conn.commit()

    # ------------------------------------------------------------------
    # 幂等日志
    # ------------------------------------------------------------------

    def log_task(
        self,
        task_id: str,
        *,
        node_count: int = 0,
        edge_count: int = 0,
        status: str = "ok",
    ) -> None:
        """记录 task 的抽取结果(幂等依据)。"""
        with self._lock, self._conn() as conn:
            conn.execute(
                """
                INSERT INTO graph_task_log
                    (task_id, extracted_at, node_count, edge_count, status)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    extracted_at=excluded.extracted_at,
                    node_count=excluded.node_count,
                    edge_count=excluded.edge_count,
                    status=excluded.status
                """,
                (task_id, _now_iso(), node_count, edge_count, status),
            )
            conn.commit()

    def has_task(self, task_id: str) -> bool:
        """该 task 是否已抽过图(幂等检查)。"""
        if not task_id:
            return False
        with self._lock, self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM graph_task_log WHERE task_id = ?", (task_id,)
            ).fetchone()
        return row is not None

    def get_task_log(self, task_id: str) -> dict[str, Any] | None:
        """取 task 的抽取日志。"""
        with self._lock, self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM graph_task_log WHERE task_id = ?", (task_id,)
            ).fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------------
    # 统计(观测用)
    # ------------------------------------------------------------------

    def stats(self) -> dict[str, int]:
        """图规模统计。"""
        with self._lock, self._conn() as conn:
            nodes = conn.execute("SELECT COUNT(*) AS n FROM graph_entities").fetchone()
            edges = conn.execute("SELECT COUNT(*) AS n FROM graph_edges").fetchone()
            live = conn.execute(
                "SELECT COUNT(*) AS n FROM graph_edges WHERE invalid_at IS NULL"
            ).fetchone()
            tasks = conn.execute("SELECT COUNT(*) AS n FROM graph_task_log").fetchone()
            bound = conn.execute(
                "SELECT COUNT(*) AS n FROM graph_entities WHERE voice_bound = 1"
            ).fetchone()
        return {
            "nodes": int(nodes["n"]) if nodes else 0,
            "edges": int(edges["n"]) if edges else 0,
            "active_edges": int(live["n"]) if live else 0,
            "tasks": int(tasks["n"]) if tasks else 0,
            "voice_bound_nodes": int(bound["n"]) if bound else 0,
        }

    # ------------------------------------------------------------------
    # 清空(运维用)
    # ------------------------------------------------------------------

    def clear_all(self) -> dict[str, int]:
        """清空图的全部数据(节点 + 边 + 幂等日志)。

        ⚠️ **危险操作** —— 与 ``GraphEdge.invalid_at`` 的"非破坏性失效"
        策略相反:这里是真的删。因为图是**可重建的增强层**(从 fact 层
        重新抽一遍就能恢复),但也意味着清空后**声纹绑定关系会丢失**
        (节点上的 ``user_id`` / ``voice_bound`` 字段一并没了)。
        声纹库本身不受影响,重新录入声纹后可以再次绑定。

        三张表全清 + VACUUM 释放磁盘。

        Returns:
            删掉的行数统计 ``{"entities": n, "edges": n, "task_log": n}``。
        """
        counts = {"entities": 0, "edges": 0, "task_log": 0}
        with self._lock, self._conn() as conn:
            for table in ("graph_entities", "graph_edges", "graph_task_log"):
                n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                conn.execute(f"DELETE FROM {table}")
                counts[table.replace("graph_", "")] = int(n)
            conn.commit()
            # VACUUM 释放磁盘(失败不致命,数据已清)
            try:
                conn.execute("VACUUM")
            except Exception as e:  # noqa: BLE001
                logger.warning("graph_store.vacuum_failed err=%s", e)
        return counts


# ---------------------------------------------------------------------------
# 行 <-> 模型
# ---------------------------------------------------------------------------


def _row_to_node(row: sqlite3.Row) -> GraphNode | None:
    try:
        return GraphNode(
            node_id=row["node_id"],
            name=row["name"],
            aliases=_load_json_list(row["aliases"]),
            type=row["type"],
            user_id=row["user_id"],
            voice_bound=bool(row["voice_bound"]),
            summary=row["summary"] or "",
            created_at=row["created_at"] or "",
            updated_at=row["updated_at"] or "",
        )
    except (IndexError, KeyError, TypeError):
        return None


def _row_to_edge(row: sqlite3.Row) -> GraphEdge | None:
    try:
        return GraphEdge(
            edge_id=row["edge_id"],
            source_id=row["source_id"],
            target_id=row["target_id"],
            relation_type=row["relation_type"],
            fact=row["fact"],
            speaker=row["speaker"] or "",
            valid_at=row["valid_at"],
            invalid_at=row["invalid_at"],
            created_at=row["created_at"] or "",
            expired_at=row["expired_at"],
            task_ids=_load_json_list(row["task_ids"]),
            embedding=row["embedding"],
        )
    except (IndexError, KeyError, TypeError):
        return None


def _load_json_list(raw: Any) -> list[str]:
    """解析 JSON 数组字符串(容忍脏数据)。"""
    if not raw:
        return []
    if isinstance(raw, (list, tuple)):
        return [str(x) for x in raw]
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return [str(x) for x in data]


def _merge_names(existing: Iterable[str], incoming: Iterable[str]) -> list[str]:
    """合并名字列表(保序去重)。"""
    out: list[str] = []
    for name in [*existing, *incoming]:
        if name and name not in out:
            out.append(name)
    return out


__all__ = ["GraphStore"]
