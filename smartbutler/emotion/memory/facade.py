"""MemoryFacade —— thinking 层唯一入口(Phase 6.2 P0 核心 + 6.3 P1 扩展)。

设计原则:
1. **thinking 只 import facade** —— 不感知底层 Embedder / Retriever / Hooks / LongTerm / Forgetting / Vacuum
2. **所有方法非阻塞 async** —— 任何环节失败快速降级,管家不崩
3. **内部统一调 MemoryHooks** —— 扩展能力的接入点
4. **source 隔离(问题 10)** —— remember 自动写 ``source:xxx`` tag,
   recall 自动按 source tag 过滤(后续 6.3 加 ``user_id`` payload 做强隔离)

关键接口:
- ``MemoryContext``: ``user_id``(可空) + ``source``(说话人) + ``session_id``(跨 thread 标识)
- ``remember()``: 存长期记忆
- ``recall()``: 召回相关记忆
- ``format_for_prompt()``: 召回 + 格式化成可注入 system prompt 的 markdown 片段
- ``consolidate()``: 6.3 触发 consolidation 升格
- ``forget()``: 6.3 主动遗忘(5 类之一)
- ``restore()``: 6.3 恢复软删除
- ``list_deleted()``: 6.3 列软删除
- ``purge_now()``: 6.3 立即物理删
- ``vacuum_stats()``: 6.3 容量统计
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from smartbutler.capabilities.embedding.base import Embedder
from smartbutler.emotion.memory.forgetting import ForgetResult
from smartbutler.emotion.memory.hooks import MemoryEventType, MemoryHooks
from smartbutler.emotion.memory.retrieval import MemoryRetriever
from smartbutler.emotion.memory.long_term import LongTermStore
from smartbutler.utils.logging import get_logger

logger = get_logger(__name__)


def _resolve_max_fact_chars() -> int | None:
    """从 :class:`MemorySettings` 读单条 fact 注入上限,0 → ``None``(不截断)。

    🆕 2026-10-12:原先是模块常量 ``_MAX_FACT_CHARS``,改读 ``.env``:
    ``SMARTBUTLER_MEMORY_MAX_FACT_CHARS``,默认 0 = 不截断
    (用户决策:召回优先于 prompt 预算,详见 :mod:`smartbutler.config.memory`)。

    读取失败(比如 pydantic_settings 不可用)→ 退化为不截断,
    保证"配置坏了也不能让记忆内容丢失"。
    """
    try:
        from smartbutler.config.memory import load_memory_settings

        value = load_memory_settings().max_fact_chars
    except Exception as e:  # noqa: BLE001
        logger.warning("facade.max_fact_chars_load_failed,按不截断处理: %s", e)
        return None
    return value or None


def _truncate_middle(text: str, limit: int | None) -> str:
    """超长文本截断:**保留首尾**,砍掉中间。``limit=None`` → 原样返回。

    为什么不是直接 ``text[:limit]``:fact 是多轮对话流拼接,关键事实
    可能在任意轮次(宠物 case 里在第 5 轮 / 位置 1279)。只保留头部
    会把结论整段丢掉,LLM 就会答"我没记录过"。

    首尾各留一半,中间用 ``...(\\n[省略 N 字符]\\n...)`` 标记,
    让 LLM 知道这里有省略、不是用户没说过。

    当前默认上限为 ``None``(不截断),本函数在重新启用预算时才生效;
    保留它是为了不必重写。
    """
    if limit is None or len(text) <= limit:
        return text

    marker_budget = 40
    keep = limit - marker_budget
    head = keep // 2
    tail = keep - head
    dropped = len(text) - keep
    marker = f"...\n[省略中间 {dropped} 字符]\n..."

    return text[:head] + marker + text[-tail:]


def _extract_user_id_from_hit(hit: dict[str, Any]) -> str:
    """从 recall hit 中提取 user_id。

    优先顺序:
    1. tags 中的 ``user:xxx`` —— 2026-10-13 auto_extract 写入
    2. tags 中的 ``source:xxx`` —— 旧路径(2.5.1 时代)写入
    3. content 文本前缀 ``[user=xxx]`` —— 兜底:即便 tag 缺失,
       从内容头部也能反查;这条是**最可靠的兜底**,因为 content 是
       auto_extract._collect_user_content 实打实拼的。
    4. hit["key"] 解析 —— 旧 ``mem:user_id:uuid`` 格式,**当前代码
       已不再写此格式**,但保留解析以兼容历史数据
    5. hit["user_id"] 显式字段 —— 兜底
    6. 全部 miss → "未知"
    """
    # 1. tags.user:
    for tag in hit.get("tags", []) or []:
        if tag.startswith("user:"):
            uid = tag[len("user:") :]
            if uid:
                return uid

    # 2. tags.source:
    for tag in hit.get("tags", []) or []:
        if tag.startswith("source:"):
            uid = tag[len("source:") :]
            if uid:
                return uid

    # 3. content 文本前缀 [user=xxx]
    content = hit.get("content", "") or ""
    m = re.match(r"^\[user=([^\]]+)\]", content)
    if m:
        return m.group(1).strip()

    # 4. key 解析(兼容旧 mem:user_id:uuid)
    key = hit.get("key", "") or ""
    if key:
        parts = key.split(":")
        if len(parts) >= 2 and parts[0] == "mem":
            uid = parts[1]
            if uid and uid != "unknown":
                return uid

    # 5. 显式 user_id 字段
    uid = hit.get("user_id") or ""
    if uid:
        return uid

    return "未知"


@dataclass
class MemoryContext:
    """调用方提供的上下文(全部可空,空 = 单用户模式)。"""

    user_id: str | None = None
    source: str = ""
    session_id: str | None = None


class MemoryFacade:
    """Memory 高级 API —— thinking 层唯一入口。"""

    def __init__(
        self,
        retriever: MemoryRetriever,
        hooks: MemoryHooks,
        long_term: LongTermStore,
        embedder: Embedder,
        *,
        n_completed_tasks: int = 3,
        n_chitchat_tasks: int = 30,
        n_prior_tasks: int = 3,
    ) -> None:
        self._retriever = retriever
        self._hooks = hooks
        self._long_term = long_term
        self._embedder = embedder
        # 6.3 子服务(可选注入,None 时 6.3 API 不可用)
        self._forgetting: Any = None
        self._consolidator: Any = None
        self._vacuum: Any = None
        # 🆕 Phase 6.3 P2: 短期库视图(可选,None 时 format_for_prompt 跳过短库)
        self._short_term_view: Any = None
        # 🆕 Phase 6.3 P2: 短库锚点比例(短库 block 占总 max_chars 的比例)
        self._short_term_ratio: float = 0.4
        # 🆕 Phase 6.4 P2: 短库窗口切窗 N 值(可被 bind 覆盖)
        self._n_completed_tasks = n_completed_tasks
        self._n_chitchat_tasks = n_chitchat_tasks
        self._n_prior_tasks = n_prior_tasks
        # 🆕 Phase 6.4 P2: 任务调度器(可空,空则不主动触发)
        self._task_scheduler: Any = None
        # 🆕 recall 相似度下限(默认 0=不过滤;由 wiring 层按
        # MemorySettings.recall_min_score 注入,避免每个调用点都要传参)
        self._min_score: float = 0.0

    def set_min_score(self, min_score: float) -> None:
        """设置 recall 默认相似度下限(0-1,由 wiring 层调一次)。"""
        self._min_score = min_score

    # ------------------- 6.3 子服务注入(可选) -------------------

    def bind_forgetting(self, service: Any) -> None:
        """注入 5 类遗忘服务(由 wiring 层调)。"""
        self._forgetting = service

    def bind_consolidator(self, service: Any) -> None:
        """注入 consolidation 服务(由 wiring 层调)。"""
        self._consolidator = service

    def bind_vacuum(self, service: Any) -> None:
        """注入 vacuum 服务(由 wiring 层调)。"""
        self._vacuum = service

    def bind_short_term_view(self, view: Any) -> None:
        """注入短期库视图(Phase 6.3 P2,2.5.1.2 task 边界+滑窗)。

        注入后,``format_for_prompt`` 会先读短库构造 task window,再拼到 prompt。
        None = 老行为(只 recall 长期)。
        """
        self._short_term_view = view

    def bind_short_term_factory(self, factory: Any) -> None:
        """注入异步 saver 工厂(Phase 6.3 P2,2.5.1.2)。

        给 orchestrator 用:每次 ainvoke 时调 ``await factory()`` 拿 ``AsyncSqliteSaver``。
        优先于 ``bind_short_term_view``(后者是直接绑 view 实例)。
        """
        self._short_term_factory = factory

    def bind_task_index(self, task_index: Any) -> None:
        """🆕 Phase 6.4 P1: 注入 task 索引,使 short_term_view 走 LLM judge 路径。

        None = 老行为(实时规则 boundary + 扫所有 thread)。
        注入后,``_ensure_short_term_view`` 构造的 view 会拿这个 task_index,
        ``build_window`` 从 task_index 读 active task(更准)。
        """
        self._task_index = task_index

    def bind_task_scheduler(self, scheduler: Any) -> None:
        """🆕 Phase 6.4 P2: 注入任务调度器(供 orchestrator 通知消息触发 judge)。

        注入后,``notify_message_received()`` 会转调 ``scheduler.on_message_received()``。
        None = 老行为(仅依赖周期扫描)。
        """
        self._task_scheduler = scheduler

    async def notify_message_received(self, *, thread_id: str | None = None) -> dict[str, Any] | None:
        """🆕 Phase 6.4 P2: 通知 facade 有新 user 消息(供 thinking 层调)。

        内部转调 ``TaskScheduler.on_message_received``(有开关 + 频率限制)。
        scheduler 未注入时直接返 None(不抛)。
        """
        if self._task_scheduler is None:
            return None
        return await self._task_scheduler.on_message_received(thread_id=thread_id)

    async def _ensure_short_term_view(self) -> Any:
        """懒构造 short_term_view(短库 saver 是 async 句柄,复用现有)。"""
        if self._short_term_view is not None:
            return self._short_term_view
        factory = getattr(self, "_short_term_factory", None)
        if factory is None:
            return None
        try:
            short_term = factory()
            logger.debug("facade.ensure_short_term_view factory_called path=%s",
                         getattr(short_term, "_path", "?"))
            saver = await short_term.get_async_saver()
            logger.info(
                "facade.ensure_short_term_view saver_ready type=%s",
                type(saver).__name__,
            )
            from smartbutler.emotion.memory.short_term_view import ShortTermView
            from smartbutler.emotion.memory.task_boundary import TaskBoundaryDetector
            view = ShortTermView(
                saver=saver, detector=TaskBoundaryDetector(),
                task_index=getattr(self, "_task_index", None),
                n_completed_tasks=self._n_completed_tasks,
                n_chitchat_tasks=self._n_chitchat_tasks,
                n_prior_tasks=self._n_prior_tasks,
            )
            # 缓存:同一 saver 复用
            self._short_term_view = view
            return view
        except Exception as e:  # noqa: BLE001
            logger.warning("facade.ensure_short_term_view failed: %s", e)
            return None

    # ------------------- 暴露给 thinking 层的依赖 -------------------

    @property
    def embedder(self) -> Embedder:
        """暴露 embedder,供 thinking 直接 embed(预留,P0 阶段不调)。"""
        return self._embedder

    @property
    def retriever(self) -> MemoryRetriever:
        return self._retriever

    @property
    def long_term(self) -> LongTermStore:
        """暴露 LongTermStore(供 6.3 list_deleted 等用)。"""
        return self._long_term

    @property
    def hooks(self) -> MemoryHooks:
        return self._hooks

    # ------------------- P0 已有方法(签名零修改) -------------------

    async def remember(
        self,
        content: str,
        *,
        ctx: MemoryContext | None = None,
        tags: list[str] | None = None,
        importance: float | None = None,
        # 🆕 7.4 多模态扩展位(默认 None 不存字段,7.6/7.8 传值)
        image_ref: str | None = None,
        audio_ref: str | None = None,
    ) -> str:
        """存一条长期记忆。返回 key。

        🆕 7.4 扩展:支持多模态 ref(image_ref / audio_ref)。
        - image_ref: 原始图片文件路径,7.8 CameraAdapter 传入
        - audio_ref: 原始音频文件路径,7.6 VoiceAdapter 传入
        - 7.4-7.8 只存 Qdrant payload 字段,不做向量化
        - 7.9 启动时由 EmbeddingGemma2Embedder 补 image_embedding
        - 老调用(只传 content / ctx / tags / importance)零修改,完全兼容
        """
        if not content or not content.strip():
            raise ValueError("content 不能为空")
        source = ctx.source if ctx else ""
        key = await self._retriever.remember(
            content,
            tags=list(tags) if tags else None,
            source=source,
            importance=importance,
            image_ref=image_ref,  # 🆕 7.4 透传
            audio_ref=audio_ref,  # 🆕 7.4 透传
        )
        self._hooks.trigger(MemoryEventType.ON_STORE, key=key, content=content)
        logger.debug(
            "facade.remember key=%s source=%s image_ref=%s audio_ref=%s",
            key, source, image_ref, audio_ref,
        )
        return key

    async def recall(
        self,
        query: str,
        *,
        ctx: MemoryContext | None = None,
        k: int = 5,
        min_importance: float = 0.3,
        min_score: float | None = None,
    ) -> list[dict[str, Any]]:
        """召回相关记忆。语义检索 + 重要性过滤 + 时间衰减。

        🆕 2026-10-12: **不再按 ``ctx.source`` 过滤**。

        原因:管家是**面向所有用户的单一实体**,记忆池是全局的,
        不按说话人分箱。正确的语义是——
        male 用户说"下周三去上海"、female 用户说"下周三去北京",
        任一方问"行程"时都应该召回**两条**,由 LLM 综合判断并追问
        "male 说过下周三去上海,要改成北京吗?"。

        旧实现用 ``tag=f"source:{ctx.source}"`` 过滤,导致:
        - A 用户存的事实,B 用户(哪怕问的是同一件事)召不回
        - 实测踩坑:``--user u-male`` 问"火龙果培训内容",
          库里存的是 ``source=u-male-v3-b-default``,tag 对不上 → 召回为空
        - 同一个人换个 user_id 命名(如测试脚本 vs 正式跑)就"失忆"

        ``ctx.source`` 仍会写进 tags(供**人工排查/CLI 按 source 翻查**用,
        见 ``memory_cli recall --source``),只是不再参与召回过滤。

        Args:
            min_score: 相似度下限(0-1,作用在归一化 raw_norm)。
                低于此值丢弃,避免噪声进 prompt。
                ``None``(默认)= 用 :attr:`_min_score`(wiring 层从
                ``MemorySettings.recall_min_score`` 注入)。
        """
        if not query or not query.strip():
            return []
        if min_score is None:
            min_score = self._min_score
        logger.info("facade.recall start query=%r k=%d min_importance=%.2f min_score=%.2f",
                    query[:50], k, min_importance, min_score)
        hits = await self._retriever.recall(
            query,
            k=k,
            min_importance=min_importance,
            # ⚠️ tag 恒为 None —— 管家记忆是全局池,不按 source 分箱(2026-10-12)。
            # 要按 source 查请走 memory_cli recall --source(CLI 层显式传 tag)。
            tag=None,
            min_score=min_score,
        )
        for h in hits:
            self._hooks.trigger(
                MemoryEventType.ON_RECALL,
                key=h["key"],
                score=h.get("score", 0.0),
            )
        logger.info("facade.recall done hit_count=%d", len(hits))
        return hits

    async def format_for_prompt(
        self,
        query: str,
        *,
        ctx: MemoryContext | None = None,
        k: int = 5,
        max_chars: int = 2000,
    ) -> str:
        """召回 + 格式化成可注入 system prompt 的 markdown 片段。

        先拼短库 task 窗口,再拼长期 recall。短库 / 长期任一失败 →
        跳过该部分,不抛。

        🆕 2026-10-12:**不做总预算截断**(召回优先于 prompt 预算)。
        ``max_chars`` 保留仅为签名兼容 + 将来重新引入预算控制时使用,
        当前不参与裁剪 —— 实测按 max_chars 裁剪会把 fact 中段的关键事实
        砍掉(详细踩坑记录见 :func:`_resolve_max_fact_chars`)。
        单条 fact 的上限由 ``SMARTBUTLER_MEMORY_MAX_FACT_CHARS`` 控制,
        默认 0 = 不截断。
        """
        # ---- 1. 短期库 view(2.5.1.2)----
        short_block = ""
        logger.info("facade.format_for_prompt start session=%s has_ctx=%s",
                    ctx.session_id if ctx else None, ctx is not None)
        view = await self._ensure_short_term_view()
        if view is not None and ctx and ctx.session_id:
            try:
                from smartbutler.emotion.memory.short_term_view import (
                    build_short_term_prompt_block,
                )
                window = await view.build_window(
                    current_session_id=ctx.session_id,
                    current_user_input=query,
                    # ⚠️ 不要在这里传 n_prior_tasks / n_completed_tasks /
                    # n_chitchat_tasks —— build_window 的 call-time 参数优先级
                    # 高于构造参数(``n_prior if n_prior_tasks is None else ...``),
                    # 一旦在这里写死,facade 构造时收到的
                    # MemorySettings.{n_prior_tasks,n_completed_tasks,n_chitchat_tasks}
                    # 会被静默丢弃,.env 改了也不生效(踩过)。
                    # 不传 → 走 ShortTermView 构造时注入的那组值。
                    user_id=ctx.user_id,
                )
                short_block = build_short_term_prompt_block(window)
                logger.info("facade.format_for_prompt short_block len=%d", len(short_block))
            except Exception as e:  # noqa: BLE001
                logger.warning("facade.short_term_block_failed: %s", e)
                short_block = ""
        elif view is None:
            logger.warning("facade.format_for_prompt short_view_unavailable")
        elif not ctx or not ctx.session_id:
            logger.warning("facade.format_for_prompt no_session_id_in_ctx")

        # ---- 2. 长期库 recall(原有逻辑)----
        hits = await self.recall(query, ctx=ctx, k=k)
        long_block = ""
        if hits:
            lines: list[str] = [f"### 相关历史记忆(共 {len(hits)} 条)"]
            for h in hits:
                content = h.get("content", "")
                content = _truncate_middle(content, _resolve_max_fact_chars())
                # 从 key("mem:user_id:uuid") 或 tags("source:user_id") 提取 user_id
                user_id = _extract_user_id_from_hit(h)
                lines.append(f"- **[{user_id} 说]** {content}")
            long_block = "\n".join(lines)

        # ---- 3. 拼装 ----
        #
        # 🆕 2026-10-12 用户决策:**召回优先于 prompt 预算**。
        # 原来这里按 `max_chars` 拆分 short/long 配额再各自截断,但实测会
        # 把 fact 中段的关键事实砍掉(宠物 case:足展/年龄在位置 1279,
        # long_budget=1200 直接砍没),LLM 于是答"没记录"。
        #
        # 所以现在**不做总预算裁剪**:召回到的内容原样注入,保证完整。
        # `max_chars` 参数保留(签名兼容 + 将来重新引入预算时用),
        # 当前不参与裁剪;单条 fact 的上限另由 .env 控制
        # (`SMARTBUTLER_MEMORY_MAX_FACT_CHARS`,默认 0=不截断)。
        parts: list[str] = []
        if short_block:
            parts.append(short_block)
        if long_block:
            parts.append(long_block)
        return "\n\n".join(parts)

    # ------------------- 6.3 新增方法(6 个) -------------------

    async def consolidate(
        self,
        *,
        level: str = "daily",
    ) -> dict[str, Any]:
        """触发一次 consolidation(6.3 升格管道)。

        Args:
            level: "daily" / "weekly" / "monthly"。

        Returns:
            ConsolidationReport.to_dict()(空报告若 consolidator 未注入)。
        """
        if self._consolidator is None:
            logger.warning("facade.consolidate no_consolidator_bound")
            return {"level": level, "promoted": 0, "skipped": 0, "errors": []}
        report = await self._consolidator.consolidate(level=level)
        return {
            "level": report.level.value,
            "scanned": report.candidates_scanned,
            "promoted": report.promoted,
            "forgotten": report.forgotten,
            "skipped_low_importance": report.skipped_low_importance,
            "skipped_invalid_5w1h": report.skipped_invalid_5w1h,
            "new_keys": list(report.new_keys),
            "errors": list(report.errors),
        }

    async def forget(
        self,
        key: str,
        *,
        reason: str = "user_request",
    ) -> dict[str, Any]:
        """主动遗忘一条事实(6.3)。

        Args:
            key: 记忆 key。
            reason: 遗忘原因(``"user_request"``/``"key"``/``"ttl"``/``"conflict"``/``"promote"``)。

        Returns:
            ForgetResult.to_dict() 风格。
        """
        if self._forgetting is None:
            logger.warning("facade.forget no_forgetting_bound")
            return {"requested": 1, "forgotten": 0, "skipped": 1, "reason": reason}
        from smartbutler.emotion.memory.forgetting import ForgetReason
        try:
            fr = ForgetReason(reason)
        except ValueError:
            fr = ForgetReason.USER_REQUEST
        if fr == ForgetReason.TTL:
            result = await self._forgetting.forget_by_ttl()
        elif fr == ForgetReason.PROMOTE:
            # facade 不直接接 promote(consolidation 内部调),改走 key
            result = await self._forgetting.forget_by_user_request(key)
            result = ForgetResult(
                requested=result.requested, forgotten=result.forgotten,
                skipped=result.skipped, reason=fr,
                forgotten_keys=result.forgotten_keys,
                skipped_keys=result.skipped_keys,
            )
        elif fr == ForgetReason.CONFLICT:
            # facade 不接 conflict(需要新/旧 importance),改走 key
            result = await self._forgetting.forget_by_user_request(key)
        else:
            result = await self._forgetting.forget_by_user_request(key)
        return {
            "requested": result.requested,
            "forgotten": result.forgotten,
            "skipped": result.skipped,
            "reason": result.reason.value,
            "forgotten_keys": list(result.forgotten_keys),
            "skipped_keys": list(result.skipped_keys),
        }

    async def restore(self, key: str) -> bool:
        """恢复软删除的事实(6.3)。"""
        if self._forgetting is None and self._vacuum is None:
            logger.warning("facade.restore no_service_bound")
            return False
        # 优先 forgetting.restore(Qdrant 主体),fallback 到 vacuum.restore(archived)
        if self._forgetting is not None:
            ok = await self._forgetting.restore(key)
            if ok:
                return True
        if self._vacuum is not None:
            return self._vacuum.restore(key)
        return False

    async def list_deleted(
        self,
        *,
        level: str = "any",
        limit: int = 100,
        older_than_days: int = 0,
    ) -> list[dict[str, Any]]:
        """列软删除的事实(6.3)。"""
        if self._vacuum is None:
            logger.warning("facade.list_deleted no_vacuum_bound")
            return []
        items = self._vacuum.list_deleted(
            level=level, limit=limit, older_than_days=older_than_days,
        )
        return [it.to_dict() for it in items]

    async def purge_now(
        self,
        key: str,
        *,
        confirm: str = "no",
    ) -> bool:
        """立即物理删(6.3,跳过 7+30 天反悔期)。"""
        if self._vacuum is None:
            logger.warning("facade.purge_now no_vacuum_bound")
            return False
        return self._vacuum.purge_now(key, confirm=confirm)

    async def vacuum_stats(self) -> dict[str, int]:
        """返回容量统计(6.3)。"""
        if self._vacuum is None:
            logger.warning("facade.vacuum_stats no_vacuum_bound")
            return {}
        return self._vacuum.vacuum_stats()

    async def vacuum_once(
        self,
        *,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """跑一次 vacuum(6.3,默认 dry_run=True 推荐先看)。"""
        if self._vacuum is None:
            logger.warning("facade.vacuum_once no_vacuum_bound")
            return {"dry_run": dry_run, "scanned_total": 0, "to_archive": 0, "to_purge": 0}
        report = self._vacuum.vacuum_once(dry_run=dry_run)
        return report.to_dict()

    async def count_deleted_by_age(
        self,
        *,
        level: str = "any",
    ) -> dict[str, int]:
        """按软删除时长分桶统计(6.4 增强,见 CLI age-summary 子命令)。"""
        if self._vacuum is None:
            logger.warning("facade.count_deleted_by_age no_vacuum_bound")
            return {}
        return self._vacuum.count_deleted_by_age(level=level)

    async def bulk_purge_before(
        self,
        *,
        older_than_days: int,
        confirm: str = "no",
    ) -> dict[str, Any]:
        """按软删除时长批量物理删(6.4 增强,见 CLI purge-before 子命令)。"""
        if self._vacuum is None:
            logger.warning("facade.bulk_purge_before no_vacuum_bound")
            return {"requested": 0, "purged": 0, "skipped": 0, "errors": ["no_vacuum"], "dry_run": True}
        return self._vacuum.bulk_purge_before(
            older_than_days=older_than_days, confirm=confirm,
        )

    async def bulk_purge_archived(
        self,
        *,
        older_than_days: int = 30,
        confirm: str = "no",
    ) -> dict[str, Any]:
        """按 archived 时长批量物理删(6.4 增强,见 CLI purge-archived 子命令)。"""
        if self._vacuum is None:
            logger.warning("facade.bulk_purge_archived no_vacuum_bound")
            return {"requested": 0, "purged": 0, "errors": ["no_vacuum"], "dry_run": True}
        return self._vacuum.bulk_purge_archived(
            older_than_days=older_than_days, confirm=confirm,
        )


__all__ = ["MemoryFacade", "MemoryContext"]
