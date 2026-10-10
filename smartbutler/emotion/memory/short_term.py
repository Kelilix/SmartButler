"""短期记忆（按 README Phase 6.1 + Phase 4b）。

用 LangGraph 的 ``SqliteSaver`` 把 LangGraph checkpointer 状态落到 SQLite。
这样：

1. **进程退出再回来不丢上下文**——README Phase 6.1 不变量 1 明确"短期 = checkpointer + SQLite"。
2. **Phase 6b Proactive 真实化时能查"最近一次对话"**——InMemorySaver 重启就丢,
   满足不了"管家看到用户刚才说心情不好,主动关心"这种场景。

设计要点：
1. **薄包装** —— ShortTermMemory 把 SqliteSaver 包装成一个 manager 对象,
   暴露 ``get_saver()`` 给 ButlerOrchestrator 用,以及 ``cleanup_expired()`` 维护接口。
2. **可降级** —— 如果 langgraph-checkpoint-sqlite 装不上(罕见,装包失败),
   自动回退 InMemorySaver 并 logger.warning(不崩)。
3. **TTL by thread** —— 不做全局 TTL;每个 thread 的状态由 LangGraph checkpointer
   自己管理。如果将来要"30 天前的对话自动清",在 cleanup_expired() 里按时间戳删。
4. **多文件路径** —— 默认 ``data/short_term.db``,跟 storage.sqlite_path 错开,
   避免和长期情景记忆的 SQLite JSON 混在一个文件。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from smartbutler.utils.logging import get_logger

logger = get_logger(__name__)

# 软依赖:langgraph-checkpoint-sqlite 失败时降级 InMemorySaver
try:
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    _SQLITE_SAVER_AVAILABLE = True
except ImportError:  # pragma: no cover - 降级路径
    _SQLITE_SAVER_AVAILABLE = False
    logger.warning(
        "langgraph-checkpoint-sqlite 未装,短期记忆降级为 InMemorySaver,"
        "重启即丢上下文(Phase 6.1 不变量 1 不满足)。"
    )

# 降级后备
try:
    from langgraph.checkpoint.memory import InMemorySaver

    _MEMORY_SAVER_AVAILABLE = True
except ImportError:  # pragma: no cover
    _MEMORY_SAVER_AVAILABLE = False


class ShortTermMemory:
    """短期记忆管理器（接 LangGraph checkpointer）。

    典型用法::

        stm = ShortTermMemory(Path("data/short_term.db"))
        async with stm.get_saver() as saver:
            graph = builder.compile(checkpointer=saver)
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._async_held: "_AsyncSqliteSaverHeld | None" = None

    @property
    def is_persistent(self) -> bool:
        """True=SqliteSaver(持久),False=InMemorySaver(降级)。"""
        return _SQLITE_SAVER_AVAILABLE

    @contextmanager
    def get_saver(self) -> Iterator[Any]:
        """返回 LangGraph checkpointer 实例。

        注意:AsyncSqliteSaver 是 async context manager,本方法是同步的
        contextmanager 包装 —— 调用方在 ``async with stm.get_saver()`` 内部
        ``async with`` 内部使用会出错。正确用法见 ``get_async_saver()``。
        """
        if _SQLITE_SAVER_AVAILABLE:
            from langgraph.checkpoint.sqlite import SqliteSaver

            with SqliteSaver.from_conn_string(str(self._path)) as saver:
                yield saver
                return
        # 降级
        if _MEMORY_SAVER_AVAILABLE:
            yield InMemorySaver()
            return
        raise RuntimeError("无可用 checkpointer（既没 SqliteSaver 也没 InMemorySaver）")

    async def get_async_saver(self) -> Any:
        """返回 ``AsyncSqliteSaver`` 实例(供 LangGraph async 图用)。

        与 ``get_sync_saver`` 对称:内部用 ``__aenter__`` 立刻完成 context manager
        初始化,持有关联 connection,调用方直接 await ``aput / aget_tuple``。
        进程结束前需调 ``aclose()`` 释放连接。

        失败时降级 InMemorySaver(进程内,重启即丢)。
        """
        if _SQLITE_SAVER_AVAILABLE:
            held = _AsyncSqliteSaverHeld(self._path)
            saver = await held.__aenter__()
            # 持有 held 以便 close() / aclose() 释放连接
            self._async_held = held
            return saver
        if _MEMORY_SAVER_AVAILABLE:
            logger.warning("短期记忆降级为 InMemorySaver(重启即丢)")
            return InMemorySaver()
        raise RuntimeError("无可用 checkpointer")

    def get_sync_saver(self) -> Any:
        """返回同步 SqliteSaver 实例（Phase 6.2 P0,供 ButlerOrchestrator._ensure_graph 用）。

        LangGraph 1.x 的 ``compile(checkpointer=...)`` 接受 BaseCheckpointSaver 实例,
        同步 saver 可直接传入,不需要 async context manager。

        关键陷阱:LangGraph 的 ``SqliteSaver.from_conn_string`` 是 ``@contextmanager`` 装饰的,
        ``__enter__`` 返回的 saver 实例**依赖外层 with 块的 conn 句柄**,出了 with 块 conn 就 close。
        所以我们必须持有这个 CM,延迟 __exit__ 到进程退出。

        降级路径:SqliteSaver 不可用时回退 InMemorySaver(进程内)。
        """
        if _SQLITE_SAVER_AVAILABLE:
            from langgraph.checkpoint.sqlite import SqliteSaver

            # 持有 CM 句柄,延迟 __exit__ 到 close() 时
            cm = SqliteSaver.from_conn_string(str(self._path))
            saver = cm.__enter__()
            # 把 cm 挂到 saver 上,close() 时一并释放
            self._cm = cm
            return saver
        if _MEMORY_SAVER_AVAILABLE:
            logger.warning(
                "短期记忆降级为 InMemorySaver(重启即丢):"
                "langgraph-checkpoint-sqlite 未装"
            )
            return InMemorySaver()
        raise RuntimeError("无可用 checkpointer")

    def close(self) -> None:
        """释放 SqliteSaver 的 context manager(Phase 6.2 P0)。

        同步版本:释放 ``get_sync_saver`` 持有的 ``SqliteSaver`` 句柄。
        如果已通过 ``get_async_saver()`` 拿到 async saver,需在事件循环里
        调 ``aclose()``(see :meth:`aclose`)。
        """
        cm = getattr(self, "_cm", None)
        if cm is not None:
            try:
                cm.__exit__(None, None, None)
            except Exception as e:  # pragma: no cover
                logger.warning("ShortTermMemory.close failed: %s", e)
            self._cm = None

    async def aclose(self) -> None:
        """释放 ``get_async_saver()`` 持有的 ``AsyncSqliteSaver`` 句柄(Phase 6.2 P0)。

        必须在事件循环里调(同步 close() 不释放 async 句柄)。
        """
        held = getattr(self, "_async_held", None)
        if held is not None:
            await held.aclose()
            self._async_held = None

    async def cleanup_expired(self, older_than_days: int = 30) -> int:
        """清理 N 天前的 checkpointer 历史。

        LangGraph SqliteSaver 用 blob 存,清理需要查 checkpoints 表。
        简化版:用 aiosqlite 直接连,删掉 created_at < cutoff 的行。

        Returns:
            删除的 checkpoint 条数。
        """
        if not _SQLITE_SAVER_AVAILABLE:
            return 0
        import time

        import aiosqlite

        cutoff = time.time() - older_than_days * 86400
        try:
            async with aiosqlite.connect(str(self._path)) as conn:
                # LangGraph SqliteSaver 表结构(checkpoints + writes),先看实际表名
                async with conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ) as cur:
                    tables = [row[0] for row in await cur.fetchall()]
                if "checkpoints" not in tables:
                    return 0
                async with conn.execute(
                    "DELETE FROM checkpoints WHERE thread_id IN "
                    "(SELECT DISTINCT thread_id FROM checkpoints "
                    "WHERE thread_ts < ?)",
                    (cutoff,),
                ) as cur:
                    deleted = cur.rowcount
                await conn.commit()
                return deleted
        except Exception as e:  # pragma: no cover - 维护接口
            logger.warning("cleanup_expired failed: %s", e)
            return 0


class _AsyncSqliteSaverWrapper:
    """AsyncSqliteSaver 的薄包装。

    LangGraph 1.x 的 AsyncSqliteSaver 需要 ``from_conn_string`` 返回的 context manager
    在 async with 内使用。我们把它的 setup() 调好再返回,让调用方直接 ``await
    wrapper.get()`` / ``await wrapper.aput()``。
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._saver: Any | None = None
        self._cm: Any | None = None

    async def __aenter__(self) -> _AsyncSqliteSaverWrapper:
        self._cm = AsyncSqliteSaver.from_conn_string(str(self._path))
        self._saver = await self._cm.__aenter__()
        await self._saver.setup()  # 建表
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._cm is not None:
            await self._cm.__aexit__(*exc)
            self._cm = None
            self._saver = None

    def __getattr__(self, name: str) -> Any:
        """透传所有属性到 AsyncSqliteSaver 实例。"""
        if self._saver is None:
            raise RuntimeError(
                "AsyncSqliteSaver 未初始化 —— 必须在 'async with wrapper' 内使用"
            )
        return getattr(self._saver, name)


class _AsyncSqliteSaverHeld:
    """``AsyncSqliteSaver`` 长生命周期持有版(Phase 6.2 P0)。

    解决 ``from_conn_string`` 必须 ``async with`` 才有连接的问题:
    我们 ``await __aenter__()`` 一次,把 cm 句柄挂在自身上,让调用方可以像用
    普通 saver 一样直接 ``await saver.aput(...)``,无需每次套一层 ``async with``。
    进程退出前由 orchestrator 调 ``await aclose()`` 释放。

    用法::

        held = _AsyncSqliteSaverHeld(path)
        saver = await held.__aenter__()
        # ... 用 saver ...
        await held.aclose()
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._cm: Any | None = None
        self._saver: Any | None = None

    async def __aenter__(self) -> Any:
        """立刻完成 ``AsyncSqliteSaver.from_conn_string`` 的 CM 初始化。

        Returns:
            ``AsyncSqliteSaver`` 实例,可直接用 ``aput / aget_tuple / setup``。
        """
        if self._saver is not None:
            return self._saver
        self._cm = AsyncSqliteSaver.from_conn_string(str(self._path))
        self._saver = await self._cm.__aenter__()
        await self._saver.setup()  # 建表(幂等)
        return self._saver

    async def aclose(self) -> None:
        """释放 connection(由 ShortTermMemory.aclose_async 统一调用)。"""
        if self._cm is None:
            return
        try:
            await self._cm.__aexit__(None, None, None)
        except Exception as e:  # pragma: no cover
            logger.warning("_AsyncSqliteSaverHeld.aclose failed: %s", e)
        self._cm = None
        self._saver = None

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()


__all__ = ["ShortTermMemory"]
