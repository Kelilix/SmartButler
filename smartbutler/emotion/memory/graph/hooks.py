"""🆕 Phase 9:图的抽取钩子(挂 ``ON_TASK_COMPLETE``)。

与 ``auto_extract.py`` 并列的**第二条抽取通路**:

- ``auto_extract``  → 写 Qdrant(向量的 fact 层)
- ``GraphExtractHook`` → 写 SQLite(关系图的边层)

两条通路**互不依赖** —— 图挂了不影响向量召回,反之亦然。

**为什么图要有独立的 hook 而不是复用 auto_extract**:
auto_extract 的输出是"按说话人分的 fact 文本",它的目的是 embedding。
图需要的是"人名 + 谓词 + 关系",语义完全不同,硬塞进同一个 prompt
会让两边都变差(而且要多花一次 LLM 调用)。
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from smartbutler.emotion.memory.graph.extractor import GraphExtractor
from smartbutler.emotion.memory.graph.writer import GraphWriter
from smartbutler.utils.logging import get_logger

if TYPE_CHECKING:
    from smartbutler.emotion.memory.hooks import MemoryHooks
    from smartbutler.emotion.memory.task_index import TaskIndex

logger = get_logger(__name__)


class GraphExtractHook:
    """task 完成时抽取人物关系并落图。

    用法::

        hook = GraphExtractHook(extractor=ex, writer=writer, task_index=idx)
        hook.register(hooks)
    """

    def __init__(
        self,
        *,
        extractor: GraphExtractor,
        writer: GraphWriter,
        task_index: TaskIndex,
        enabled: bool = True,
        max_edges_per_task: int = 20,
    ) -> None:
        """Args:
            extractor: LLM 抽取器。
            writer: 落库器。
            task_index: task 索引(读 task 的消息作为抽取输入)。
            enabled: 总开关(False = 不抽,零副作用)。
            max_edges_per_task: 单 task 边数上限。
        """
        self._extractor = extractor
        self._writer = writer
        self._task_index = task_index
        self._enabled = enabled
        self._max_edges = max_edges_per_task
        self._extracted_count = 0
        self._skipped_count = 0
        self._failed_count = 0

    # ------------------- 可观测性 -------------------

    @property
    def extracted_count(self) -> int:
        """成功落图的 task 数。"""
        return self._extracted_count

    @property
    def skipped_count(self) -> int:
        """被跳过(开关关 / 无内容 / 幂等命中)的 task 数。"""
        return self._skipped_count

    @property
    def failed_count(self) -> int:
        """抽取失败的 task 数。"""
        return self._failed_count

    def stats(self) -> dict[str, int]:
        """抽取统计(观测用)。"""
        return {
            "extracted": self._extracted_count,
            "skipped": self._skipped_count,
            "failed": self._failed_count,
        }

    # ------------------- 注册 -------------------

    def register(self, hooks: MemoryHooks) -> None:
        """注册到 :class:`MemoryHooks`,监听 ``ON_TASK_COMPLETE``。"""
        from smartbutler.emotion.memory.hooks import MemoryEventType

        hooks.register(MemoryEventType.ON_TASK_COMPLETE, self._on_task_complete)
        logger.info(
            "graph_extract_hook.registered enabled=%s max_edges=%s",
            self._enabled, self._max_edges,
        )

    # ------------------- 内部 -------------------

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
        """hook 回调 —— 同步签名,内部 fire-and-forget。"""
        if not self._enabled:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.debug("graph_extract.no_running_loop skip task_id=%s", task_id)
            return
        loop.create_task(
            self._extract_and_write(task_id=task_id, title=content),
            name=f"graph-extract-{task_id}",
        )

    async def _extract_and_write(self, *, task_id: str, title: str) -> None:
        """抽取 + 落库。"""
        try:
            facts, messages = self._collect_input(task_id=task_id, title=title)
            if not facts:
                self._skipped_count += 1
                logger.debug("graph_extract.empty_input task_id=%s", task_id)
                return

            result = await self._extractor.extract(facts=facts, messages=messages)
            if result.is_empty:
                self._skipped_count += 1
                logger.debug("graph_extract.empty_result task_id=%s", task_id)
                return

            speaker = facts[0][0] if facts else ""
            node_count, edge_count = await self._writer.write_task(
                task_id=task_id,
                result=result,
                speaker=speaker,
            )
            if edge_count or node_count:
                self._extracted_count += 1
            else:
                self._skipped_count += 1

            # 🆕 Step 3.9:一条汇总日志还原整次抽取的全过程
            # (节点数 / 边数 / 复用数 / 矛盾数 / 丢弃数按原因)
            logger.info(
                "graph_extract.summary task_id=%s nodes=%s edges=%s "
                "dropped_endpoint=%s dropped_predicate=%s dropped_non_person=%s",
                task_id, node_count, edge_count,
                result.dropped_invalid_endpoint,
                result.dropped_unknown_predicate,
                result.dropped_non_person,
            )
        except Exception as e:  # noqa: BLE001
            self._failed_count += 1
            logger.warning("graph_extract.failed task_id=%s err=%s", task_id, e)

    def _collect_input(
        self,
        *,
        task_id: str,
        title: str,
    ) -> tuple[list[tuple[str, str]], str]:
        """收集抽取输入:fact 骨架 + 原始消息。

        Returns:
            ``([(speaker, text), ...], messages_block)``
            —— 骨架是"谁说了什么"(归属),messages 是完整对话流(细节)。
        """
        try:
            msgs = list(self._task_index.iter_task_messages(task_id))
        except Exception as e:  # noqa: BLE001
            logger.debug(
                "graph_extract.iter_task_messages_failed task_id=%s err=%s", task_id, e
            )
            return ([], "")

        facts: list[tuple[str, str]] = []
        lines: list[str] = []

        for m in msgs:
            role = getattr(m, "role", "")
            content = (getattr(m, "content", "") or "").strip()
            if not content:
                continue
            user_id = getattr(m, "user_id", "") or ""
            if role == "user":
                facts.append((user_id, content))
                lines.append(f"[user] {content}")
            elif role == "assistant":
                lines.append(f"[assistant] {content}")

        if not facts and title:
            # 兜底:读不到消息时用 task 标题
            facts.append(("", title))
            lines.append(f"[user] {title}")

        return (facts, "\n".join(lines))


__all__ = ["GraphExtractHook"]
