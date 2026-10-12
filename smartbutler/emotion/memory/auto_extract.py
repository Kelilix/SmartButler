"""🆕 Phase 6.3+: 短期对话 → 长期 fact 自动抽取 hook(流水线 A)。

触发时机:
- :data:`MemoryEventType.ON_TASK_COMPLETE` —— TaskScheduler 检到 task 进入
  completed/chitchat_done 时调

行为:
1. 从 short_term 读 task 期间 user 消息(拼成一段 content)
2. ImportanceScorer 启发式评分(P0 阶段不调 LLM)
3. importance < ``min_importance`` → 丢弃(闲聊门控)
4. 调 :meth:`MemoryFacade.remember` 写 Qdrant(``level=None`` 留给
   Consolidator 升格)

设计原则:
- **不阻塞** callback(让 hook 内部 spawn 异步任务),遵循 ON_TASK_COMPLETE 回调约定
- **降级优先** —— 任何环节失败 warn + 跳过,不让管家崩
- **idempotent** —— 已抽取过的 task_id 不重复抽(由 TaskScheduler 保证)
"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from smartbutler.emotion.memory.hooks import MemoryEventType, MemoryHooks
from smartbutler.emotion.memory.importance import ImportanceScorer
from smartbutler.utils.logging import get_logger

if TYPE_CHECKING:
    from smartbutler.emotion.memory.facade import MemoryFacade
    from smartbutler.emotion.memory.short_term import ShortTermMemory
    from smartbutler.emotion.memory.task_index import TaskIndex

logger = get_logger(__name__)


class AutoExtractHook:
    """🆕 Phase 6.3+: 短期对话 → 长期 fact 自动抽取。

    Args:
        facade: :class:`MemoryFacade` —— 提供 ``remember()``
        short_term: :class:`ShortTermMemory` —— 用来读 task 期间 user 消息
        task_index: :class:`TaskIndex` —— 用来查 task title/category 兜底
        scorer: 重要性评分器,默认 :class:`ImportanceScorer`(启发式)
        min_importance: importance 门控,低于此值丢弃(默认 0.3,过滤纯闲聊)
    """

    def __init__(
        self,
        *,
        facade: "MemoryFacade",
        short_term: "ShortTermMemory",
        task_index: "TaskIndex",
        scorer: ImportanceScorer | None = None,
        min_importance: float = 0.3,
    ) -> None:
        self._facade = facade
        self._short_term = short_term
        self._task_index = task_index
        self._scorer = scorer or ImportanceScorer()
        self._min_importance = min_importance
        self._extracted_count = 0
        self._skipped_low_importance = 0
        self._failed_count = 0

    # ------------------- 公开方法 -------------------

    @property
    def extracted_count(self) -> int:
        """已成功写 Qdrant 的 fact 数(可观测性)。"""
        return self._extracted_count

    @property
    def skipped_low_importance(self) -> int:
        """被 importance 门控丢弃的 fact 数(可观测性)。"""
        return self._skipped_low_importance

    @property
    def failed_count(self) -> int:
        """抽取失败的 fact 数(可观测性)。"""
        return self._failed_count

    def register(self, hooks: MemoryHooks) -> None:
        """注册到 :class:`MemoryHooks`,监听 :data:`ON_TASK_COMPLETE`。

        触发后 fire-and-forget 跑抽取,不阻塞 scheduler。
        """
        hooks.register(MemoryEventType.ON_TASK_COMPLETE, self._on_task_complete)
        logger.info("auto_extract_hook.registered min_importance=%s", self._min_importance)

    # ------------------- 内部方法 -------------------

    def _on_task_complete(
        self,
        *,
        task_id: str,
        category: str,
        content: str,
        status: str,
        thread_id: str = "",
        **kwargs: Any,
    ) -> None:
        """hook 回调 —— 同步签名,内部 fire-and-forget 起异步任务。

        Args:
            task_id: 已写入 task_index 的 task id
            category: 任务类别(booking/phone_call/chitchat/...)
            content: 任务标题(task.title 兜底)
            status: completed / chitchat_done
            thread_id: 任务来源 thread
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.debug("auto_extract.no_running_loop skip task_id=%s", task_id)
            return
        loop.create_task(
            self._extract_and_store(
                task_id=task_id,
                category=category,
                title=content,
                status=status,
                thread_id=thread_id,
            ),
            name=f"auto-extract-{task_id}",
        )

    async def _extract_and_store(
        self,
        *,
        task_id: str,
        category: str,
        title: str,
        status: str,
        thread_id: str,
    ) -> None:
        """抽取 + 评分 + remember。

        步骤:
        1. 读 task 期间 user 消息(从 short_term)
           - 如果读不到(任务在 session 重启前已关)→ 用 title 兜底
        2. 拼成 content
        3. ImportanceScorer 评分
        4. importance < min_importance → 跳过,记 skipped_low_importance
        5. remember() 写 Qdrant
        """
        try:
            # 1) 读 user 消息(按 task_id 从 task_index 抽,不再按 thread)
            #    task_index.task_messages 表在 task_segmenter 写消息时已按 task_id 切好。
            #    旧实现按 thread 读会把同 thread 多 task 消息一起抽进来导致重复。
            user_content = await self._collect_user_content(
                task_id=task_id,
                title=title,
            )
            if not user_content:
                logger.debug("auto_extract.empty_content task_id=%s", task_id)
                return

            # 2) importance 评分
            importance = await self._scorer.score(user_content)
            if importance < self._min_importance:
                self._skipped_low_importance += 1
                logger.debug(
                    "auto_extract.skip_low_importance task_id=%s importance=%.2f threshold=%.2f",
                    task_id, importance, self._min_importance,
                )
                return

            # 3) remember —— level=None 留给 Consolidator 升格
            await self._facade.remember(
                user_content,
                tags=[f"task:{task_id}", f"category:{category}", f"status:{status}"],
                importance=importance,
            )
            self._extracted_count += 1
            logger.info(
                "auto_extract.stored task_id=%s importance=%.2f content=%s",
                task_id, importance, user_content[:80],
            )
        except Exception as e:  # noqa: BLE001
            self._failed_count += 1
            logger.warning("auto_extract.failed task_id=%s err=%s", task_id, e)

    async def _collect_user_content(
        self,
        *,
        task_id: str,
        title: str,
    ) -> str:
        """按 task_id 从 :class:`TaskIndex` 拿本 task 的 user 消息,拼成一段。

        Args:
            task_id: task id(task_index.task_messages 按此切分)
            title: 任务标题(读不到消息时兜底)

        Returns:
            拼接后的 content 字符串;空字符串表示无内容。
        """
        try:
            msgs = self._task_index.iter_task_messages(task_id)
        except Exception as e:  # noqa: BLE001
            logger.debug("auto_extract.iter_task_messages_failed task_id=%s err=%s", task_id, e)
            return title

        # 🆕 拼完整对话流(user + assistant),而不是只拼 user。
        #
        # 为什么:
        # - 旧版只取 user,fact 变成 "你是谁 | 你叫阿福吧",
        #   管家实际怎么回的完全丢失 → 召回后没有可用上下文,
        #   用户问"上次你自我介绍说了什么"这类 query 命中了也没内容可给。
        # - assistant 回复是 task 语义的一部分(尤其是 web_parse / booking
        # 这类"管家替用户做完了事"的 task,回复里才有结论)。
        #
        # 为什么不用 JSON 格式:
        # - 向量库的 content 会整段参与 embedding,"Q1"/"Q"/"A" 这类 key
        #   不是自然语言,会把向量方向从语义上带偏,和自然语言 query 不对齐。
        # - 纯对话流格式和用户真实 query 分布一致,检索更稳。
        # - task_id / category / status 已经走 tags 字段,不必重复塞进 content。
        #
        # 为什么不需要 LLM 参与:
        # - ImportanceScorer 是纯启发式(P0 不调 LLM);这里只是格式化,
        #   调 LLM 只会多一次 API 调用和延迟,收益不明显。
        # - 后续若要"提炼 fact"而不是"存对话流",那是 summarizer 的职责。
        lines: list[str] = []
        for m in msgs:
            role = (getattr(m, "role", "") or "").lower()
            user_id = (getattr(m, "user_id", "") or "")
            content = (getattr(m, "content", "") or "").strip()
            if not content:
                continue
            if role in ("user", "human"):
                tag = f"[user={user_id}]" if user_id else "[user]"
                lines.append(f"{tag} {content}")
            elif role in ("assistant", "ai"):
                lines.append(f"[assistant] {content[:300]}")
            elif role == "tool":
                lines.append(f"[tool] {content[:100]}")

        if lines:
            return "\n".join(lines)
        return title


__all__ = ["AutoExtractHook"]
