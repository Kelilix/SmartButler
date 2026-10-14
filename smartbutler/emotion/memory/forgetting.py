"""5 类遗忘 API(Phase 6.3 P1)。

设计要点:
1. **5 类遗忘**(2026-10-10 决策):
   - TTL 被动(importance < 0.2 且 created > 30 天)
   - 升格触发(consolidation 升格时,自动调 forget 软删 L1 旧 fact)
   - 主动(user_request)
   - 管家建议(key,recall 后管家问"还要保留吗")
   - 冲突覆盖(conflict,同 topic 新 fact 替换旧 fact)
2. **全部走软删除** —— ``LongTermStore.soft_delete()``,不物理删。
3. **可恢复** —— ``restore()`` 7 天内(在 Qdrant)或 30 天内(在 archived SQLite)有效。
4. **可审计** —— 每次 forget 记 INFO 日志(key + reason + 时间)。
5. **降级** —— soft_delete 失败(Qdrant 挂)→ 记 ERROR,返回 False,不抛。

不变量(全文遵守):
- 软删除永远不过 vacuum(默认关闭)—— 给用户反悔机会
- 物理删除由 vacuum_once 处理(7+30 天双时间窗)
- 软删除的 key 永远不在 recall 路径出现
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, UTC, timedelta
from enum import StrEnum, unique
from typing import Any

from smartbutler.emotion.memory.levels import (
    CONSOLIDATION_TTL_THRESHOLD,
    DAILY_TO_WEEKLY_DAYS,
)
from smartbutler.emotion.memory.long_term import (
    LongTermStore,
    PAYLOAD_DELETED,
    PAYLOAD_DELETED_REASON,
)
from smartbutler.utils.logging import get_logger

logger = get_logger(__name__)


@unique
class ForgetReason(StrEnum):
    """5 类遗忘原因。"""

    TTL = "ttl"                  # 被动 TTL(importance < 0.2 且 age > 30 天)
    PROMOTE = "promote"          # 升格触发(consolidation 升格时)
    USER_REQUEST = "user_request"  # 主动(user 说"忘掉 X")
    KEY = "key"                  # 管家建议(用户否)
    CONFLICT = "conflict"        # 冲突覆盖(同 topic 新 fact 替换)


@dataclass(frozen=True)
class ForgetResult:
    """单次 forget 的结果。"""

    requested: int     # 请求遗忘的条数
    forgotten: int     # 实际软删除成功条数
    skipped: int       # 跳过(key 不存在 / 已删 / 不满足条件)
    reason: ForgetReason
    forgotten_keys: tuple[str, ...]  # 实际软删的 key
    skipped_keys: tuple[str, ...]    # 跳过的 key


class ForgettingService:
    """5 类遗忘的统一入口(Phase 6.3 P1)。"""

    def __init__(self, store: LongTermStore) -> None:
        self._store = store

    # ------------------- 5 类遗忘的具体方法 -------------------

    async def forget_by_ttl(
        self,
        *,
        now: datetime | None = None,
        limit: int = 10_000,
    ) -> ForgetResult:
        """TTL 被动遗忘(第 1 类)。

        规则:``importance < CONSOLIDATION_TTL_THRESHOLD (0.2)`` 且 ``created > DAILY_TO_WEEKLY_DAYS (7) 天前``
        的 fact 自动软删除。

        注意:阈值比 daily 升格阈值低(0.2 vs 0.5),这保证"既不会被升格,也不会被遗忘"的情况不存在。

        Args:
            now: 当前时间(默认 = now UTC,测试时可注入)。
            limit: 单次扫描上限(Qdrant scroll 默认 10000)。

        Returns:
            :class:`ForgetResult`。
        """
        now = now or datetime.now(UTC)
        cutoff = now - timedelta(days=DAILY_TO_WEEKLY_DAYS)
        cutoff_iso = cutoff.isoformat()
        # 全量 list(只取非 deleted)
        all_items = self._store.list_keys_by_level(include_deleted=False, limit=limit)
        candidates: list[dict[str, Any]] = []
        for it in all_items:
            if it["importance"] >= CONSOLIDATION_TTL_THRESHOLD:
                continue
            created_str = it.get("created_at", "")
            if not created_str:
                continue
            try:
                created_at = datetime.fromisoformat(created_str)
            except ValueError:
                continue
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=UTC)
            if created_at > cutoff:
                continue
            candidates.append(it)
        return await self._forget_many(candidates, ForgetReason.TTL)

    async def forget_by_user_request(self, key: str) -> ForgetResult:
        """主动遗忘(第 3 类)—— 用户说"忘掉 X"。

        Args:
            key: 用户想忘掉的事实 key(或内容关键词,本接口只支持 key)。

        Returns:
            :class:`ForgetResult`(1 条或 0 条)。
        """
        return await self._forget_one(key, ForgetReason.USER_REQUEST)

    async def forget_by_key(self, key: str) -> ForgetResult:
        """管家建议遗忘(第 4 类)—— 管家问"还要保留吗"→ 用户否。"""
        return await self._forget_one(key, ForgetReason.KEY)

    async def forget_by_conflict(
        self,
        old_key: str,
        new_key: str,
        *,
        new_importance: float,
        old_importance: float,
    ) -> ForgetResult:
        """冲突覆盖(第 5 类)—— 同 topic 新 fact 替换旧 fact。

        规则:仅当 new_importance > old_importance 时才忘旧,避免误删。

        Args:
            old_key: 旧 fact key。
            new_key: 新 fact key(留作 audit)。
            new_importance: 新 fact importance。
            old_importance: 旧 fact importance。

        Returns:
            :class:`ForgetResult`。
        """
        if new_importance <= old_importance:
            logger.debug(
                "forget_by_conflict skipped new=%.2f not > old=%.2f",
                new_importance, old_importance,
            )
            return ForgetResult(
                requested=1, forgotten=0, skipped=1, reason=ForgetReason.CONFLICT,
                forgotten_keys=(), skipped_keys=(old_key,),
            )
        result = await self._forget_one(old_key, ForgetReason.CONFLICT)
        logger.info(
            "forget_by_conflict old=%s new=%s new_imp=%.2f old_imp=%.2f",
            old_key, new_key, new_importance, old_importance,
        )
        return result

    async def forget_by_promote(
        self, source_keys: list[str], new_key: str
    ) -> ForgetResult:
        """升格触发(第 2 类)—— consolidation 升格完成后,自动调 forget 软删源 fact。

        Args:
            source_keys: 被升格覆盖的源 fact keys。
            new_key: 升格后新 fact key(留作 audit)。

        Returns:
            :class:`ForgetResult`。
        """
        candidates: list[dict[str, Any]] = []
        not_found: list[str] = []
        for k in source_keys:
            pl = self._store.get_payload(k)
            if pl is None:
                not_found.append(k)
                continue
            if pl.get(PAYLOAD_DELETED):
                not_found.append(k)
                continue
            candidates.append({
                "key": k,
                "importance": pl.get("importance", 0.5),
                "created_at": pl.get("created_at", ""),
            })
        result = await self._forget_many(candidates, ForgetReason.PROMOTE)
        # 合并:missing 也算 skipped
        all_skipped = list(result.skipped_keys) + not_found
        final = ForgetResult(
            requested=len(source_keys),
            forgotten=result.forgotten,
            skipped=len(all_skipped),
            reason=result.reason,
            forgotten_keys=result.forgotten_keys,
            skipped_keys=tuple(all_skipped),
        )
        logger.info(
            "forget_by_promote new=%s forgotten=%d skipped=%d",
            new_key, final.forgotten, final.skipped,
        )
        return final

    async def restore(self, key: str) -> bool:
        """恢复软删除的事实(7 天内有效,见 vacuum.py 文档)。"""
        return await self._store.restore_deleted(key)

    # ------------------- 内部工具方法 -------------------

    async def _forget_one(self, key: str, reason: ForgetReason) -> ForgetResult:
        """单 key 软删除。"""
        existing = self._store.get_payload(key)
        if existing is None:
            return ForgetResult(
                requested=1, forgotten=0, skipped=1, reason=reason,
                forgotten_keys=(), skipped_keys=(key,),
            )
        if existing.get(PAYLOAD_DELETED):
            return ForgetResult(
                requested=1, forgotten=0, skipped=1, reason=reason,
                forgotten_keys=(), skipped_keys=(key,),
            )
        ok = await self._store.soft_delete(key, reason.value)
        return ForgetResult(
            requested=1, forgotten=1 if ok else 0, skipped=0 if ok else 1,
            reason=reason,
            forgotten_keys=(key,) if ok else (),
            skipped_keys=() if ok else (key,),
        )

    async def _forget_many(
        self, candidates: list[dict[str, Any]], reason: ForgetReason
    ) -> ForgetResult:
        """批量软删除。"""
        if not candidates:
            return ForgetResult(
                requested=0, forgotten=0, skipped=0, reason=reason,
                forgotten_keys=(), skipped_keys=(),
            )
        forgotten: list[str] = []
        skipped: list[str] = []
        for c in candidates:
            key = c["key"]
            ok = await self._store.soft_delete(key, reason.value)
            if ok:
                forgotten.append(key)
            else:
                skipped.append(key)
        logger.info(
            "forget_many reason=%s requested=%d forgotten=%d skipped=%d",
            reason.value, len(candidates), len(forgotten), len(skipped),
        )
        return ForgetResult(
            requested=len(candidates),
            forgotten=len(forgotten),
            skipped=len(skipped),
            reason=reason,
            forgotten_keys=tuple(forgotten),
            skipped_keys=tuple(skipped),
        )

    # ------------------- 审计 / 查询 -------------------

    def list_forgotten(
        self, *, limit: int = 10_000
    ) -> list[dict[str, Any]]:
        """列出所有软删除的事实(运维/审计用)。"""
        return self._store.list_keys_by_level(include_deleted=True, limit=limit)

    def get_reason(self, key: str) -> ForgetReason | None:
        """读一条软删除事实的 reason。"""
        pl = self._store.get_payload(key)
        if pl is None or not pl.get(PAYLOAD_DELETED):
            return None
        reason_str = pl.get(PAYLOAD_DELETED_REASON, "")
        try:
            return ForgetReason(reason_str)
        except ValueError:
            return None


__all__ = [
    "ForgettingService",
    "ForgetReason",
    "ForgetResult",
]
