"""🆕 2026-11-02: 记忆系统装配层(wiring)。

**为什么需要这个模块**
------------------
记忆系统的各个组件(短库 / 长库 / task 索引 / task 切分 / 调度器 /
自动抽取 / 升格 / 遗忘)在 Phase 6 里是**分别交付**的,各自都有单测,
但**从来没有被组装起来跑过** —— 线上进程里没有任何一处创建它们。

后果:
- 短期记忆(task 切分、已完成 task 摘要)从不注入 prompt
- 长期记忆(fact / 日报 / 周报)从不写入,也从不召回
- 升格管道从不执行

这个模块把"组件构造"集中到一处,让 `voice_service` 等入口只需一行::

    memory = await build_memory_system(settings)
    orchestrator = ButlerOrchestrator(..., memory_facade=memory.facade,
                                     short_term=memory.short_term)

**装配原则**
------------
1. **单一入口** —— 组件之间的依赖关系只在这里维护,不在业务代码里散落
2. **降级优先** —— 任一可选组件构造失败都不阻断主流程(记忆挂了就挂了,
   管家还能聊天)。**核心组件**(facade / short_term)失败才抛
3. **不吞异常** —— 构造失败要 warn 出来,便于排查"为什么记忆没生效"
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from smartbutler.emotion.memory.auto_extract import AutoExtractHook
from smartbutler.emotion.memory.consolidation import Consolidator
from smartbutler.emotion.memory.facade import MemoryFacade
from smartbutler.emotion.memory.forgetting import ForgettingService
from smartbutler.emotion.memory.graph.paths import resolve_graph_db_path
from smartbutler.emotion.memory.hooks import MemoryHooks
from smartbutler.emotion.memory.long_term import create_long_term_store
from smartbutler.emotion.memory.retrieval import MemoryRetriever
from smartbutler.emotion.memory.short_term import ShortTermMemory
from smartbutler.emotion.memory.summarizer import LLMScorer, LLMSummarizer
from smartbutler.emotion.memory.task_index import TaskIndex
from smartbutler.emotion.memory.task_scheduler import TaskScheduler
from smartbutler.emotion.memory.task_segmenter import TaskSegmenter
from smartbutler.emotion.memory.temporal import TemporalExtractor
from smartbutler.utils.logging import get_logger

logger = get_logger(__name__)


@dataclass
class MemorySystem:
    """装配好的记忆系统句柄。

    业务侧只需要 ``facade``(thinking 层唯一入口)和 ``short_term``,
    其余组件由本对象管理生命周期。
    """

    #: thinking 层唯一入口
    facade: MemoryFacade
    #: 短期记忆(checkpointer)
    short_term: ShortTermMemory
    #: 长期记忆存储(Qdrant)
    long_term: Any = None
    #: task 索引(SQLite)
    task_index: TaskIndex | None = None
    #: task 切分器(LLM judge)
    segmenter: TaskSegmenter | None = None
    #: 周期调度器
    scheduler: TaskScheduler | None = None
    #: 自动抽取 hook
    auto_extract: AutoExtractHook | None = None
    #: 升格管道
    consolidator: Consolidator | None = None
    #: 遗忘服务
    forgetting: ForgettingService | None = None
    #: 🆕 Phase 9:记忆图的存储(SQLite)。None = 图未启用。
    graph_store: Any = None
    #: 🆕 Phase 9:图的抽取 hook
    graph_extract: Any = None
    #: 🆕 Phase 9:声纹绑定器
    graph_binding: Any = None
    #: 事件钩子
    hooks: MemoryHooks = field(default_factory=MemoryHooks)
    #: 装配过程中的告警(非致命问题)
    warnings: list[str] = field(default_factory=list)
    #: scheduler.start() 的异步任务句柄(优雅关停时等它)
    _scheduler_task: Any = None

    def start(self) -> None:
        """启动后台调度器(task judge 周期扫描)。

        注意 :meth:`TaskScheduler.start` 是 **async**(内部要读磁盘游标),
        这里用 ``asyncio.create_task`` 起它,避免阻塞同步调用方。

        失败只 warn —— 后台任务挂掉不该让整个服务起不来。
        """
        if self.scheduler is None:
            return
        try:
            self._scheduler_task = asyncio.create_task(
                self.scheduler.start(), name="task-scheduler-start"
            )
            logger.info("memory_system.scheduler_starting")
        except Exception as e:  # noqa: BLE001
            logger.warning("memory_system.scheduler_start_failed err=%s", e)
            self.warnings.append(f"scheduler_start_failed: {e}")

    async def stop(self) -> None:
        """停止后台调度器(优雅关停)。"""
        if self.scheduler is None:
            return
        # 先等 start() 任务结束(否则可能在 stop 之后才真正开始跑)
        if self._scheduler_task is not None:
            try:
                await asyncio.wait_for(self._scheduler_task, timeout=5.0)
            except (TimeoutError, asyncio.CancelledError):
                logger.warning("memory_system.scheduler_start_task_timeout")
            except Exception as e:  # noqa: BLE001
                logger.warning("memory_system.scheduler_start_task_failed err=%s", e)
            self._scheduler_task = None
        try:
            await self.scheduler.stop()
            logger.info("memory_system.scheduler_stopped")
        except Exception as e:  # noqa: BLE001
            logger.warning("memory_system.scheduler_stop_failed err=%s", e)

    def bind_name_resolver(self, resolver: Any) -> None:
        """绑定 user_id → 显示名解析器。

        Args:
            resolver: ``fn(user_id) -> display_name``;None = 解绑。
        """
        self.facade.bind_name_resolver(resolver)


async def build_memory_system(
    settings: Any,
    *,
    llm: Any | None = None,
    data_dir: Path | None = None,
    name_resolver: Any | None = None,
    qdrant_path: Path | None = None,
    qdrant_url: str | None = None,
    qdrant_collection: str = "smartbutler_memory",
    vector_size: int = 1024,
) -> MemorySystem:
    """装配完整的记忆系统。

    Args:
        settings: :class:`~smartbutler.config.memory.MemorySettings` 实例
            (或任何有同名属性的对象)。
        llm: 可选 LLM 实例(给 judge / 评分 / 摘要用)。None → 降级到
            规则边界检测 + 启发式评分 + 拼接式摘要。
        data_dir: 数据目录(默认 ``PROJECT_ROOT / "data" / "memory"``)。
            里面放 ``task_index.db`` / ``short_term.db``。
        name_resolver: 可选 ``fn(user_id) -> display_name``,用于把召回渲染
            里的 ``speaker_a3f2b1c`` 换成"高天禹"。
        qdrant_path: Qdrant 本地存储路径(默认 ``PROJECT_ROOT/data/qdrant``)。
            **本地嵌入式 Qdrant 不支持多进程并发访问** —— 测试里必须传
            各自的 tmp_path,否则会报 "already accessed by another instance"。
        qdrant_url: Qdrant 服务地址(设了则走服务模式,忽略 qdrant_path)。
        qdrant_collection: 集合名。
        vector_size: 向量维度(须与 Embedder 维度一致)。

    Returns:
        :class:`MemorySystem`。
    """
    warnings: list[str] = []

    # ---- 数据目录 ----
    #
    # ⚠️ 路径必须走 ``paths.default_data_dir``(单一事实来源)。
    # 2026-11-09 事故:CLI 默认用 ``data/short_term.db``,运行时用
    # ``data/memory/short_term.db``,用户执行"全清理"清的是空壳库,
    # 真实数据纹丝不动 → 旧对话重启后又被注入 prompt。
    if data_dir is None:
        from smartbutler.emotion.memory.paths import default_data_dir

        data_dir = default_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)

    # ---- Embedder(向量化)----
    embedder = _build_embedder(warnings)

    # ---- 长期存储(Qdrant)----
    long_term = create_long_term_store(
        qdrant_path=qdrant_path,
        qdrant_url=qdrant_url,
        collection=qdrant_collection,
        vector_size=vector_size,
    )
    logger.info("memory_system.long_term_ready")

    # ---- 事件钩子 ----
    hooks = MemoryHooks()

    # ---- 检索器 + Facade ----
    retriever = MemoryRetriever(store=long_term, embedder=embedder)
    facade = MemoryFacade(
        retriever=retriever,
        hooks=hooks,
        long_term=long_term,
        embedder=embedder,
        n_completed_tasks=getattr(settings, "n_completed_tasks", 3),
        n_chitchat_tasks=getattr(settings, "n_chitchat_tasks", 30),
        n_prior_tasks=getattr(settings, "n_prior_tasks", 3),
    )
    facade.set_min_score(float(getattr(settings, "recall_min_score", 0.0)))

    # ---- 短期记忆 ----
    short_term = ShortTermMemory(data_dir / "short_term.db")

    # ---- task 索引 / 切分器 / 调度器 ----
    task_index = TaskIndex(data_dir / "task_index.db")
    segmenter = TaskSegmenter(
        task_index=task_index,
        llm=llm,
        llm_timeout_seconds=float(getattr(settings, "judge_timeout_seconds", 8.0)),
    )

    scheduler = TaskScheduler(
        short_term=short_term,
        task_index=task_index,
        segmenter=segmenter,
        interval_seconds=float(getattr(settings, "judge_interval_seconds", 3600.0)),
        on_message_enabled=bool(getattr(settings, "judge_on_message", False)),
        min_interval_seconds=float(getattr(settings, "judge_min_interval_seconds", 60.0)),
        on_message_debounce_seconds=float(
            getattr(settings, "on_message_debounce_seconds", 0.0)
        ),
        paused_max_staleness=int(getattr(settings, "paused_max_staleness", 5)),
        judge_window_size=int(getattr(settings, "judge_window_size", 2)),
    )

    # ---- 自动抽取 hook(短期 → 长期 fact)----
    auto_extract: AutoExtractHook | None = None
    if bool(getattr(settings, "auto_extract_enabled", True)):
        # 🆕 2026-11-04: 时间抽取器 —— 把"明天""下周一"绝对化。
        #
        # 为什么挂在这里而不是 consolidator:
        # 时间是**fact 级**的属性(每条 fact 有自己的 spoken_at 和 valid_at),
        # 不是摘要级的。日报是很多 fact 的聚合,没法再抽单条 fact 的时间。
        #
        # llm=None → TemporalExtractor 降级为"不抽 valid_at",
        # 但 spoken_at(代码取)照常写 —— 不会因为没配 LLM 就完全丢失时间。
        temporal_extractor = TemporalExtractor(llm)
        auto_extract = AutoExtractHook(
            facade=facade,
            short_term=short_term,
            task_index=task_index,
            min_importance=float(
                getattr(settings, "auto_extract_min_importance", 0.0)
            ),
            temporal_extractor=temporal_extractor,
        )
        auto_extract.register(hooks)

    # ---- 🆕 Phase 9:记忆图(人物关系图)----
    #
    # 总开关 ``graph_enabled`` 默认 False —— 图是**增强层**,灰度打开。
    # 关闭时零副作用:不建表、不抽取、不召回,**并且把 resolve_person
    # tool 注销掉** —— 让 LLM 看不见它,避免它白跑一趟再返回
    # "记忆图功能未启用"。
    graph_store = None
    graph_extract = None
    graph_binding = None
    if bool(getattr(settings, "graph_enabled", False)):
        try:
            from smartbutler.capabilities.tools.graph import resolve_person as graph_tool
            from smartbutler.emotion.memory.graph.binding import VoiceBinding
            from smartbutler.emotion.memory.graph.contradiction import (
                ContradictionResolver,
            )
            from smartbutler.emotion.memory.graph.extractor import GraphExtractor
            from smartbutler.emotion.memory.graph.hooks import GraphExtractHook
            from smartbutler.emotion.memory.graph.resolver import PersonResolver
            from smartbutler.emotion.memory.graph.store import GraphStore
            from smartbutler.emotion.memory.graph.writer import GraphWriter

            # 路径解析统一走 paths.resolve_graph_db_path(CLI 也用同一个)
            graph_db_path = resolve_graph_db_path(
                getattr(settings, "graph_db_path", "graph.db"),
                data_dir=data_dir,
            )

            graph_store = GraphStore(graph_db_path)
            graph_resolver = PersonResolver(store=graph_store)
            graph_contradiction = ContradictionResolver(
                store=graph_store,
                llm=llm,
                dedupe_threshold=float(
                    getattr(settings, "graph_dedupe_threshold", 0.85)
                ),
            )
            graph_writer = GraphWriter(
                store=graph_store,
                resolver=graph_resolver,
                contradiction=graph_contradiction,
            )
            graph_binding = VoiceBinding(store=graph_store, resolver=graph_resolver)

            # tool 依赖注入(resolve_person 是对话中建关系的入口)
            graph_tool.set_graph_dependencies(
                store=graph_store,
                resolver=graph_resolver,
                writer=graph_writer,
            )
            # 重新注册(可能被之前"图关闭"的装配注销过)
            _reregister_graph_tool()

            graph_extract = GraphExtractHook(
                extractor=GraphExtractor(
                    llm=llm,
                    max_edges=int(
                        getattr(settings, "graph_edge_max_per_task", 20)
                    ),
                ),
                writer=graph_writer,
                task_index=task_index,
                enabled=True,
                max_edges_per_task=int(
                    getattr(settings, "graph_edge_max_per_task", 20)
                ),
            )
            graph_extract.register(hooks)

            # 召回通道:把图边作为**补充**注入 prompt
            facade.bind_graph_store(
                graph_store,
                max_edges=int(getattr(settings, "graph_recall_max_edges", 10)),
            )

            logger.info(
                "memory_system.graph_enabled db=%s", graph_db_path,
            )
        except Exception as e:  # noqa: BLE001
            warnings.append(f"graph_init_failed: {e}")
            logger.warning("memory_system.graph_init_failed err=%s", e)
            graph_store = None
            graph_extract = None
            graph_binding = None
            _unregister_graph_tool()
    else:
        # 图关闭 → 注销 tool,让 LLM 看不见它
        _unregister_graph_tool()

    # ---- 升格管道(fact → 日报 → 周报 → 月报)----
    consolidator = Consolidator(
        store=long_term,
        embedder=embedder,
        llm_scorer=LLMScorer(llm),
        llm_summarizer=LLMSummarizer(llm),
        hooks=hooks,
    )
    facade.bind_consolidator(consolidator)

    # ---- 遗忘服务 ----
    forgetting = ForgettingService(long_term)
    facade.bind_forgetting(forgetting)

    # ---- 短期视图(按 task 池切窗)----
    # 注入后短期块走"task 池"路径:未完成 / 已完成 / 闲聊分组呈现,
    # 这正是"记忆以 task 维度组织、让 LLM 知道最近大家在做什么"的短期侧。
    try:
        from smartbutler.emotion.memory.short_term_view import ShortTermView

        saver = await short_term.get_async_saver()
        view = ShortTermView(
            saver=saver,
            task_index=task_index,
            n_completed_tasks=int(getattr(settings, "n_completed_tasks", 3)),
            n_chitchat_tasks=int(getattr(settings, "n_chitchat_tasks", 30)),
            n_prior_tasks=int(getattr(settings, "n_prior_tasks", 3)),
        )
        facade.bind_short_term_view(view)
    except Exception as e:  # noqa: BLE001
        warnings.append(f"short_term_view_failed: {e}")
        logger.warning("memory_system.short_term_view_failed err=%s", e)

    facade.bind_task_scheduler(scheduler)

    # ---- 显示名解析 ----
    if name_resolver is not None:
        facade.bind_name_resolver(name_resolver)

    system = MemorySystem(
        facade=facade,
        short_term=short_term,
        long_term=long_term,
        task_index=task_index,
        segmenter=segmenter,
        scheduler=scheduler,
        auto_extract=auto_extract,
        consolidator=consolidator,
        forgetting=forgetting,
        graph_store=graph_store,
        graph_extract=graph_extract,
        graph_binding=graph_binding,
        hooks=hooks,
        warnings=warnings,
    )

    logger.info(
        "memory_system.built auto_extract=%s llm=%s warnings=%d",
        auto_extract is not None, llm is not None, len(warnings),
    )
    return system


def _reregister_graph_tool() -> None:
    """🆕 Phase 9: 重新注册 ``resolve_person`` tool(图开启时)。

    **为什么需要**:tool 是模块级 ``@register_tool`` 注册的 —— 只在
    import 时执行一次。若之前有一次装配把图关掉了(→ 注销了 tool),
    后来再开启时不会自动注册回来。

    重新注册的代价是覆盖注册表里的同�� tool(实现不变,只是依赖更新),
    不会有副作用。
    """
    try:
        from smartbutler.capabilities.tools.graph.resolve_person import (
            resolve_person,
        )
        from smartbutler.capabilities.tools.registry import ToolRegistry

        registry = ToolRegistry.get_default()
        if registry.try_get("resolve_person") is not None:
            return
        # 重新走一遍注册流程
        from smartbutler.capabilities.tools.decorator import register_tool

        tool = register_tool(
            name="resolve_person",
            description=(
                "登记或查询一个人,并建立人与人之间的关系。\n"
                "场景:用户提到某个人(如'我妈''我同事李雷'),"
                "当需要建立人际关系、或系统不知道这个人是谁时调用。\n"
                "参数:\n"
                "  display_name      —— 对方的名字(必填)\n"
                "  relation_type     —— 关系类型(必填),如 IS_MOTHER_OF / "
                "IS_FRIEND_OF / WORKS_WITH / IS_FATHER_OF / IS_COLLEAGUE_OF / KNOWS\n"
                "  speaker           —— 说话人的 user_id(从上下文取,必填)\n"
                "  candidate_node_id —— 可选。**第二轮确认时传**:用户已确认是哪一个候选人\n"
                "返回值 status=needs_confirmation 表示图里已有同名的人,"
                "请把 candidates 里的描述转述给用户听(如'是不是这个人:张伟,我的同事?'),"
                "等用户回答后再调用一次。\n"
                "  - 用户说'是' → 带上 candidate_node_id 再调一次\n"
                "  - 用户说'不是'/'同名' → **不要**带 candidate_node_id,直接再调一次"
                "(系统会自动建新节点区分这两个人)"
            ),
        )(resolve_person)
        logger.info("memory_system.graph_tool_reregistered tool=%s", tool.name)
    except Exception as e:  # noqa: BLE001
        logger.warning("memory_system.graph_tool_reregister_failed err=%s", e)


def _unregister_graph_tool() -> None:
    """🆕 Phase 9: 注销 ``resolve_person`` tool(图关闭时)。

    **为什么**:tool 是模块级 ``@register_tool`` 注册的 —— 进程启动就
    在注册表里了,和 ``graph_enabled`` 无关。图关闭时 LLM 仍能看到它,
    调它只会得到"记忆图功能未启用",白烧一次工具调用。

    注销掉,LLM 就看不见它了(它本来也用不了)。
    """
    try:
        from smartbutler.capabilities.tools.registry import ToolRegistry

        registry = ToolRegistry.get_default()
        if registry.try_get("resolve_person") is None:
            return
        registry.unregister("resolve_person")
        logger.info("memory_system.graph_tool_unregistered")
    except Exception as e:  # noqa: BLE001
        logger.debug("memory_system.graph_tool_unregister_skipped err=%s", e)


def _build_embedder(warnings: list[str]) -> Any:
    """构造 Embedder。

    优先用项目默认实现;失败时返回一个**恒零向量**的降级实现 ——
    记忆系统此时功能受限(召回不准)但不会崩,管家还能正常对话。
    """
    try:
        from smartbutler.capabilities.embedding import build_default_embedder

        return build_default_embedder()
    except Exception as e:  # noqa: BLE001
        warnings.append(f"embedder_build_failed: {e}")
        logger.warning("memory_system.embedder_build_failed err=%s", e)
        return _ZeroEmbedder()


class _ZeroEmbedder:
    """降级 Embedder —— 恒返回零向量。

    仅在默认 Embedder 构造失败时使用(通常是模型文件缺失)。
    召回会变得不准确,但链路不崩。
    """

    #: 向量维度(Qdrant collection 建库时定的)
    dim: int = 1024

    async def embed(self, text: str) -> list[float]:  # noqa: ARG002
        return [0.0] * self.dim

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [[0.0] * self.dim for _ in texts]


__all__ = ["MemorySystem", "build_memory_system"]
