"""Vacuum + 容量治理(Phase 6.3 P1,见 §2.5.1.16)。

3 阶段状态机::

    active ──(forget)──> deleted_recoverable ──(7 天)──> archived ──(30 天)──> purged(物理删)
       ↑                       ↑                              ↑                       ↑
    recall 可见        recall 不可见,             SQLite 备份表              真的没了
                       restore() 可恢复         recall 不可见,
                                                restore() 仍可恢复
                                                (从 SQLite 还原 + re-embed)

关键不变量(全文遵守):
1. 30 天反悔期 —— 用户的"忘掉 X" 30 天内可恢复
2. archived 必须能 restore —— 走 SQLite 备份,Qdrant 已删的重新 embed + upsert 回去
3. purge_now 必须 confirm="yes" —— 防止脚本/UI 误操作
4. vacuum 必须支持 dry_run —— 所有自动化操作都先 dry-run 看报告
5. vacuum 不阻塞 recall —— 异步跑(本阶段是同步,后续可改 async 不破签名)
6. archived 不进 Qdrant recall 路径 —— 恢复时显式调 restore(),不自动复活
7. vacuum_stats() 永远可调 —— 不论 vacuum 跑没跑过,状态可查

默认行为:
- vacuum 调度**默认关闭** —— 用户手动调 CLI / API
- 开启方式:env `SMARTBUTLER_MEMORY_VACUUM_AUTO_ENABLED=true`
- 频率:env `SMARTBUTLER_MEMORY_VACUUM_AUTO_CRON=weekly`(默认 weekly)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, UTC, timedelta
from typing import Any

from smartbutler.capabilities.embedding.base import Embedder, EmbedderError
from smartbutler.emotion.memory.archive_store import ArchiveStore, ArchivedMemory
from smartbutler.emotion.memory.forgetting import ForgetReason
from smartbutler.emotion.memory.levels import (
    ARCHIVED_RETENTION_DAYS,
    DELETED_RECOVERABLE_DAYS,
)
from smartbutler.emotion.memory.long_term import (
    LongTermStore,
    PAYLOAD_DELETED,
    PAYLOAD_DELETED_AT,
    PAYLOAD_DELETED_REASON,
    PAYLOAD_LEVEL,
)
from smartbutler.utils.logging import get_logger

logger = get_logger(__name__)


@dataclass
class VacuumReport:
    """单次 vacuum 报告。"""

    scanned_total: int
    to_archive: int
    to_purge: int
    archived_now: int
    purged_now: int
    dry_run: bool
    scanned_at: datetime
    error_keys: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scanned_total": self.scanned_total,
            "to_archive": self.to_archive,
            "to_purge": self.to_purge,
            "archived_now": self.archived_now,
            "purged_now": self.purged_now,
            "dry_run": self.dry_run,
            "scanned_at": self.scanned_at.isoformat(),
            "error_keys": list(self.error_keys),
        }


@dataclass
class DeletedMemoryEntry:
    """list_deleted / list_forgotten 用的条目。"""

    key: str
    content: str
    importance: float
    level: str
    created_at: str
    deleted_at: str
    deleted_reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "content": self.content,
            "importance": self.importance,
            "level": self.level,
            "created_at": self.created_at,
            "deleted_at": self.deleted_at,
            "deleted_reason": self.deleted_reason,
        }


class VacuumService:
    """3 阶段状态机 + 4 个手动 API + VacuumReport。"""

    def __init__(
        self,
        *,
        store: LongTermStore,
        archive: ArchiveStore,
        embedder: Embedder | None = None,
    ) -> None:
        self._store = store
        self._archive = archive
        # embedder 可选 —— restore 才需要
        self._embedder = embedder

    # ------------------- 主入口 -------------------

    def vacuum_once(
        self,
        *,
        recoverable_age_days: int = DELETED_RECOVERABLE_DAYS,
        archive_age_days: int = ARCHIVED_RETENTION_DAYS,
        dry_run: bool = False,
        now: datetime | None = None,
    ) -> VacuumReport:
        """跑一次 vacuum。

        Args:
            recoverable_age_days: deleted_recoverable → archived 的年龄阈值(默认 7)。
            archive_age_days: archived → purged 的保留期(默认 30)。
            dry_run: True = 只统计,不动数据(默认 False)。
            now: 当前时间(测试时可注入)。

        Returns:
            :class:`VacuumReport`。
        """
        now = now or datetime.now(UTC)
        recoverable_cutoff = (now - timedelta(days=recoverable_age_days)).isoformat()
        purge_cutoff = (now - timedelta(days=archive_age_days)).isoformat()

        report = VacuumReport(
            scanned_total=0,
            to_archive=0,
            to_purge=0,
            archived_now=0,
            purged_now=0,
            dry_run=dry_run,
            scanned_at=now,
        )

        # 1. 扫所有软删除条目
        all_items = self._store.list_keys_by_level(include_deleted=True, limit=50_000)
        report.scanned_total = len(all_items)

        to_archive_keys: list[dict[str, Any]] = []
        for it in all_items:
            if not it.get("deleted"):
                continue
            deleted_at = it.get("created_at", "")  # list_keys_by_level 不返 deleted_at
            # 用 get_payload 拿完整字段
            pl = self._store.get_payload(it["key"])
            if pl is None or not pl.get(PAYLOAD_DELETED):
                continue
            deleted_at = pl.get(PAYLOAD_DELETED_AT, "")
            if deleted_at and deleted_at < recoverable_cutoff:
                to_archive_keys.append({
                    "key": it["key"],
                    "payload": pl,
                })
        report.to_archive = len(to_archive_keys)

        # 2. 扫 archived SQLite,准备 purged
        archived_items = self._archive.list_all(older_than_days=archive_age_days)
        to_purge_keys = [it.key for it in archived_items]
        report.to_purge = len(to_purge_keys)

        if dry_run:
            logger.info(
                "vacuum.dry_run scanned=%d to_archive=%d to_purge=%d",
                report.scanned_total, report.to_archive, report.to_purge,
            )
            return report

        # 3. 真跑:deleted_recoverable → archived
        for item in to_archive_keys:
            try:
                self._archive_one(item["key"], item["payload"])
                # QdrantStorage.delete 是 async 包装 sync 内部,这里直接走 sync
                # 实现(避免在 sync 上下文里折腾 event loop)
                self._purge_via_sync_api(item["key"])
                report.archived_now += 1
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "vacuum.archive_failed key=%s err=%s", item["key"], e,
                )
                report.error_keys.append(item["key"])

        # 4. 真跑:archived → purged
        for key in to_purge_keys:
            try:
                if self._archive.remove(key):
                    report.purged_now += 1
            except Exception as e:  # noqa: BLE001
                logger.warning("vacuum.purge_failed key=%s err=%s", key, e)
                report.error_keys.append(key)

        # 5. SQLite VACUUM(物理回收空间)
        if report.purged_now > 0:
            self._archive.vacuum()

        logger.info(
            "vacuum.done scanned=%d archived=%d purged=%d errors=%d",
            report.scanned_total, report.archived_now, report.purged_now,
            len(report.error_keys),
        )
        return report

    # ------------------- 4 个手动 API -------------------

    def list_deleted(
        self,
        *,
        level: str = "any",
        limit: int = 100,
        older_than_days: int = 0,
    ) -> list[DeletedMemoryEntry]:
        """列出软删除的记忆(运维/UI 用)。

        Args:
            level: "any" / "daily" / "weekly" / "monthly" / "permanent"。
            limit: 最多返多少条。
            older_than_days: 只返软删除时间早于 now - N 天的(0 = 全部)。

        Returns:
            :class:`DeletedMemoryEntry` 列表。
        """
        all_items = self._store.list_keys_by_level(
            level=None if level == "any" else level,
            include_deleted=True,
            limit=50_000,
        )
        out: list[DeletedMemoryEntry] = []
        now = datetime.now(UTC)
        for it in all_items:
            if not it.get("deleted"):
                continue
            pl = self._store.get_payload(it["key"])
            if pl is None or not pl.get(PAYLOAD_DELETED):
                continue
            deleted_at = pl.get(PAYLOAD_DELETED_AT, "")
            if older_than_days > 0 and deleted_at:
                try:
                    d = datetime.fromisoformat(deleted_at)
                    if d.tzinfo is None:
                        d = d.replace(tzinfo=UTC)
                    if d > (now - timedelta(days=older_than_days)):
                        continue
                except ValueError:
                    pass
            out.append(DeletedMemoryEntry(
                key=it["key"],
                content=it.get("content", ""),
                importance=it.get("importance", 0.0),
                level=it.get("level", ""),
                created_at=it.get("created_at", ""),
                deleted_at=deleted_at,
                deleted_reason=pl.get("deleted_reason", ""),
            ))
            if len(out) >= limit:
                break
        return out

    def count_deleted_by_age(
        self,
        *,
        level: str = "any",
    ) -> dict[str, int]:
        """按"软删除时长"分桶统计 deleted_recoverable + archived。

        分桶(以 deleted_at 到 now 的天数):
        - "<7d"    —— 新软删(还在 deleted_recoverable 状态,真空吸中)
        - "7-30d"  —— 该走 archived 了(vacuum 该处理)
        - "30-90d" —— archived 已超过 30 天,该 purged
        - "90-180d"
        - "180-365d"
        - ">365d"  —— 真的很久了

        Args:
            level: "any" / "daily" / "weekly" / "monthly" / "permanent"。

        Returns:
            ``{"<7d": int, "7-30d": int, "30-90d": int, "90-180d": int,
               "180-365d": int, ">365d": int, "_total": int}``
        """
        now = datetime.now(UTC)
        buckets: dict[str, int] = {
            "<7d": 0,
            "7-30d": 0,
            "30-90d": 0,
            "90-180d": 0,
            "180-365d": 0,
            ">365d": 0,
            "_total": 0,
        }
        all_items = self._store.list_keys_by_level(
            level=None if level == "any" else level,
            include_deleted=True,
            limit=50_000,
        )
        for it in all_items:
            if not it.get("deleted"):
                continue
            pl = self._store.get_payload(it["key"])
            if pl is None or not pl.get(PAYLOAD_DELETED):
                continue
            deleted_at = pl.get(PAYLOAD_DELETED_AT, "")
            if not deleted_at:
                buckets["<7d"] += 1
                buckets["_total"] += 1
                continue
            try:
                d = datetime.fromisoformat(deleted_at)
                if d.tzinfo is None:
                    d = d.replace(tzinfo=UTC)
            except ValueError:
                buckets["<7d"] += 1
                buckets["_total"] += 1
                continue
            age_days = (now - d).total_seconds() / 86400
            if age_days < 7:
                buckets["<7d"] += 1
            elif age_days < 30:
                buckets["7-30d"] += 1
            elif age_days < 90:
                buckets["30-90d"] += 1
            elif age_days < 180:
                buckets["90-180d"] += 1
            elif age_days < 365:
                buckets["180-365d"] += 1
            else:
                buckets[">365d"] += 1
            buckets["_total"] += 1
        return buckets

    def bulk_purge_before(
        self,
        *,
        older_than_days: int,
        confirm: str = "no",
    ) -> dict[str, Any]:
        """按"软删除时间"批量物理删(Qdrant 主体里的 deleted_recoverable)。

        这是你"按时间删除"的核心入口:
        - 跳过 7 天内的(用户反悔期)
        - 跳过 archived 的(走 vacuum 不在这里)
        - 一键清掉所有 7+N 天的软删条目

        Args:
            older_than_days: 只删 deleted_at < now - N 天的(必须 ≥ 7,否则 0 物理删拒执行)
            confirm: 必须是 ``"yes"`` 才执行。

        Returns:
            ``{"requested": int, "purged": int, "skipped": int, "errors": list[str],
               "dry_run": bool}``。
        """
        if confirm != "yes":
            return {
                "requested": 0, "purged": 0, "skipped": 0,
                "errors": ["confirm != 'yes'"], "dry_run": True,
            }
        if older_than_days < 7:
            return {
                "requested": 0, "purged": 0, "skipped": 0,
                "errors": [f"older_than_days={older_than_days} < 7 (反悔期保护)"],
                "dry_run": False,
            }
        # 扫所有 deleted_recoverable
        items = self.list_deleted(level="any", limit=50_000, older_than_days=older_than_days)
        purged: list[str] = []
        errors: list[str] = []
        for it in items:
            try:
                if self._purge_via_sync_api(it.key):
                    purged.append(it.key)
                else:
                    errors.append(f"not_found: {it.key}")
            except Exception as e:  # noqa: BLE001
                errors.append(f"{it.key}: {e}")
        logger.warning(
            "bulk_purge_before older_than=%dd purged=%d errors=%d",
            older_than_days, len(purged), len(errors),
        )
        return {
            "requested": len(items),
            "purged": len(purged),
            "skipped": len(items) - len(purged),
            "errors": errors,
            "dry_run": False,
        }

    def bulk_purge_archived(
        self,
        *,
        older_than_days: int = ARCHIVED_RETENTION_DAYS,
        confirm: str = "no",
    ) -> dict[str, Any]:
        """按"archived 时长"批量物理删 archived SQLite 里的条目。

        默认 30 天(双时间窗的反悔期),但允许"我就要现在全清"的强场景。

        Args:
            older_than_days: archived_at < now - N 天的全清(默认 30)。
            confirm: 必须是 ``"yes"`` 才执行。

        Returns:
            ``{"requested": int, "purged": int, "errors": list[str], "dry_run": bool}``。
        """
        if confirm != "yes":
            return {
                "requested": 0, "purged": 0, "errors": ["confirm != 'yes'"],
                "dry_run": True,
            }
        items = self._archive.list_all(older_than_days=older_than_days, limit=50_000)
        purged: list[str] = []
        errors: list[str] = []
        for it in items:
            try:
                if self._archive.remove(it.key):
                    purged.append(it.key)
            except Exception as e:  # noqa: BLE001
                errors.append(f"{it.key}: {e}")
        if purged:
            self._archive.vacuum()  # SQLite 物理回收空间
        logger.warning(
            "bulk_purge_archived older_than=%dd purged=%d errors=%d",
            older_than_days, len(purged), len(errors),
        )
        return {
            "requested": len(items),
            "purged": len(purged),
            "errors": errors,
            "dry_run": False,
        }

    def restore(self, key: str) -> bool:
        """恢复软删除的事实(从 Qdrant 主体恢复)。

        Args:
            key: 记忆 key。

        Returns:
            True = 成功恢复(Qdrant 主体 / archived SQLite 都可),False = 找不到。
        """
        # 先看 Qdrant 主体
        pl = self._store.get_payload(key)
        if pl is not None and pl.get(PAYLOAD_DELETED):
            return self._restore_sync(key)

        # Qdrant 没有(可能已 archived)→ 看 SQLite
        item = self._archive.get(key)
        if item is None:
            logger.warning("vacuum.restore_not_found key=%s", key)
            return False
        return self._restore_from_archive(item)

    def _restore_sync(self, key: str) -> bool:
        """从 Qdrant 主体恢复(原地 restore)。"""
        import asyncio
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # 已在 running loop → 走 thread pool
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                    future = ex.submit(asyncio.run, self._store.restore_deleted(key))
                    return future.result(timeout=10)
            return loop.run_until_complete(self._store.restore_deleted(key))
        except RuntimeError:
            return asyncio.run(self._store.restore_deleted(key))

    def _restore_from_archive(self, item: ArchivedMemory) -> bool:
        """从 archived SQLite 恢复(re-embed + upsert 回 Qdrant)。"""
        if self._embedder is None:
            logger.error(
                "vacuum.restore_from_archive no_embedder key=%s", item.key,
            )
            return False
        # 调 embedder(异步)
        try:
            emb = self._run_embed_sync(item.content)
        except EmbedderError as e:
            logger.warning(
                "vacuum.restore_embed_failed key=%s err=%s", item.key, e,
            )
            return False
        # upsert 回 Qdrant
        payload: dict[str, Any] = {
            "content": item.content,
            "importance": item.importance,
            "tags": item.tags,
            "created_at": item.created_at,
        }
        if item.level:
            payload[PAYLOAD_LEVEL] = item.level
        for k, v in item.extra.items():
            payload[k] = v
        self._store.qdrant.upsert_with_vector(item.key, emb, payload)
        # 同步从 archive 表删
        self._archive.remove(item.key)
        logger.info("vacuum.restored_from_archive key=%s", item.key)
        return True

    def purge_now(
        self,
        key: str,
        *,
        confirm: str = "no",
    ) -> bool:
        """立即物理删(跳过 7+30 天反悔期)。

        Args:
            key: 记忆 key。
            confirm: 必须是 ``"yes"``,否则拒执行(双保险)。

        Returns:
            True = 已物理删,False = 未删(confirm 不是 yes / key 不存在)。
        """
        if confirm != "yes":
            logger.warning(
                "vacuum.purge_now refused confirm=%s key=%s", confirm, key,
            )
            return False
        # 同步路径
        import asyncio
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                return self._purge_sync(key)
            return loop.run_until_complete(self._purge_async(key))
        except RuntimeError:
            return asyncio.run(self._purge_async(key))

    def _purge_sync(self, key: str) -> bool:
        """异步路径下走 store.delete_memory 但用 sync 接口。"""
        # QdrantStorage.delete 是 async 的,这里用 _client.delete 同步接口
        # 简化:用 sync path,先查 id 再 delete
        points, _ = self._store.qdrant._client.scroll(
            collection_name=self._store.qdrant.collection,
            scroll_filter=self._store.qdrant._payload_filter_for_key(key),
            limit=1,
            with_payload=False,
            with_vectors=False,
        )
        if not points:
            return False
        self._store.qdrant._client.delete(
            collection_name=self._store.qdrant.collection,
            points_selector=[p.id for p in points],
        )
        # 同步从 archive 表也删
        self._archive.remove(key)
        logger.warning("vacuum.purged_now key=%s", key)
        return True

    async def _purge_async(self, key: str) -> bool:
        ok = await self._store.delete_memory(key)
        if ok:
            self._archive.remove(key)
            logger.warning("vacuum.purged_now key=%s", key)
        return ok

    def vacuum_stats(self) -> dict[str, int]:
        """返回各状态条目数(active / deleted_recoverable / archived)+ 文件大小。

        Returns:
            ``{"active": int, "deleted_recoverable": int, "archived": int,
               "total_storage_bytes": int, "qdrant_points": int}``
        """
        all_items = self._store.list_keys_by_level(include_deleted=True, limit=50_000)
        active = 0
        recoverable = 0
        for it in all_items:
            if it.get("deleted"):
                recoverable += 1
            else:
                active += 1
        archived_count = self._archive.count()
        archive_size = self._archive.size_bytes()
        try:
            qdrant_size = self._store.qdrant_size_bytes()
        except Exception:  # pragma: no cover
            qdrant_size = 0
        # Qdrant 嵌入式最小占用(~12K)主要是元数据 / segment 索引,不是用户数据
        # 用 min_overhead 提示用户区分
        min_qdrant_overhead = 10_000  # 空 collection 的合理下限
        user_qdrant_bytes = max(0, qdrant_size - min_qdrant_overhead)
        return {
            "active": active,
            "deleted_recoverable": recoverable,
            "archived": archived_count,
            "qdrant_storage_bytes": qdrant_size,
            "qdrant_user_data_bytes": user_qdrant_bytes,
            "archive_storage_bytes": archive_size,
            "total_storage_bytes": qdrant_size + archive_size,
        }

    # ------------------- 内部工具 -------------------

    def _purge_via_sync_api(self, key: str) -> bool:
        """sync 物理删(走 Qdrant client scroll + delete,绕过 async wrapper)。"""
        points, _ = self._store.qdrant._client.scroll(
            collection_name=self._store.qdrant.collection,
            scroll_filter=self._store.qdrant._payload_filter_for_key(key),
            limit=1,
            with_payload=False,
            with_vectors=False,
        )
        if not points:
            return False
        self._store.qdrant._client.delete(
            collection_name=self._store.qdrant.collection,
            points_selector=[p.id for p in points],
        )
        return True

    def _archive_one(self, key: str, payload: dict[str, Any]) -> None:
        """把一条软删除记忆搬到 archived SQLite。"""
        item = ArchivedMemory(
            key=key,
            content=payload.get("content", ""),
            importance=float(payload.get("importance", 0.5)),
            level=payload.get(PAYLOAD_LEVEL, ""),
            tags=list(payload.get("tags", [])),
            created_at=payload.get("created_at", ""),
            deleted_at=payload.get(PAYLOAD_DELETED_AT, ""),
            deleted_reason=payload.get("deleted_reason", ""),
            archived_at=datetime.now(UTC).isoformat(),
            # 额外字段(不解析的也存)
            extra={
                k: v for k, v in payload.items()
                if k not in {
                    "content", "importance", "tags", "created_at",
                    PAYLOAD_DELETED, PAYLOAD_DELETED_AT,
                    PAYLOAD_DELETED_REASON, PAYLOAD_LEVEL,
                }
            },
        )
        self._archive.add(item)

    def _run_embed_sync(self, content: str) -> list[float]:
        """同步上下文里跑 embed(支持 event loop 已 running 的场景)。"""
        import asyncio
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # 已在 event loop → 用 run_coroutine_threadsafe
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                    future = ex.submit(asyncio.run, self._embedder.embed(content))
                    return future.result(timeout=10)
            return loop.run_until_complete(self._embedder.embed(content))
        except RuntimeError:
            return asyncio.run(self._embedder.embed(content))


__all__ = [
    "VacuumService",
    "VacuumReport",
    "DeletedMemoryEntry",
]
