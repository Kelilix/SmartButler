"""🆕 Phase 6.3+: 短期对话 → 长期 fact 自动抽取 hook(流水线 A)。

触发时机:
- :data:`MemoryEventType.ON_TASK_COMPLETE` —— TaskScheduler 检到 task 进入
  completed/chitchat_done 时调

行为(🆕 2026-10-13):
1. 从 task_index 按 task_id 读 user 消息(拼成一段 content)
2. **importance 门控由 ``min_importance`` 字段(来自 .env 的
   ``SMARTBUTLER_MEMORY_AUTO_EXTRACT_MIN_IMPORTANCE``)决定**:
   - = 0.0(默认)→ 跳过 HeuristicScorer,所有 fact 都进库(importance=0.5)
   - > 0       → 走 HeuristicScorer 启发式评分,<阈值丢弃
3. tags 带上 ``user:xxx`` / ``task:xxx`` / ``category:xxx`` / ``status:xxx``

设计原则:
- **不阻塞** callback(让 hook 内部 spawn 异步任务),遵循 ON_TASK_COMPLETE 回调约定
- **降级优先** —— 任何环节失败 warn + 跳过,不让管家崩
- **idempotent** —— 已抽取过的 task_id 不重复抽(由 TaskScheduler 保证)

🆕 2026-10-13:为什么默认 min_importance=0.0(全量抽取)
------------------------------------------------------------------
旧实现:HeuristicScorer 启发式打分,<0.3 直接丢。
问题:身份信息("我是高天禹,男主人")启发式 ~0.6,刚好过 0.3;"下周三
      去北京""想换工作"等短句易因长度/情绪词缺失被误判,门控在抽取
      阶段 = 大量信息进不来,召回永远为空。
新行为:默认 min_importance=0.0 = 不门控,所有事实都进库,
      consolidation(periodic / daily)阶段按 importance + 重复度 +
      时间衰减决定"保留/合并/遗忘"。门控该放在 consolidator,
      不在抽取。
兼容老行为:wiring 层读 .env,如果 ``auto_extract_min_importance > 0``
      直接把值传给 hook 的 ``min_importance`` 即可。
"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from smartbutler.emotion.memory.hooks import MemoryEventType, MemoryHooks
from smartbutler.emotion.memory.importance import ImportanceScorer
from smartbutler.emotion.memory.levels import (
    participants_tag,
    task_started_at_tag,
    task_tag,
    user_tag,
)
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
        scorer: ImportanceScorer(默认 :class:`ImportanceScorer` 启发式)
        min_importance: importance 门控阈值,**来自 .env 的
            ``SMARTBUTLER_MEMORY_AUTO_EXTRACT_MIN_IMPORTANCE``(由
            ``MemorySettings.auto_extract_min_importance`` 注入)。
            = 0.0(默认)→ 不门控,所有 fact 进库(importance=0.5);
            > 0       → 启发式评分 < 此值丢弃(老行为)。
    """

    def __init__(
        self,
        *,
        facade: "MemoryFacade",
        short_term: "ShortTermMemory",
        task_index: "TaskIndex",
        scorer: ImportanceScorer | None = None,
        min_importance: float = 0.0,
    ) -> None:
        self._facade = facade
        self._short_term = short_term
        self._task_index = task_index
        self._scorer = scorer or ImportanceScorer()
        # 唯一门控源:min_importance 字段。0=不门控(默认),>0=老行为。
        self._min_importance = float(min_importance)
        self._extracted_count = 0
        self._skipped_low_importance = 0
        self._failed_count = 0
        # 🆕 2026-10-13:门控由 min_importance 字段单点控制(0=不门控)。
        #
        # 为什么:身份信息("我是高天禹,男主人")的 HeuristicScorer 启发式
        # 分 ~0.6,刚好过 0.3 门控;但"下周三去北京""想换工作"这种短句容易
        # 因长度/情绪词缺失被误判,门控在抽取阶段 = 大量信息永远进不来。
        # 门控该放在 consolidation(consolidator 按 importance + 重复度
        # + 时间衰减决定"保留/合并/遗忘"),不在抽取阶段。
        self._filter_enabled: bool = self._min_importance > 0.0
        if self._filter_enabled:
            logger.info(
                "auto_extract.importance_filter_active threshold=%.2f",
                self._min_importance,
            )
        else:
            logger.debug("auto_extract.importance_filter_inactive (min_importance=0)")

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
        2. **按说话人分桶**(2026-11-02)—— 每个说话人一条 fact
        3. 默认 importance=0.5(中性,consolidator 后处理)
        4. remember() 写 Qdrant,tags 完整保留 user / task / category / status

        🆕 2026-11-02: 一个 task 可能产出**多条** fact(按说话人拆),
        但它们**共享同一个 ``task:`` tag** —— 这是"task 是工作单元、
        记忆以 task 组织"的落地方式:按 task 召回时能把分片拼回完整闭环。
        """
        try:
            # 1) 读 user 消息(按 task_id 从 task_index 抽,不再按 thread)
            #    task_index.task_messages 表在 task_segmenter 写消息时已按 task_id 切好。
            #    旧实现按 thread 读会把同 thread 多 task 消息一起抽进来导致重复。
            user_content, user_ids = await self._collect_user_content(
                task_id=task_id,
                title=title,
            )
            if not user_content:
                logger.debug("auto_extract.empty_content task_id=%s", task_id)
                return

            # 2) 🆕 2026-10-13: importance 处理
            #    - min_importance == 0(默认,wiring 不传或传 0) → 门控关闭
            #      importance=0.5(中性),所有 fact 都进库
            #    - min_importance > 0(.env 显式设) → 走 HeuristicScorer 门控
            if self._filter_enabled:
                importance = await self._scorer.score(user_content)
                if importance < self._min_importance:
                    self._skipped_low_importance += 1
                    logger.debug(
                        "auto_extract.skip_low_importance task_id=%s importance=%.2f threshold=%.2f",
                        task_id, importance, self._min_importance,
                    )
                    return
            else:
                importance = 0.5  # 中性默认值

            # 3) 🆕 2026-11-02: 按说话人拆 fact
            #
            # 归属 = 说话人(声纹给的 user_id,客观)。转述内容原样留在正文里,
            # 不做"我妈 → 妈妈 user_id"的映射 —— 那是置信度问题,交给 LLM
            # 在召回时判断,代码只负责客观的 speaker 归属。
            #
            # 读不到 task_messages 时(兜底路径)只能落一条 unknown 归属的 fact,
            # 总比丢信息强。
            buckets = self._group_by_speaker(self._safe_iter_messages(task_id))
            if not buckets:
                buckets = {"": [user_content]}
                user_ids = []

            # task 起始时间(跨天 task 归一化用)
            task_started_at = self._task_started_at(task_id)

            # 参与者 = 本 task 涉及的全部 user
            participants = list(user_ids) or [
                u for u in buckets.keys() if u
            ]

            for speaker, lines in buckets.items():
                tags = self._build_fact_tags(
                    task_id=task_id,
                    speaker=speaker,
                    category=category,
                    status=status,
                    participants=participants,
                    task_started_at=task_started_at,
                )
                content = "\n".join(lines) if lines else user_content
                await self._facade.remember(
                    content,
                    tags=tags,
                    importance=importance,
                )
                self._extracted_count += 1
                logger.info(
                    "auto_extract.stored task_id=%s speaker=%s n_lines=%d "
                    "importance=%.2f tags=%s content=%s",
                    task_id, speaker or "(unknown)", len(lines),
                    importance, tags, content[:80],
                )
        except Exception as e:  # noqa: BLE001
            self._failed_count += 1
            logger.warning("auto_extract.failed task_id=%s err=%s", task_id, e)

    def _safe_iter_messages(self, task_id: str) -> list[Any]:
        """读 task 消息,失败返回空列表(不抛)。"""
        try:
            return list(self._task_index.iter_task_messages(task_id))
        except Exception as e:  # noqa: BLE001
            logger.debug(
                "auto_extract.iter_task_messages_failed task_id=%s err=%s", task_id, e
            )
            return []

    def _task_started_at(self, task_id: str) -> str:
        """取 task 的起始时间(ISO)。

        用于跨天 task 归一化:一个 task 跨了午夜时,它的 fact 应该全部
        落在"开始那天"的日报里,而不是被切成两段。
        读不到返回 ""(退化为用 fact 自身的 created_at)。
        """
        try:
            rec = self._task_index.get_task(task_id)
        except Exception as e:  # noqa: BLE001
            logger.debug(
                "auto_extract.get_task_failed task_id=%s err=%s", task_id, e
            )
            return ""
        return getattr(rec, "start_ts", "") or ""

    async def _collect_user_content(
        self,
        *,
        task_id: str,
        title: str,
    ) -> tuple[str, list[str]]:
        """按 task_id 从 :class:`TaskIndex` 拿本 task 的 user 消息,拼成一段。

        Args:
            task_id: task id(task_index.task_messages 按此切分)
            title: 任务标题(读不到消息时兜底)

        Returns:
            (拼接后的 content, 该 task 涉及的所有 user_id 列表)
            空字符串 + 空列表 = 无内容。
        """
        try:
            msgs = self._task_index.iter_task_messages(task_id)
        except Exception as e:  # noqa: BLE001
            logger.debug("auto_extract.iter_task_messages_failed task_id=%s err=%s", task_id, e)
            return title, []

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
        user_ids: list[str] = []
        for m in msgs:
            role = (getattr(m, "role", "") or "").lower()
            user_id = (getattr(m, "user_id", "") or "")
            content = (getattr(m, "content", "") or "").strip()
            if not content:
                continue
            if role in ("user", "human"):
                tag = f"[user={user_id}]" if user_id else "[user]"
                lines.append(f"{tag} {content}")
                if user_id:
                    user_ids.append(user_id)
            elif role in ("assistant", "ai"):
                lines.append(f"[assistant] {content[:300]}")
            # 🆕 2026-10-12: **不再写入 tool 行**。
            #
            # 为什么:tool 的 content 是 HTTP 原始响应片段(如
            # "[HTTP 200, content-type=text/html, length=5022]" + 网页正文),
            # 对 LLM 回答"页面讲了什么"毫无价值,却有两个害处:
            #   1. embedding 被 HTML 词表拉偏,和自然语言 query 不对齐;
            #   2. 挤占字符预算 —— 实测那条课程 fact 里 tool 段占前 189 字符,
            #      把 assistant 结论段顶到第 189 字符之后,旧的 200 字符
            #      注入截断正好把结论砍在标签上([assista...])。
            #
            # 有价值的结论已经在 assistant 回复里(课程名/费用/报名条件),
            # tool 噪音只会稀释它。

        if lines:
            return "\n".join(lines), user_ids
        return title, []

    # 🆕 2026-11-02: 按说话人分桶
    # -----------------------------------------------------------------------
    #
    # 设计要点(**归属 = 说话人,不是话语里提到的人**):
    #
    # 高天禹说"我妈今天也去上海" → fact 归属**高天禹**,不是他妈妈。
    # 因为"我妈去上海"是**高天禹的说法**,可信度取决于谁说的;
    # 如果高天禹自己说"我今天去上海",才是他本人的事实。
    #
    # 这就是"转述的置信度问题"在系统里的落法:
    # - **归属只认 speaker**(声纹识别出的 user_id,客观事实)
    # - **转述内容靠 content 里的自然语言保留**(LLM 读正文时自己判断)
    #
    # 代码不能做、也不该做"这句话描述的是谁的行程"的判断 —— 那没有客观
    # 答案。只有 speaker 是客观的。
    #
    # 为什么按说话人拆成多条 fact(而不是一个 task 一条):
    # - 一个 task 里多人说话时,单条 fact 只能挂一个 user tag → 另一半发言
    #   的归属被"Counter 取主用户"悄悄丢掉(旧实现的 bug)。
    # - 拆开后,升格时按 user 分桶就能得到"每个人的一天",互不污染。

    def _group_by_speaker(
        self,
        msgs: list[Any],
    ) -> dict[str, list[str]]:
        """把 task 内的消息按**说话人**分桶。

        Args:
            msgs: :meth:`TaskIndex.iter_task_messages` 的结果。

        Returns:
            ``{user_id: [行, ...]}`` —— user_id 为主说话人,行是拼好的对话流。
            无 user_id 的消息归到 ``""``(unknown 桶)。

        规则:
        - ``user`` 消息进**自己**的桶
        - ``assistant`` 消息进**上一位 user**的桶
          (管家在回应谁 → 那条 task 的结论属于谁;
           与 ``task_segmenter`` 的锚点逻辑一致 —— assistant 跟随锚点 user)
        - 桶内保持原始消息顺序
        """
        buckets: dict[str, list[str]] = {}
        last_speaker = ""
        for m in msgs:
            role = (getattr(m, "role", "") or "").lower()
            user_id = (getattr(m, "user_id", "") or "").strip()
            content = (getattr(m, "content", "") or "").strip()
            if not content:
                continue
            if role in ("user", "human"):
                last_speaker = user_id
                tag = f"[user={user_id}]" if user_id else "[user]"
                buckets.setdefault(user_id, []).append(f"{tag} {content}")
            elif role in ("assistant", "ai"):
                # assistant 跟随上一位 user;若还没有 user,归 unknown 桶
                buckets.setdefault(last_speaker, []).append(f"[assistant] {content[:300]}")
        return buckets

    def _build_fact_tags(
        self,
        *,
        task_id: str,
        speaker: str,
        category: str,
        status: str,
        participants: list[str],
        task_started_at: str,
    ) -> list[str]:
        """拼 fact 的 tags。

        **归属三层不变式的起点** —— 升格时这三类 tag 会被代码重新写一遍:

        - ``task:<task_id>``     工作单元锚点(同一 task 的多条 fact 共享)
        - ``user:<speaker>``     **主归属 = 说话人**
        - ``participants:<ids>`` 本 task 涉及的全部 user(多人 task 才写)
        - ``task_started_at:<iso>`` task 起始时间(跨天 task 归一化用)

        Args:
            task_id: task id。
            speaker: 本条 fact 的说话人(主归属)。
            category: task 类别。
            status: task 状态。
            participants: 本 task 涉及的全部 user_id。
            task_started_at: task 起始时间(ISO),空则不写该 tag。

        Returns:
            tag 列表。
        """
        tags = [
            task_tag(task_id),
            user_tag(speaker),
            f"category:{category}",
            f"status:{status}",
        ]
        if len(participants) > 1:
            p_tag = participants_tag(participants)
            if p_tag:
                tags.append(p_tag)
        started = task_started_at_tag(task_started_at)
        if started:
            tags.append(started)
        return [t for t in tags if t]


__all__ = ["AutoExtractHook"]
