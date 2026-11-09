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
from dataclasses import dataclass
from typing import Any

from smartbutler.capabilities.embedding.base import Embedder
from smartbutler.emotion.memory.forgetting import ForgetResult
from smartbutler.emotion.memory.hooks import MemoryEventType, MemoryHooks
from smartbutler.emotion.memory.levels import (
    INVALIDATES_TAG,
    parse_invalid_at,
    parse_participants_tag,
    parse_spoken_at,
    parse_task_tags,
    parse_valid_at,
)
from smartbutler.emotion.memory.long_term import LongTermStore
from smartbutler.emotion.memory.retrieval import MemoryRetriever
from smartbutler.emotion.memory.temporal import (
    is_valid_at_active,
    is_valid_at_pending,
)
from smartbutler.utils.logging import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# 🆕 Phase 9: 图通道辅助
# ---------------------------------------------------------------------------

#: 谓词 → 中文标签(给 LLM 看的关系描述)
_RELATION_LABELS: dict[str, str] = {
    "IS_FAMILY_OF": "是家人",
    "IS_MOTHER_OF": "是母亲",
    "IS_FATHER_OF": "是父亲",
    "IS_PARENT_OF": "是父母",
    "IS_CHILD_OF": "是子女",
    "IS_SIBLING_OF": "是兄弟姐妹",
    "IS_SPOUSE_OF": "是配偶",
    "IS_PARTNER_OF": "是伴侣",
    "IS_FRIEND_OF": "是朋友",
    "KNOWS": "认识",
    "WORKS_WITH": "是同事",
    "REPORTS_TO": "是上级",
    "IS_COLLEAGUE_OF": "是同事",
    "MANAGES": "是管理者",
    "RELATES_TO": "有关系",
}


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
        # 🆕 2026-11-02: user_id → 显示名解析器(可选)
        #
        # 归属 tag 里存的是 user_id(如 ``speaker_a3f2b1c``),直接注入 prompt
        # 用户看不懂。注入一个回调把 user_id 换成显示名(如"高天禹")。
        # 签名: ``Callable[[str], str]`` —— 传 user_id,返回可读名字。
        # 由 wiring 层绑定声纹库(voiceprint registry)。
        self._name_resolver: Any = None
        # 🆕 Phase 9: 记忆图存储(可选,None 时无图通道)
        self._graph_store: Any = None
        self._graph_max_edges: int = 10

    def set_min_score(self, min_score: float) -> None:
        """设置 recall 默认相似度下限(0-1,由 wiring 层调一次)。"""
        self._min_score = min_score

    def bind_name_resolver(self, resolver: Any) -> None:
        """绑定 user_id → 显示名解析器(由 wiring 层调)。

        Args:
            resolver: 可调用对象 ``fn(user_id) -> display_name``;
                None = 解绑(回退到直接显示 user_id)。
        """
        self._name_resolver = resolver

    def _display_name(self, user_id: str) -> str:
        """把 user_id 解析成显示名(解析失败回退到原值)。

        解析器抛异常时**不能**影响召回渲染 —— 显示成裸 user_id 仍然
        是可用的(甚至更安全,不会泄露错误的姓名)。
        """
        if not user_id:
            return "未知"
        if self._name_resolver is None:
            return user_id
        try:
            name = self._name_resolver(user_id)
        except Exception as e:  # noqa: BLE001
            logger.debug("facade.name_resolver_failed user_id=%s err=%s", user_id, e)
            return user_id
        return name or user_id

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

    def bind_graph_store(self, store: Any, *, max_edges: int = 10) -> None:
        """🆕 Phase 9: 注入记忆图存储(人物关系图通道)。

        注入后,``format_for_prompt`` 会在长期 recall 之外**追加**一段
        人物关系块(见 ``_render_graph_block``)。

        图通道是**补充**不是替代 —— 它解决向量召回解决不了的问题:
        "我妈是谁""谁是我同事""我朋友的老婆是谁"这类**关系型**问题,
        向量检索命中的是"张女士是我朋友"这种**文本**,不是关系本身。

        Args:
            store: :class:`GraphStore` 实例;None = 关闭图通道(老行为)。
            max_edges: 注入 prompt 的边条数上限。
        """
        self._graph_store = store
        self._graph_max_edges = max_edges

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
            long_block = self._render_long_block(hits)

        # ---- 3. 🆕 Phase 9: 图通道(人物关系)----
        graph_block = self._render_graph_block(query, ctx=ctx)

        # ---- 4. 拼装 ----
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
        if graph_block:
            parts.append(graph_block)
        return "\n\n".join(parts)

    def _render_graph_block(
        self,
        query: str,
        *,
        ctx: MemoryContext | None = None,
    ) -> str:
        """🆕 Phase 9: 渲染人物关系块。

        图通道解决向量召回解决不了的问题:

        - "我妈是谁" → 需要**关系边**,不是相似文本
        - "谁是我同事" → 同上
        - "我朋友的老婆是谁" → 需要 2 跳遍历

        向量检索命中的是"张女士是我朋友"这种**文本**,而不是关系本身;
        而且这类问题往往没有足够的语义相似度(问的是"我妈",库里写的是
        "我妈妈")。图能精确回答。

        渲染成自然语言(不是节点/边的 JSON)—— LLM 读自然语言最准。

        Args:
            query: 用户 query(用于挑选相关节点)。
            ctx: 记忆上下文(拿 user_id)。

        Returns:
            markdown 字符串(没有可用边时返回 "")。
        """
        if self._graph_store is None:
            return ""
        try:
            edges = self._collect_graph_edges(query, ctx=ctx)
            if not edges:
                return ""
            lines = ["### 人物关系"]
            for edge in edges:
                lines.append(f"- {self._render_graph_edge(edge)}")
            return "\n".join(lines)
        except Exception as e:  # noqa: BLE001
            logger.warning("facade.graph_block_failed: %s", e)
            return ""

    def _collect_graph_edges(
        self,
        query: str,
        *,
        ctx: MemoryContext | None = None,
    ) -> list[Any]:
        """收集与 query 相关的图边。

        策略:
        1. **说话人锚定** —— 优先取当前用户(user_id)关联的边
           (问"我妈是谁"时,答案就在"我"的关系里)
        2. **名字匹配** —— query 里提到的人名 → 查该节点的边
        3. 合并去重,按关联度排序,截断到上限

        Args:
            query: 用户 query。
            ctx: 记忆上下文。

        Returns:
            边列表(活跃边)。
        """
        if self._graph_store is None:
            return []

        edges: list[Any] = []
        seen: set[str] = set()

        # 1) 说话人锚定
        user_id = getattr(ctx, "user_id", "") if ctx else ""
        if user_id:
            node = self._graph_store.find_node_by_user_id(user_id)
            if node is not None:
                for e in self._graph_store.find_edges_for_node(
                    node.node_id, only_active=True, limit=self._graph_max_edges * 2
                ):
                    if e.edge_id not in seen:
                        seen.add(e.edge_id)
                        edges.append(e)

        # 2) query 里出现**图里已有的人名**
        #
        # ⚠️ **不从 query 里猜人名** —— 中文没有空格,纯正则分不出
        # "张伟最近怎么样" 里的"张伟"和"最近"。猜错会拉出无关关系。
        #
        # 反过来做:遍历图里的节点,看它的名字是否出现在 query 里。
        # 图里有哪些人我们是知道的 —— 这比猜准得多。
        for node in self._iter_all_nodes():
            if not node.name:
                continue
            if node.name not in query:
                continue
            for e in self._graph_store.find_edges_for_node(
                node.node_id, only_active=True, limit=self._graph_max_edges
            ):
                if e.edge_id not in seen:
                    seen.add(e.edge_id)
                    edges.append(e)

        return edges[: self._graph_max_edges]

    def _iter_all_nodes(self) -> list[Any]:
        """遍历图里所有节点(召回时找名字用)。

        图规模小(个人管家场景几百个节点),直接全扫最简单可靠 ——
        不需要额外的索引维护。
        """
        try:
            return self._graph_store.list_nodes()
        except Exception as e:  # noqa: BLE001
            logger.warning("facade.iter_graph_nodes_failed: %s", e)
            return []

    def _render_graph_edge(self, edge: Any) -> str:
        """把一条边渲染成一句自然语言。

        ``"我 --IS_MOTHER_OF--> 妈妈(事实上:我妈妈是我的母亲)"``
        """
        source = self._graph_store.get_node(edge.source_id)
        target = self._graph_store.get_node(edge.target_id)
        src_name = source.name if source else edge.source_id
        tgt_name = target.name if target else edge.target_id

        rel_cn = _RELATION_LABELS.get(edge.relation_type, edge.relation_type)
        text = f"{src_name} —{rel_cn}→ {tgt_name}"
        if edge.fact and edge.fact not in text:
            text = f"{text}({edge.fact})"
        if edge.valid_at:
            text = f"{text}[关系成立于 {edge.valid_at}]"
        return text

    def _render_long_block(self, hits: list[dict[str, Any]]) -> str:
        """把长期记忆命中渲染成 prompt 块 —— **按 task 分组**。

        🆕 2026-11-02。设计依据:用户决策"记忆以 task 维度组织,
        让 LLM 知道最近大家在做什么,力求 task 闭环"。

        旧实现是平铺的 ``- [user 说] content``,LLM 看到的是一堆零散事实,
        拼不出"这件事进展到哪了"。现在按 ``task:`` tag 分组:

            ### 相关历史记忆(共 N 条,3 个任务)

            **【订上海机票】**(高天禹)
            - **[高天禹 说]** 我明天要去上海
            - **[妈妈 说]** 我也去

            **【看房】**(高天禹、妈妈)
            - **[高天禹 说]** 周末看房

        归属用**显示名**(经 ``_name_resolver`` 解析),解析不了则显示 user_id。
        没有 task 归属的 fact(老数据)归到"其他"组。

        Args:
            hits: :meth:`recall` 的结果。

        Returns:
            markdown 字符串(无 hits 时返回 "")。
        """
        if not hits:
            return ""

        # 🆕 2026-11-04:算出"本轮被修正的旧 fact"。
        #
        # 为什么需要:Step 1 还不知道"张女士不再是我朋友了"具体否定的是
        # **哪一条**(那要实体消解 + 图,属 Step 3)。但如果**同一 task 内**
        # 同时召回了:
        #     旧:"张女士是我朋友"              (无标记)
        #     新:"张女士不再是我朋友了,我们掰了"  (invalidates:1)
        # 那么旧的那条**就是**被修正的对象 —— 至少在同一 task 范围内成立。
        #
        # 为什么要限定"同一 task":不同 task 的两条 fact 可能是**不同主题**
        # (task A 说"张女士是我朋友",task B 说"张女士不再是我朋友了"其实
        # 指另一个张女士?)。跨 task 乱标会把不相关的事实打成"已修正",
        # 比不标更糟。
        #
        # 限定 task 是 Step 1 能做到的最保守、最安全的推断。
        superseded_keys = self._compute_superseded_keys(hits)

        # 按 task 分组(保持首次出现顺序)
        groups: dict[str, list[dict[str, Any]]] = {}
        order: list[str] = []
        for h in hits:
            tids = parse_task_tags(h.get("tags", []) or [])
            tid = tids[0] if tids else ""
            if tid not in groups:
                groups[tid] = []
                order.append(tid)
            groups[tid].append(h)

        n_tasks = len([t for t in order if t])
        header = f"### 相关历史记忆(共 {len(hits)} 条"
        header += f",{n_tasks} 个任务)" if n_tasks else ")"

        lines: list[str] = [header]
        for tid in order:
            items = groups[tid]
            if tid:
                # 段落标题:task id + 该组的参与者
                parts = set()
                for h in items:
                    parts.update(parse_participants_tag(h.get("tags", []) or []))
                if not parts:
                    for h in items:
                        uid = _extract_user_id_from_hit(h)
                        if uid:
                            parts.add(uid)
                who = "、".join(
                    sorted(self._display_name(p) for p in parts)
                )
                lines.append(f"\n**【{tid}】**" + (f"({who})" if who else ""))
            else:
                lines.append("\n**【其他】**")
            for h in items:
                content = h.get("content", "")
                content = _truncate_middle(content, _resolve_max_fact_chars())
                user_id = _extract_user_id_from_hit(h)
                name = self._display_name(user_id)
                lines.append(
                    f"- **[{name} 说]** {content}"
                    f"{self._render_time_hint(h, superseded=superseded_keys)}"
                )

        return "\n".join(lines)

    @staticmethod
    def _render_time_hint(
        hit: dict[str, Any],
        *,
        superseded: set[str] | None = None,
    ) -> str:
        """给召回条目渲染时间提示(🆕 2026-11-04)。

        让 LLM 在读记忆正文时**同时看到时间语义**,能区分:
        - "2026-01-01 领证了" → 过去发生的事实
        - "张女士是我朋友(长期有效)" → 当前状态
        - "张女士不再是我朋友了(已失效/修正)" → 状态变更

        为什么必须显式给 LLM 看:
        正文里的"明天""下周一"在**抽取那一刻**已经绝对化过,
        但正文本身还是原话("我明天去上海")。只给正文,LLM 不知道
        "明天"指的是哪天 —— tag 里的绝对时间必须**翻译给 LLM**。

        Args:
            hit: 召回条目。
            superseded: **已被本轮其他 fact 修正**的 key 集合。
                命中 → 渲染成"已被修正",避免旧 fact 与新 fact 互相矛盾。

        返回空串表示没有时间信息(不添乱)。
        """
        tags = list(hit.get("tags", []) or [])
        valid = parse_valid_at(tags)
        invalid = parse_invalid_at(tags)
        is_superseded = bool(superseded) and hit.get("key", "") in superseded
        if not valid and not invalid and not is_superseded:
            return ""

        parts: list[str] = []
        if valid:
            parts.append(_describe_valid_at(valid))
        if invalid:
            parts.append(f"已于 {invalid} 失效")
        elif is_superseded:
            # 🆕 已被本轮修正 → 明确告诉 LLM "这条不再是当前状态"。
            #
            # 这是 Step 1 能在**不建图**的前提下解决矛盾的关键:
            # 旧 fact("张女士是我朋友")本身没有任何失效标记(我们还没法
            # 知道该给它打什么 invalid_at),但同一轮召回里出现了
            # "不再是我朋友了"这条修正 fact —— 那旧 fact 就是被修正的对象。
            #
            # 标记成"已被修正"而不是"长期有效",LLM 就不会把两条都当成
            # 当前状态,答案唯一。
            parts.append("已被后续说法修正")
        elif valid:
            # 🆕 2026-11-04:区分"尚未生效"与"当前已不适用"。
            #
            # 两者 ``is_valid_at_active`` 都是 False,但语义相反:
            # - 未来精确点("我明天去上海")→ **尚未生效**(计划中的事)
            # - 已过期的区间("我那阵子在杭州出差")→ **当前已不适用**(过去的事)
            #
            # 标错的话,LLM 会把用户明天的安排当成"已经作废"。
            if is_valid_at_pending(valid):
                parts.append("尚未生效")
            elif not is_valid_at_active(valid):
                parts.append("当前已不适用")
        if INVALIDATES_TAG in tags:
            parts.append("修正了此前的说法")
        if not parts:
            return ""
        return f"({'，'.join(parts)})"

    @staticmethod
    def _compute_superseded_keys(hits: list[dict[str, Any]]) -> set[str]:
        """算出本轮召回中**被修正 fact 覆盖**的旧 fact 的 key 集合。

        🆕 2026-11-04。Step 1 范围内解决"历史关系矛盾"的关键。

        规则(保守):
        1. 只看**带 ``invalidates:1``** 的 fact(明确说了"不再是/已终止")
        2. 只在**同一 task** 内找被覆盖的旧 fact
           (跨 task 可能是不同主题,乱标比不标更糟)
        3. 旧 fact 必须**说话时间更早**(修正总是发生在旧事实之后)
        4. 旧 fact 自己**不能**也是 invalidates fact

        为什么这些约束:
        - 规则 1:没把握的不要动
        - 规则 2:task 是我们已有的"工作单元"边界,是最保守的关联单位
        - 规则 3:防止把"后来的修正"标成"修正了后来的东西"
        - 规则 4:两条修正语句之间不该互相覆盖

        ⚠️ 这仍然是**启发式**,不是精确的实体消解。精确版要靠
        实体消解 + 图(Step 3)。当前实现的定位是:
        **在绝大多数"同一件事前后两次说法"的场景下让 LLM 拿到唯一答案**,
        而不是在所有场景下都正确。

        Args:
            hits: :meth:`recall` 的结果。

        Returns:
            被覆盖的旧 fact 的 key 集合(空集 = 没有可确定的覆盖关系)。
        """
        invalidated: list[tuple[str, str, str]] = []  # (key, task, spoken_at)
        for h in hits:
            tags = list(h.get("tags", []) or [])
            if INVALIDATES_TAG not in tags:
                continue
            tids = parse_task_tags(tags)
            invalidated.append((
                h.get("key", ""),
                tids[0] if tids else "",
                parse_spoken_at(tags),
            ))
        if not invalidated:
            return set()

        superseded: set[str] = set()
        for h in hits:
            tags = list(h.get("tags", []) or [])
            if INVALIDATES_TAG in tags:
                continue  # 修正 fact 本身不参与"被覆盖"
            key = h.get("key", "")
            tids = parse_task_tags(tags)
            task = tids[0] if tids else ""
            spoken = parse_spoken_at(tags)
            for _ikey, itask, ispoken in invalidated:
                # 必须同 task(两边都要有 task,且相同)
                if not task or not itask or task != itask:
                    continue
                # 旧 fact 的说话时间必须更早(或未知 —— 未知时不排除)
                if spoken and ispoken and spoken >= ispoken:
                    continue
                superseded.add(key)
                break
        return superseded

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


# ---------------------------------------------------------------------------
# 时间语义渲染辅助(🆕 2026-11-04)
# ---------------------------------------------------------------------------


def _describe_valid_at(valid_at: str) -> str:
    """把 valid_at 编码值翻译成**人能读、LLM 能懂**的一句话。

    编码值 → 中文描述:

    ==============================  ====================================
    编码                            描述
    ==============================  ====================================
    ``2026-01-01``                 "生效于 2026-01-01"
    ``2026-11-09/2026-11-15``      "生效于 2026-11-09 至 2026-11-15"
    ``recurring:01-29``            "每年 01-29 生效"
    ``permanent``                  "长期有效"
    ==============================  ====================================

    为什么给 LLM 看中文而不是原始编码:
    LLM 的语义理解基于自然语言。``recurring:01-29`` 这种自造编码
    对 LLM 来说是**弱语义**,翻译成"每年 01-29 生效"才真正起作用。
    """
    v = (valid_at or "").strip()
    if not v:
        return ""
    if v == "permanent":
        return "长期有效"
    if v.startswith("recurring:"):
        md = v[len("recurring:") :]
        return f"每年 {md} 生效"
    if "/" in v:
        start, _, end = v.partition("/")
        return f"生效于 {start} 至 {end}"
    return f"生效于 {v}"


__all__ = ["MemoryFacade", "MemoryContext"]
