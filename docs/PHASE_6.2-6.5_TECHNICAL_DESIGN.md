# Phase 6.2-6.5 技术方案:Memory 完整化

> **范围**:仅 Memory 4 个子阶段(部分已划入后续扩展)
> **不包含**:Personality(6.6)/ Learn(6.7)/ Phase 6b ProactiveLoop 真实化
> **前置依赖**:Phase 6.1(已交付) + qwen3.7-text-embedding-flash(已决策) + langmem 0.0.30(已决策,走 capabilities/langmem 抽象层)
> **关联 ADR**:`TECHNICAL_DESIGN.md §5.9`(Proactive 真实化需 Memory 数据) + `README.md` Phase 6 子阶段表

> **🆕 重大变更(2026-10-10 经讨论)**:原 6.2-6.5 4 个子阶段重新规划——
> **P0(必做,当前 1-2 周)** = 6.2 高级 API(让 thinking 真接入)
> **P1(后续扩展)** = 6.3 固化遗忘(DEFERRED) / 6.4 层次总结 / 6.5 habit(DEFERRED)
> **理由**:原计划做了大量"记忆管理基础设施"但没接通业务,6.1 后 thinking 仍 0 调用方。先把"想得起 + 想得准"打通,再谈"忘得掉 / 记得巧"。

---

## 0. 当前状态盘点(2026-10-10)

### 0.1 已交付(Phase 6.1)

| 模块 | 文件 | 状态 |
|------|------|------|
| **BaseStorage Protocol** | `smartbutler/storage/base.py` | ✅ |
| **SQLiteStorage** | `smartbutler/storage/sqlite.py` | ✅ |
| **QdrantStorage** | `smartbutler/storage/qdrant.py` | ✅(默认值已统一 1024) |
| **ShortTermMemory** | `smartbutler/emotion/memory/short_term.py` | ✅ |
| **LongTermStore** | `smartbutler/emotion/memory/long_term.py` | ✅(默认值已统一 1024) |
| **thinking 接入** | — | ❌ **完全没有**(grep 验证 0 调用方) |

**确认:Phase 6.1 给的接口已经预留了 6.2 钩子**(`importance: float` 已存 payload,但没被 Qdrant 索引过滤;`min_importance` 走客户端二次过滤)。

### 0.2 本方案要做 / 后续扩展

| 阶段 | 文件 | 状态 | 何时做 |
|------|------|------|--------|
| **6.2 capabilities/embedding** | `capabilities/embedding/{base.py, qwen.py, factory.py}` | 🆕 待做 | **P0 当前** |
| **6.2 capabilities/langmem** | `capabilities/langmem/{base.py, _v0_0_30.py, _fallback.py, factory.py}` | 🆕 待做 | **P0 当前** |
| **6.2 embedder 薄包装** | `emotion/memory/embedder.py` | 🆕 待做 | **P0 当前** |
| **6.2 importance 评分** | `emotion/memory/importance.py` | 🆕 待做 | **P0 当前** |
| **6.2 retrieval** | `emotion/memory/retrieval.py` | 🆕 待做 | **P0 当前** |
| **6.2 高级 API(thinking 接入层)** | `emotion/memory/facade.py` | 🆕 待做 | **P0 当前** |
| **6.2 thinking 接入** | `thinking/loop/` 改造 | 🆕 待做 | **P0 当前** |
| 6.2 hooks 钩子 | `emotion/memory/hooks.py` | 🆕 待做 | **P0 当前**(空实现,EventType 枚举 + register/trigger 完整) |
| **6.3 consolidation + forgetting** | `emotion/memory/{consolidation,forgetting}.py` | ⏸ DEFERRED | 后续扩展 |
| **6.3 forgetting 软删除** | (并入 forgetting.py) | ⏸ DEFERRED | 后续扩展 |
| **6.4 层次化总结** | `emotion/memory/summarizer.py` | ⏸ DEFERRED | 后续扩展 |
| **6.4 情景记忆** | `emotion/memory/episodic.py` | ⏸ DEFERRED | 后续扩展 |
| **6.5 habit** | `emotion/memory/habit.py` | ⏸ DEFERRED | 后续扩展 |
| **6.5 cross_thread** | `emotion/memory/cross_thread.py` | ⏸ DEFERRED | 后续扩展 |

**确认:Phase 6.1 给的接口已经预留了 6.2 钩子**(`importance: float` 已存 payload,但没被 Qdrant 索引过滤;`min_importance` 走客户端二次过滤)。

---

## 1. 整体架构

### 1.1 模块依赖图

```
┌──────────────────────────────────────────────────────────────────────┐
│                       Memory 子模块分层                                │
├──────────────────────────────────────────────────────────────────────┤
│                                                                      │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐             │
│  │ embedder │  │importance│  │retrieval │  │ episodic │             │
│  │ (抽象 +  │  │(LLM 评分)│  │(语义 + 衰│  │(对话轨迹)│             │
│  │ 2 实现)  │  │          │  │  减 + 过滤)│  │          │             │
│  └────┬─────┘  └────┬─────┘  └────┬─────┘  └────┬─────┘             │
│       │             │             │             │                   │
│       └─────────────┴──────┬──────┴─────────────┘                   │
│                            ▼                                         │
│                  ┌──────────────────┐                                │
│                  │  LongTermStore   │  ← 6.1 已交付,本阶段加 langmem  │
│                  │  (QdrantStorage) │                                  │
│                  └────────┬─────────┘                                │
│                           │                                          │
│       ┌───────────────────┼───────────────────┐                      │
│       ▼                   ▼                   ▼                      │
│  ┌──────────┐       ┌──────────┐       ┌──────────┐                  │
│  │consolida-│       │ forgetting│       │summarizer│                 │
│  │tion(固化)│       │(遗忘)    │       │(层次总结)│                  │
│  └────┬─────┘       └────┬─────┘       └────┬─────┘                 │
│       │                  │                  │                        │
│       └──────┬───────────┴──────────┬───────┘                        │
│              ▼                      ▼                                │
│       ┌──────────┐            ┌──────────┐                          │
│       │  habit   │            │ ShortTerm│  ← 6.1 已交付              │
│       │ (习惯)   │            │ Memory   │                            │
│       └──────────┘            └──────────┘                          │
│                                                                      │
└──────────────────────────────────────────────────────────────────────┘
```

### 1.2 三大不变量(全文遵守)

1. **3 层存储严格分离** — 短期 = SqliteSaver(checkpointer)/ 长期事实 = Qdrant / 长期情景 = SQLite JSON
2. **不重写 langmem** — 6.2 装 langmem,但只用其抽象接口;具体检索/评分能用自研就用自研
3. **可降级** — langmem 装不上 / Embedder 不可用 / Qdrant 挂 → 全链路降级到 SQLiteStorage,管家不崩

---

## 2. P0:Phase 6.2 — thinking 接入 + 高级 API(预计 1-2 周,~990 行,含测试)

> **🆕 核心调整**:不再追求"6.2 评分 + 6.3 固化 + 6.4 总结"的串行推进。
> 改为**先把 thinking 接入跑通**,再补 6.3-6.5 的扩展能力。

### 2.0 本阶段唯一目标

`thinking/loop/ButlerOrchestrator` 在用户消息进来时,能调用 memory 召回相关历史 → 注入 system prompt → LLM 看到"用户之前说过 X" → 用户感受到"管家记得"。

**为后续扩展预留的口子(不实现,只留 API)**:
- `MemoryHooks`(4 事件:on_store / on_recall / on_promote / on_habit_detected)—— 注册/触发完整,callback 全 stub
- `importance` 评分字段已存 Qdrant payload,后续要加 LLM 评分只改 `ImportanceScorer` 不改存储
- 高级 API 暴露 `source` 参数(说话人/来源,可空字符串),后续多用户场景不需改 facade
- `MemoryContext` 暴露 `user_id` 字段(可空,空 = 单用户模式),后续多用户场景不需改 facade

### 2.1 新增文件

| 文件 | 行数 | 职责 |
|------|------|------|
| `capabilities/embedding/__init__.py` | ~10 | 公共导出 |
| `capabilities/embedding/base.py` | ~30 | `Embedder` Protocol + 异常 |
| `capabilities/embedding/qwen.py` | ~60 | `QwenEmbedder` 实现(`qwen3.7-text-embedding-flash`) |
| `capabilities/embedding/factory.py` | ~20 | `build_default_embedder()`(按 env 选 + 降级) |
| `capabilities/embedding/config.py` | ~40 | `EmbeddingSettings`(`SMARTBUTLER_EMBEDDING_*` 前缀) |
| `capabilities/langmem/__init__.py` | ~10 | 公共导出 |
| `capabilities/langmem/base.py` | ~30 | `BaseLangmemAdapter` ABC + 异常 |
| `capabilities/langmem/_v0_0_30.py` | ~60 | `Langmem0030Adapter` 实现(软依赖 try-import) |
| `capabilities/langmem/_fallback.py` | ~50 | `RegexFactExtractor`(langmem 装不上时接管) |
| `capabilities/langmem/factory.py` | ~20 | `build_default_adapter()` |
| `emotion/memory/embedder.py` | ~30 | 业务层薄包装(只 import Protocol) |
| `emotion/memory/importance.py` | ~40 | `HeuristicScorer` + `ImportanceScorer`(本阶段只走启发式) |
| `emotion/memory/retrieval.py` | ~50 | `MemoryRetriever`(组合 Embedder + Qdrant + 衰减) |
| `emotion/memory/hooks.py` | ~60 | `MemoryHooks` + `MemoryEventType` 枚举 |
| `emotion/memory/facade.py` | ~80 | **`MemoryFacade`(thinking 唯一入口)** |
| `tests/unit/capabilities/test_embedding_*.py` | ~80 | Embedder 单测 |
| `tests/unit/capabilities/test_langmem_*.py` | ~60 | langmem 抽象层单测 |
| `tests/unit/emotion/memory/test_retrieval.py` | ~50 | retriever 单测 |
| `tests/unit/emotion/memory/test_importance.py` | ~30 | importance 单测 |
| `tests/unit/emotion/memory/test_facade.py` | ~50 | facade 单测 |
| `tests/unit/emotion/memory/test_hooks.py` | ~30 | hooks 单测 |
| `tests/unit/storage/test_qdrant_range.py` | ~20 | Qdrant range query 补丁单测 |
| `tests/integration/memory/test_thinking_integration.py` | ~80 | 端到端:用户消息 → 召回 → 注入 → LLM 看到 |

**合计**:~990 行(代码 ~630 + 测试 ~340 + 配置 ~20)。**比原 6.2 计划(~100 行)多 9 倍**,因为加进了 thinking 接入层 + 抽象层。

### 2.2 高级 API 设计(facade.py)—— 核心

**`MemoryFacade`** 是 thinking 层唯一调用入口。所有扩展能力(consolidation/summarizer/habit)都通过 facade 的方法对外暴露,而不是让 thinking 直接 import 具体类。

```python
# smartbutler/emotion/memory/facade.py
@dataclass
class MemoryContext:
    """调用方提供的上下文(可空,空时退化到无 user 模式)"""
    user_id: str | None = None      # 暂时不强,空 = 单用户模式
    source: str = ""                # 说话人/来源("男主人"/"女主人"),可空
    session_id: str | None = None   # 跨 thread 标识预留

class MemoryFacade:
    """Memory 高级 API —— thinking 层唯一入口。

    设计原则:
    1. thinking 只 import MemoryFacade,不感知底层(Embedder/Retriever/Hooks)
    2. 所有方法都是非阻塞 async(Qdrant/Embedder 失败快速降级)
    3. 内部统一调 MemoryHooks 触发事件
    """

    def __init__(
        self,
        retriever: MemoryRetriever,
        hooks: MemoryHooks,
        long_term: LongTermStore,
        embedder: Embedder,           # 暴露出来给 thinking 直接 embed(预留,本阶段不调)
    ): ...

    async def remember(
        self, content: str, *, ctx: MemoryContext | None = None,
        tags: list[str] | None = None,
        importance: float | None = None,  # 不传 = 启发式 0.5
    ) -> str:
        """存一条长期记忆。自动 embed + importance 评分(默认启发式)+ 存。"""
        key = await self._retriever.remember(
            content, tags=tags, importance=importance,
            source=ctx.source if ctx else "",
        )
        self._hooks.trigger(MemoryEventType.ON_STORE, key=key, content=content)
        return key

    async def recall(
        self, query: str, *, ctx: MemoryContext | None = None,
        k: int = 5, min_importance: float = 0.3,
    ) -> list[dict]:
        """召回相关记忆。语义检索 + 重要性过滤 + 时间衰减(half_life=30 天)。"""
        # 用 ctx.source 作 tag 过滤(多用户场景下不串)
        tag = f"source:{ctx.source}" if ctx and ctx.source else None
        hits = await self._retriever.recall(
            query, k=k, min_importance=min_importance, tag=tag,
        )
        for h in hits:
            self._hooks.trigger(MemoryEventType.ON_RECALL, key=h["key"], score=h["score"])
        return hits

    async def format_for_prompt(
        self, query: str, *, ctx: MemoryContext | None = None,
        k: int = 5, max_chars: int = 2000,
    ) -> str:
        """召回 + 格式化成可注入 system prompt 的 markdown 片段。

        返回示例:
            "### 相关历史记忆(共 3 条)\n- 用户之前说:10.8 要去迪士尼\n- ..."

        空召回返回空字符串。
        """
        hits = await self.recall(query, ctx=ctx, k=k)
        if not hits:
            return ""
        lines = [f"### 相关历史记忆(共 {len(hits)} 条)"]
        for h in hits:
            content = h["content"][:200]
            lines.append(f"- {content}")
        block = "\n".join(lines)
        if len(block) > max_chars:
            block = block[:max_chars] + "..."
        return block
```

**关键设计**:
- `ctx` 可空:thinking 不传也能跑(单用户模式),后续多用户场景加 ctx 不需改 facade
- `format_for_prompt` 是 thinking 的"糖"—— 一行调用 = 召回 + 格式化 + 截断
- 所有方法调 `MemoryHooks.trigger()` —— 扩展能力的接入点
- `recall` 用 `source` 作 tag 过滤(问题 10 答案的最小实现)—— 后续 6.3 加 `user_id` payload 字段做强隔离

### 2.3 MemoryHooks 设计(hooks.py)

```python
# smartbutler/emotion/memory/hooks.py
class MemoryEventType(StrEnum):
    ON_STORE = "on_store"                    # 长期记忆写入
    ON_RECALL = "on_recall"                  # 长期记忆被召回
    ON_PROMOTE = "on_promote"                # 6.3 固化触发(留口子,本阶段不调)
    ON_HABIT_DETECTED = "on_habit_detected"  # 6.5 习惯发现(留口子,本阶段不调)

class MemoryHooks:
    """Memory 事件钩子 —— 6.2 实现注册/触发机制,callback 全 stub。

    设计要点:
    1. 单实例(facade 创建时注入,thinking 共享)
    2. 触发必须 try/except —— 钩子挂不影响主流程
    3. 当前阶段所有 callback 内部只 logger.info,不做事
    """

    def __init__(self) -> None:
        self._callbacks: dict[MemoryEventType, list[Callable]] = {
            t: [] for t in MemoryEventType
        }

    def register(self, event: MemoryEventType, callback: Callable) -> None:
        """注册回调。同 event 可注册多个,按注册顺序触发。"""
        self._callbacks[event].append(callback)

    def trigger(self, event: MemoryEventType, *args: Any, **kwargs: Any) -> None:
        """触发所有 callback。任何一个抛异常,捕获 + warn,不影响其他。"""
        for cb in self._callbacks[event]:
            try:
                cb(*args, **kwargs)
            except Exception as exc:
                logger.warning(
                    "memory_hook callback failed", event=event, error=str(exc),
                )

    def default_on_store(self, *, key: str, content: str) -> None:
        """默认 stub:只记日志。后续 6.3 在此加 consolidation 触发。"""
        logger.info("memory.on_store", key=key, content_preview=content[:50])

    def default_on_recall(self, *, key: str, score: float) -> None:
        """默认 stub。后续可加"被频繁召回的 fact 提升 importance"逻辑。"""
        logger.info("memory.on_recall", key=key, score=score)
```

### 2.4 Embedder 抽象层(capabilities/embedding/)

```python
# capabilities/embedding/base.py
class Embedder(Protocol):
    async def embed(self, text: str) -> list[float]: ...
    async def embed_batch(self, texts: list[str]) -> list[list[float]]: ...
    @property
    def dimension(self) -> int: ...

# capabilities/embedding/qwen.py
class QwenEmbedder:
    """调 qwen3.7-text-embedding-flash,1024 维。"""
    def __init__(self, settings: EmbeddingSettings): ...
    async def embed(self, text: str) -> list[float]: ...

# capabilities/embedding/factory.py
def build_default_embedder(settings: EmbeddingSettings | None = None) -> Embedder:
    """按 env 选实现:QwenEmbedder 优先;Qwen 失败/未配 → StubEmbedder(hash)。"""
    s = settings or load_embedding_settings()
    try:
        return QwenEmbedder(s)
    except Exception as e:
        logger.warning("QwenEmbedder 初始化失败,降级 StubEmbedder: %s", e)
        return StubEmbedder(dimension=s.dimension)
```

`.env` 配置(`SMARTBUTLER_EMBEDDING_*`):
- `API_KEY` / `BASE_URL` / `MODEL`(默认 `qwen3.7-text-embedding-flash`)
- `DIMENSION`(默认 1024,跟 `SMARTBUTLER_STORAGE_QDRANT_VECTOR_SIZE` 启动期校验一致)

### 2.5 importance 评分(启发式优先,本阶段不调 LLM)

```python
# emotion/memory/importance.py
class HeuristicScorer:
    """无 LLM 时的启发式:
    含数字/姓名/日期 +0.3,长度 > 100 +0.2,情绪词 +0.2
    范围 [0.0, 1.0]
    """
    def score(self, content: str) -> float: ...

class ImportanceScorer:
    """LLM 评分 + 启发式兜底。

    6.2 阶段策略:不主动调 LLM,只用 HeuristicScorer。
    6.3 接 consolidation 时,这里加 LLM 评分逻辑(只对候选 promote 的 fact 评)。
    """
    def __init__(self, llm: BaseLLM | None = None): ...
    async def score(self, content: str) -> float:
        return self._heuristic.score(content)
```

### 2.6 MemoryRetriever(retrieval.py)

```python
class MemoryRetriever:
    def __init__(
        self,
        store: LongTermStore,
        embedder: Embedder,
        scorer: ImportanceScorer,
        decay_half_life_days: float = 30.0,  # 6.2 固定 30 天
    ): ...

    async def remember(
        self, content: str, *, tags: list[str] | None = None,
        source: str = "", importance: float | None = None,
    ) -> str:
        """自动 embed + score(默认启发式 0.5)+ 存。"""
        emb = await self.embedder.embed(content)
        score = importance if importance is not None else await self.scorer.score(content)
        # 把 source 合并进 tags(多用户场景下 recall 过滤用)
        all_tags = list(tags or [])
        if source:
            all_tags.append(f"source:{source}")
        key = self._make_key(source)
        self.store.store_memory(key, emb, content, importance=score, tags=all_tags)
        return key

    async def recall(
        self, query: str, *, k: int = 5, min_importance: float = 0.3,
        tag: str | None = None, now: datetime | None = None,
    ) -> list[dict]:
        emb = await self.embedder.embed(query)
        # Qdrant 走 range query: importance >= min_importance(避免客户端二次过滤)
        # 取 2 倍候选,客户端按时间衰减重排
        hits = self.store.similarity_search_with_filters(
            emb, k=k * 2, min_importance=min_importance, tag=tag,
        )
        # 衰减: score' = score * exp(-Δdays / 30 * ln 2)
        return self._rerank(hits, now=now)[:k]
```

### 2.7 QdrantStorage 补丁(storage/qdrant.py 加 ~10 行)

为支持 `importance >= x` 走服务端过滤,加方法:

```python
def similarity_search_with_filters(
    self, embedding, *, k, min_importance=None, tag=None,
):
    conditions = []
    if tag:
        conditions.append(FieldCondition(key="tags", match=MatchValue(value=tag)))
    if min_importance is not None:
        conditions.append(
            FieldCondition(key="importance", range=Range(gte=min_importance))
        )
    # 走 Qdrant 服务端过滤
    ...
```

### 2.8 thinking 接入(thinking/loop/)

**`ButlerOrchestrator.decide_node` 改造**(伪代码):

```python
async def decide_node(state: ButlerState) -> dict:
    # 1. 召回相关历史
    ctx = MemoryContext(source=state.get("current_user", ""))
    history_block = await memory_facade.format_for_prompt(
        query=state["user_input"], ctx=ctx, k=5, max_chars=2000,
    )

    # 2. 注入 system prompt
    system_prompt = butler_prompt_builder.build(
        user_input=state["user_input"],
        skills=skill_runtime.render_prompt_snippet(),
        memory_block=history_block,  # 🆕 新增
    )

    # 3. 调 LLM(原逻辑不动)
    response = await llm.chat(...)
    ...
```

**`ButlerPromptBuilder.build` 加 `memory_block` 参数**:
- 空字符串 → 拼"(无相关历史记忆)"
- 非空 → 拼"## 相关历史记忆\n{history_block}"

**`ButlerOrchestrator.__init__` 加 `memory_facade: MemoryFacade` 参数**—— 由 `loop_factory` 在创建时注入。

### 2.9 不变量(本阶段)

- **thinking 不直接 import Embedder/Retriever/Hooks** —— 只 import `MemoryFacade`
- **MemoryFacade 不直接 import QwenEmbedder/Langmem0030Adapter** —— 只 import Protocol
- **降级链**:Embedder 失败 → StubEmbedder;Qdrant 失败 → SQLiteStorage LIKE 检索;LLM 评分失败 → 启发式 0.5;langmem 失败 → 正则 fallback。**任何环节失败,管家不崩**。
- **重要性评分不调 LLM**(本阶段)—— 6.3 接 consolidation 时再补
- **多用户隔离**:retriever 自动把 `source` 写入 tags,recall 自动按 source 过滤(最小隔离)
- **降级路径有降级**:`format_for_prompt` 失败 → 返回空字符串 → thinking 看到"无相关历史"继续工作

### 2.10 测试矩阵(目标 ~25 用例)

| 类别 | 数量 | 覆盖 |
|------|------|------|
| Embedder 协议 + Qwen 路径 + Stub 降级 | 5 | 协议签名 / Qwen mock 成功 / Qwen mock 401 / Stub 确定性 / 工厂降级 |
| langmem 抽象层 + fallback | 5 | 协议签名 / _v0_0_30 成功 / _fallback 接管 / 工厂自动选 / 装不上路径 |
| HeuristicScorer | 3 | 默认 0.5 / 含数字+0.3 / 情绪词+0.2 |
| MemoryRetriever | 5 | remember 流程 / recall 衰减 / importance 过滤 / tag 过滤 / 空 query |
| MemoryFacade | 4 | remember 调 retriever / recall 调 retriever / format_for_prompt 截断 / source tag 隔离 |
| MemoryHooks | 3 | register / trigger / 异常隔离 |
| Qdrant range query 补丁 | 2 | range 字段格式 / 多条件 AND |
| thinking 接入(集成测试) | 1 | mock 1 轮对话,验证 prompt 含 history_block |

---

## 2.5 P1:后续扩展(暂不实现,只标记方向)

> **6.2 落地后,本节作为 backlog 持续维护。每个扩展启动前再开 ADR 拍板细节。**

### 2.5.1 Phase 6.3 — 短期固化 + 4 层分片 + 遗忘(2026-10-10 敲定设计)

**触发条件**:Phase 6.2 P0 已交付 ✅ + 用户产生"记太多/记错"反馈 或 LLM 评分能力有提升。

**设计目标**:把"只进不出"的短期记忆,变成"有进有出、有升有降"的 4 层分片体系。

#### 2.5.1.1 整体架构(4 层分片 + 同 collection 存)

```
┌──────────────────────────────────────────────────────────────────────┐
│                       Memory 4 层分片体系                              │
├──────────────────────────────────────────────────────────────────────┤
│                                                                      │
│  L0 raw(短期)         SqliteSaver 全量存,只供 consolidation 喂入      │
│     ↓ task boundary + LLM 评分                                         │
│  L1 daily             24h 内已收尾 task 的事实 + 触发事件     │ 30 天压缩  │
│     ↓ 7d 触发                                                            │
│  L2 weekly            7 天 daily 摘要的归纳                  │ 90 天压缩  │
│     ↓ 30d 触发                                                           │
│  L3 monthly           30 天 weekly 摘要的归纳                │ 365 天压缩 │
│     ↓ 跨年触发                                                           │
│  L4 permanent         持久事实(偏好/关系/约定)              │ 永不压缩   │
│                                                                      │
│  ┌────────────────┐  ┌────────────────┐  ┌────────────────┐          │
│  │ consolidation  │  │  forgetting    │  │  soft_delete   │          │
│  │ (L0→L1→L2→L3)  │  │  (5 类)        │  │  (可恢复)      │          │
│  └────────────────┘  └────────────────┘  └────────────────┘          │
│                                                                      │
└──────────────────────────────────────────────────────────────────────┘
```

**关键决策**:
- **4 层用 tag 存同一 Qdrant collection**(不分物理 collection)—— `level:daily` / `level:weekly` / `level:monthly` / `level:permanent` + `level:raw`(raw 不进 Qdrant,只在 SqliteSaver)
- **recall 时按 tag 过滤**:`facade.recall(level="weekly", ...)` — 默认走 `level:daily + level:weekly + level:permanent`,跳过 `level:raw`
- **升格 = 写新条目(level 升) + 软删除旧条目**(`payload["deleted"]=True`),**不物理删除**
- **why 字段必须用户原话引述** —— LLM 不准编"为什么",仅在用户明确说了才填

#### 2.5.1.2 短期记忆切分(task 边界 + 滑窗)

**痛点**(2026-10-10 经讨论):
- 管家使用场景是"平时不聊天,用到时一句指令" — 时间窗口拼出来的短期记忆常常是空 / 或周末一次性塞 200 条
- 按消息数硬截断会砍掉任务执行中的关键中间步骤

**解决方案 — task 边界 + 滑窗**:
- **Task 边界判定**(`ButlerState` 新增 `task_boundary: bool`):
  - 规则 1:用户消息间隔 > N 分钟(默认 30)→ 新 task
  - 规则 2:LLM 在响应结束时判 `is_new_task_started`(轻量 LLM 调用,或用规则触发词如"再做一件事" / "另外")
  - 规则 3:管家明确回复"任务完成"后用户首次发言 → 新 task
- **窗口内**: 完整保留当前 task 全部对话(不加截断)
- **跨 task**: 只带"上一 task 的最终答复 + 关键事实"作为锚点(上一 task 摘要,非原文)
- **注入到 system prompt 的"短期片段"**: 当前 task 完整对话 + 上 N 个 task 的最终答复摘要(N 默认 3,可调)

**对 SqliteSaver 无影响** — 它照常存所有 thread 历史,recall 时由 facade 按 task 边界筛。新字段 `task_boundary` 是 facade 层的视图,不污染 checkpointer。

#### 2.5.1.3 4 层分片规则

| Level | 来源 | 提炼方式 | 压缩阈值 | 存在哪里 |
|-------|------|----------|----------|----------|
| **L0 raw** | LangGraph `SqliteSaver` | 不提炼(只存) | 30 天 | `data/short_term.db`(SQLite) |
| **L1 daily** | L0 已收尾 task | LLM 提炼 5W1H(where/who 可省,why 必须原话)+ 启发式评分 ≥ 0.2 | 7 天后升 L2 | Qdrant `level:daily` |
| **L2 weekly** | L1 daily 摘要 | LLM 归纳 daily 摘要 | 30 天后升 L3 | Qdrant `level:weekly` |
| **L3 monthly** | L2 weekly 摘要 | LLM 归纳 weekly 摘要 | 365 天后升 L4 候选 | Qdrant `level:monthly` |
| **L4 permanent** | L3 monthly 摘要 + 用户显式标记 + 5+ 次出现的偏好 | 无压缩,只允许人工 forget | 永不 | Qdrant `level:permanent` |

**重要性分级压缩**(2026-10-10 决策):

| importance 区间 | 行为 |
|-----------------|------|
| `[0.0, 0.2]` | 不进入 consolidation,直接走 TTL(30 天后软删) |
| `[0.2, 0.5]` | 进 L1 daily → 30 天后被 L2 weekly **直接吸收**(不写 L2 原条) |
| `[0.5, 0.8]` | 进 L1 + L2 + L3(逐级升格) |
| `[0.8, 1.0]` | 进 L1 + L2 + L3 + **L4 永久**(需用户 1 次确认 或 出现 5+ 次) |

**用户原话示例**:
- "开灯 / 打开电视"(琐事,无时间/数字)→ importance ~0.1 → 不进 consolidation,30 天 TTL 清掉 ✓
- "8 点烧洗澡水"(有具体时间)→ importance ~0.6 → 进 L1 → 7 天后升 L2
- "订下周去北京出差票"(有具体日期 + 任务)→ importance ~0.95 → 进 L1+L2+L3+L4 永久

#### 2.5.1.4 提炼格式(5W1H 适配版,不全照搬)

```
## 事件
- what: <具体事件>           ← 必填
- when: <时间 / 时间窗>       ← 必填
- where: <地点 / 上下文>     ← 可省
- who: <人物>               ← 可省
- how/what_changed: <结果 / 状态变化> ← 必填
- why: <用户原话引述,不要 LLM 编>  ← 仅在用户明确说了时填
```

**不变量**:
- `why` 字段**严格是用户原话引述**,**不允许 LLM 推断** — 否则就是幻觉
- 缺少可省字段时,整行不写(不要写"unknown")
- 必填字段缺失 → 不进 consolidation,降级为 raw 短期记忆

#### 2.5.1.5 遗忘(5 类)

| 类型 | 触发 | 行为 | API |
|------|------|------|-----|
| **TTL 被动** | importance < 0.2 且 created > 30 天 | 软删除 | `forget_by_ttl()` |
| **升格触发** | daily → weekly 升格完成 | 软删除所有参与升格的 L1 原始 fact | consolidation 内自动 |
| **主动** | 用户说"忘掉 X" | 软删除(可恢复) | `forget_by_user_request()` |
| **管家建议** | 管家问"这条记忆还要保留吗?" → 用户否 | 软删除 | `forget_by_key()` |
| **冲突覆盖** | 同一 topic 出现新事实且 importance > 旧 | 软删除旧 | `forget_by_conflict()`(新加) |

**软删除实现**:
- `payload["deleted"]=True` + `payload["deleted_at"]=<iso 时间>` + `payload["deleted_reason"]=<enum>`
- recall 永远 `must_not=[FieldCondition(key="deleted", match=MatchValue(value=True))]`
- 恢复 API = `restore(key)`(清 `deleted` flag + 写 audit log)
- **不物理删除** — 长期治理(7 天 → archived → 30 天 → purged)见 §2.5.1.16

**审计**: 每次 forget 记日志(key + reason + 时间 + source)。

#### 2.5.1.6 重要性评分策略

| 评分时机 | 方法 | 频率 |
|----------|------|------|
| **每条 remember** | 启发式(`HeuristicScorer`,无 LLM) | 每次写入 |
| **LLM 精评分** | `LLMScorer`,只对 consolidation **候选 promote 的 fact** 评 | consolidation 触发时,按批 |

**LLM 评分 prompt**(对候选 promote 的一批 fact):
```
对以下事实列表,给出 0-1 的重要性评分,参考标准:
- 0.0-0.2: 日常琐事(开灯/调节温度等无后续影响)
- 0.2-0.5: 有时间/数字的事实(8点烧洗澡水)
- 0.5-0.8: 任务/约定(下周出差/周末聚餐)
- 0.8-1.0: 持久偏好/关系/约定(用户长期习惯/家庭成员信息)

输入: [fact1, fact2, ...]
输出: JSON 列表,每项 {"key": "...", "importance": 0.X, "reason": "..."}
```

**降级**: LLM 评分失败 → 用启发式 0.5(不阻塞 consolidation)。

#### 2.5.1.7 facade API 扩展(P0 已有 API 不变,加 4 个新方法)

```python
# smartbutler/emotion/memory/facade.py 扩展
class MemoryFacade:
    # P0 已有(不变)
    async def remember(...) -> str: ...
    async def recall(...) -> list[dict]: ...
    async def format_for_prompt(...) -> str: ...

    # 6.3 新增
    async def consolidate(self, *, level: Literal["daily", "weekly", "monthly"] = "daily") -> int:
        """触发一次 consolidation。返回处理的 fact 数。"""

    async def forget(self, key: str, *, reason: ForgetReason) -> None:
        """主动遗忘。reason 决定走哪条路径(USER_REQUEST / TTL / CONFLICT / KEY)。"""

    async def restore(self, key: str) -> None:
        """恢复软删除的记忆(7 天内有效)。"""

    async def list_levels(self) -> dict[str, int]:
        """统计每层记忆数量(运维/调试用)。"""
```

**不破坏 P0**: `remember` / `recall` / `format_for_prompt` 签名零修改;新方法纯增量。

#### 2.5.1.8 触发器(替代原 3 触发器)

| 触发器 | 何时调 | 调什么 |
|--------|--------|--------|
| **定时 daily** | 每天凌晨 3 点(系统 cron / APScheduler) | `consolidate(level="daily")` |
| **定时 weekly** | 周日凌晨 3 点 | `consolidate(level="weekly")` |
| **定时 monthly** | 每月 1 日凌晨 4 点 | `consolidate(level="monthly")` |
| **手动** | 业务层 / 用户显式 | `consolidate(level=...)` |
| **L1 TTL** | L1 fact age > 30 天 且 importance < 0.2 | `forget_by_ttl()` |
| **L2 升格** | L1 daily 升 L2 weekly 时 | consolidation 内部自动调 `forget` 软删 L1 旧条 |
| **conflict** | 写入新 fact 时,scan 同 topic 旧条,importance 新 > 旧 | `forget_by_conflict()` |

**LangGraph 节点 hook**(可选): thinking 链尾可挂一个 `maybe_consolidate` 节点,调 `consolidate()` — **不强制**,主要由 cron 驱动。

#### 2.5.1.9 关键不变量(全文遵守)

1. **短期 SqliteSaver 全量存,recall 不直接查** — 只供 consolidation 喂入
2. **4 层分片用 tag 实现,存同一 collection** — 不分物理 collection
3. **升格 = 写新 + 软删旧,不物理删** — 7 天延迟可恢复
4. **why 字段必须用户原话引述** — LLM 不准编
5. **LLM 评分只对 consolidation 候选调** — 不调每条 remember
6. **improtance 严格分级** — `[0.0, 0.2)` 不进 consolidation / `[0.2, 0.5)` 升 1 层 / `[0.5, 0.8)` 升 2 层 / `[0.8, 1.0]` 升永久
7. **facade 高层 API 不破坏 P0** — `remember` / `recall` / `format_for_prompt` 签名零修改
8. **软删除永远不过 vacuum**(默认关闭) — 给用户反悔机会
9. **task 边界 + 滑窗** — 短期记忆的 recall 视图,不污染 SqliteSaver 存储
10. **MemoryHooks 接入** — `on_promote` / `on_forget` 钩子在 6.3 挂真实实现(为 Phase 6b / Learn 留口子)

#### 2.5.1.10 新增文件 + 行数预估

**6.3 主体(固化 + 5 类遗忘)**:

| 文件 | 行数 | 职责 |
|------|------|------|
| `emotion/memory/consolidation.py` | ~250 | 4 层分片 + LLM 评分 + 升格管道 |
| `emotion/memory/forgetting.py` | ~120 | 5 类遗忘 API + 软删除 + 恢复 |
| `emotion/memory/levels.py` | ~80 | 4 层分片的 tag 规则 + level 转换工具 |
| `emotion/memory/summarizer.py`(挪到 6.3) | ~150 | LLM 5W1H 提炼 prompt + 摘要生成 |
| `emotion/memory/task_boundary.py` | ~50 | task 边界检测规则 |
| `tests/unit/emotion/memory/test_*.py` | ~30 用例 | 见 2.5.1.11 测试矩阵 |

**6.3 扩展(长期 vacuum + 容量治理,见 §2.5.1.16)**:

| 文件 | 行数 | 职责 |
|------|------|------|
| `emotion/memory/vacuum.py` | ~200 | `vacuum_once` / `list_deleted` / `purge_now` / `vacuum_stats` + `VacuumReport` |
| `emotion/memory/archive_store.py` | ~80 | archived SQLite 表 schema + 增删查 |
| `scripts/memory_cli.py` | ~80 | CLI 入口(`memory_cli list-deleted` / `stats` / `vacuum` / `purge`) |
| `scripts/memory_scheduler.py`(可选) | ~50 | 自动调度(默认关闭) |
| `tests/unit/emotion/memory/test_vacuum.py` | ~10 用例 | 状态机迁移 / restore / dry_run / archive SQLite |
| `tests/integration/memory/test_vacuum_e2e.py` | 1 用例 | 端到端:删 → 7天模拟 → vacuum → 恢复 → 再删 |

**合计**:~1060 行代码 + ~40 单测 + 2 个 e2e(consolidation + vacuum)。**比原计划(~180 行)多 5.9 倍**,但包含 4 层分片 + LLM 评分 + 5 类遗忘 + 双时间窗 vacuum 的完整设计。

#### 2.5.1.11 测试矩阵(目标 ~30 用例)

| 类别 | 数量 | 覆盖 |
|------|------|------|
| 4 层分片 tag 规则 | 4 | level 转换 + payload 字段 + 跨层 recall 过滤 |
| 重要性分级压缩 | 4 | 4 档(0.1/0.4/0.6/0.9)各 1 case |
| LLM 评分降级 | 2 | 成功 + 失败降级到启发式 |
| 5W1H 提炼格式 | 3 | 完整 6 字段 + 部分省略 + why 必须原话 |
| task 边界检测 | 3 | 时间间隔 / 触发词 / 显式收尾 |
| 升格管道 | 4 | L1→L2 / L2→L3 / L3→L4 候选 / 不满足阈值拒绝 |
| 软删除 + 恢复 | 4 | 5 类遗忘各 1 + restore + audit log |
| TTL 遗忘 | 2 | importance < 0.2 + age > 30 天 / 不满足则保留 |
| conflict 覆盖 | 2 | 旧条软删 / 不同 topic 不冲突 |
| facade API 兼容性 | 2 | P0 API 签名零修改 / 新增 4 个 API |

#### 2.5.1.12 风险 + 降级矩阵

| 风险 | 触发 | 降级方案 |
|------|------|----------|
| **R1 LLM 提炼失败** | 评分 / 摘要生成超时 / 解析失败 | 用启发式评分 + 跳过当次 consolidation,记 ERROR 日志 |
| **R2 LLM 幻觉(why 编造)** | LLM 自行推断"用户为什么" | prompt 强约束 + 校验"why 必须是用户原话引述",否则丢弃 why 字段 |
| **R3 consolidation 成本过高** | 单次处理 > 10s | 限批大小(每次最多 50 条 fact),分批跑 |
| **R4 软删除膨胀** | 1 年后 deleted=true 占 80% | 提供手动 vacuum API(默认不调),物理删除 + 备份 |
| **R5 cron 漏跑** | 容器重启 / 系统时间跳变 | 启动时检查上次成功 consolidation 时间,补跑 |
| **R6 task 边界误判** | 用户连续追问(非新 task) | 滑窗 + "N 分钟间隔" 双校验,保守不切 |

#### 2.5.1.13 性能基线(可降级,非阻塞)

- `consolidate(level="daily")` 处理 50 条 fact: < 5s(LLM 评分 1 次 + LLM 摘要 1 次 + Qdrant 写入 50 次)
- `forget_by_ttl()` 扫描 1000 条 fact: < 1s(纯本地,无 LLM)
- `restore(key)`: < 100ms(单条 Qdrant 更新)
- e2e: 整轮 mock 对话 + consolidation: < 15s

#### 2.5.1.16 长期 vacuum 与容量治理(2026-10-10 敲定)

**问题背景**:软删除"只标不删"长期会让 Qdrant 容量膨胀,且 `must_not` 过滤要扫所有软删条目,recall 性能下降。

**容量账(管家家庭场景)**:
- 单条记忆 ≈ 6-8 KB(1024 维向量 4KB + payload 2-4KB)
- 1 年入库 ~17K 条 ≈ **130 MB**;5 年 ~85K 条
- 软删除膨胀:每年 ~8.5K 条 × 7KB ≈ **60 MB/年**
- **真正痛点不是磁盘(300MB 不大)**,而是 recall 性能:1 万条里 50% 软删 → `must_not` 过滤要扫 5K 条

#### 2.5.1.16.1 状态机:3 阶段 + 双时间窗

```
active ──(forget)──> deleted_recoverable ──(7 天)──> archived ──(30 天)──> purged(物理删)
 ↑                       ↑                              ↑                       ↑
recall 可见        recall 不可见,             SQLite 备份表              真的没了
                   restore() 可恢复         recall 不可见,
                                            restore() 仍可恢复
                                            (从 SQLite 还原)
```

| 状态 | 在哪 | recall 可见 | `restore()` | 持续 |
|------|------|-------------|-------------|------|
| **active** | Qdrant 主体 | ✅ | N/A | 永 |
| **deleted_recoverable** | Qdrant 主体(`payload.deleted=true`) | ❌ | ✅(原地恢复) | **7 天** |
| **archived** | SQLite 备份表(`data/deleted_archive.db`,Qdrant 已删) | ❌ | ✅(从 SQLite 还原 + re-embed) | **30 天** |
| **purged** | 物理删除 | ❌ | ❌ | 永 |

**容量稳态(双时间窗跑稳后)**: active 17K + deleted_recoverable 7 天滑窗 ~150 条 + archived 30 天滑窗 ~250 条 ≈ **稳态只多 400 条 ≈ 3MB**。

#### 2.5.1.16.2 API 设计 — 手动为主,自动默认关闭

**自动化(可选,默认关闭)**:
```python
# smartbutler/emotion/memory/forgetting.py 扩展
async def vacuum_once(
    self,
    *,
    recoverable_age_days: int = 7,    # deleted_recoverable → archived
    archive_age_days: int = 30,       # archived → purged
    dry_run: bool = False,            # True = 只统计,不真删
) -> VacuumReport:
    """跑一次清理。返回处理报告(扫了多少、各状态多少、准备做什么)。"""
```

**`VacuumReport`**:
```python
@dataclass
class VacuumReport:
    scanned_total: int           # 扫描的软删条目数
    to_archive: int              # 即将 deleted_recoverable → archived
    to_purge: int                # 即将 archived → purged
    archived_now: int            # 这次实际 archived
    purged_now: int              # 这次实际 purged
    dry_run: bool
    scanned_at: datetime
    error_keys: list[str]        # 出错的 key(可重试)
```

**手动接口 — 4 个**:

```python
# 1. 列出所有软删除条目(运维/UI 用)
async def list_deleted(
    self,
    *,
    level: Literal["any", "daily", "weekly", "monthly", "permanent"] = "any",
    limit: int = 100,
    older_than_days: int = 0,
) -> list[DeletedMemoryEntry]:
    """列出软删除的记忆,带原始内容(审计用)。"""

# 2. 恢复(升级 P0 的 restore —— 能从 archived 状态恢复)
async def restore(self, key: str) -> None:
    """从 deleted_recoverable 或 archived 状态恢复。
    
    archived 状态恢复需要:从 SQLite 读原文 → 调 Embedder re-embed → upsert 回 Qdrant
    """

# 3. 立即物理删(高级用户,跳过 7+30 天反悔期)
async def purge_now(
    self,
    key: str,
    *,
    confirm: Literal["yes", "no"] = "no",  # 双保险
) -> None:
    """立刻物理删,跳过 7+30 天等待。需要 confirm="yes" 显式确认。"""

# 4. 统计
async def vacuum_stats(self) -> dict[str, int]:
    """返回各状态条目数: {active, deleted_recoverable, archived, total_storage_bytes}。"""
```

#### 2.5.1.16.3 CLI 入口(必做,手动调用的主要方式)

`scripts/memory_cli.py`(新增,~80 行,纯 stdlib + 已有依赖):

```bash
# 列出所有软删超过 7 天的条目(带原文预览)
python -m smartbutler.scripts.memory_cli list-deleted --older-than 7
# 输出:
#   [deleted 8d ago] key=mem:user1:abc123  content="用户说8点烧洗澡水"
#   [deleted 12d ago] key=mem:user1:def456  content="用户开灯"
#   ...

# 统计各状态条目数
python -m smartbutler.scripts.memory_cli stats
# active=12500, deleted_recoverable=320, archived=45, total_storage_mb=85

# 跑一次 vacuum(dry-run,只看不动)
python -m smartbutler.scripts.memory_cli vacuum --dry-run
# scanned=365, to_archive=320, to_purge=45
# ⚠️  没有任何修改发生

# 真跑
python -m smartbutler.scripts.memory_cli vacuum
# archived 320, purged 45
# ✅ 清理完成

# 立即物理删某条(跳过反悔期,必须显式 confirm)
python -m smartbutler.scripts.memory_cli purge --key "mem:user1:abc" --confirm yes
# ✅ 已物理删除 mem:user1:abc
```

**为什么默认手动而非自动**(2026-10-10 决策):
- 家用 1 年才清 8.5K 条,等得起
- 自动化让用户**失去对记忆的控制感** — "管家悄悄把我说过的话删了"听着就吓人
- 留 CLI + Web UI(Phase 11+),让用户能**看见 + 决策**:"管家建议你清理 320 条,要清吗?"

**自动调度(可选,默认关闭)**:`scripts/memory_scheduler.py`(新增,~50 行)
- 启动时检查"上次成功 vacuum 时间",跳过本次(避免重启后短时间内重复跑)
- 开启方式:`SMARTBUTLER_MEMORY_VACUUM_AUTO_ENABLED=true` + 频率(默认每周日 4 点)

#### 2.5.1.16.4 不变量(vacuum 全文遵守)

1. **30 天反悔期** — 用户的"忘掉 X" 30 天内可恢复(deleted_recoverable 7 天 + archived 30 天)
2. **archived 必须能 restore** — 走 SQLite 备份,Qdrant 已删的重新 embed + upsert 回去
3. **purge_now 必须 confirm="yes"** — 防止脚本/UI 误操作
4. **vacuum 必须支持 dry_run** — 所有自动化操作都先 dry-run 看报告
5. **vacuum 不阻塞 recall** — 异步跑,分批 + checkpoint(每批 100 条),失败回滚
6. **archived 不进 Qdrant recall 路径** — 恢复时显式调 `restore()`,不自动复活
7. **vacuum_stats() 永远可调** — 不论 vacuum 跑没跑过,状态可查

#### 2.5.1.16.5 风险 + 降级

| 风险 | 触发 | 降级方案 |
|------|------|----------|
| **R1 vacuum 跑一半挂** | 进程被杀 / 断电 | 分批 + checkpoint,下次启动从断点继续 |
| **R2 archived restore 时 Embedder 失败** | Qwen API 挂 | 状态保持 archived,不强制恢复(用户可重试) |
| **R3 vacuum 占用 IO 影响 recall** | vacuum 和用户对话并发 | vacuum 限速(每秒最多 50 ops),后台跑 |
| **R4 archived SQLite 文件膨胀** | 长期不 vacuum | archived 表 vacuum(purge 时一并跑 SQLite VACUUM) |
| **R5 用户误调 purge_now** | 没看清 confirm | confirm="yes" 双保险 + 记 WARNING 日志 |
| **R6 vacuum_stats 跟实际不符** | 跨进程状态不同步 | vacuum_stats 每次都实时查(不走缓存) |

#### 2.5.1.16.6 新增文件 + 行数预估

| 文件 | 行数 | 职责 |
|------|------|------|
| `emotion/memory/vacuum.py` | ~200 | vacuum_once / list_deleted / purge_now / vacuum_stats + VacuumReport dataclass |
| `emotion/memory/archive_store.py` | ~80 | archived SQLite 表 schema + 增删查 |
| `scripts/memory_cli.py` | ~80 | CLI 入口(typer 或纯 argparse) |
| `scripts/memory_scheduler.py`(可选) | ~50 | 自动调度(默认关闭) |
| `tests/unit/emotion/memory/test_vacuum.py` | ~10 用例 | 状态机迁移 / restore / dry_run / archive SQLite |
| `tests/integration/memory/test_vacuum_e2e.py` | 1 用例 | 端到端:删 → 7天模拟 → vacuum → 恢复 → 再删 |

**合计**:~410 行代码 + ~11 单测 / 1 e2e,约 0.5-1 周。

#### 2.5.1.16.7 跟 §2.5.1.5 遗忘的关系

§2.5.1.5 定义的"5 类遗忘"产生**软删除条目**(→ deleted_recoverable 状态)。
本节定义的"vacuum"是**软删除条目进一步治理**(→ archived → purged)。

**数据流**:
```
5 类遗忘触发 ──(标记 deleted=true)──> deleted_recoverable(7 天)
                                              ↓ vacuum_once
                                          archived(30 天,SQLite 备份)
                                              ↓ vacuum_once
                                          purged(物理删)
```

**不冲突**: §2.5.1.5 的 forget() 保持只标不删(7 天反悔);本节 vacuum_once 负责 7 天后清出 Qdrant + 30 天后物理删。

#### 2.5.1.14 Phase 6.4 调整(原 2.5.2 重新规划)

**因 6.3 已实现 4 层分片**,6.4 的工作量大幅减少:
- `summarizer.py` 大部分代码已在 6.3 实现(5W1H 提炼 + LLM 摘要)
- 6.4 只补 **`episodic.py`**(对话轨迹,SQlite JSON,~150 行)+ **`hierarchical_rollup()`**(跨层手动 roll_up API,可选)
- **预估**:~150 行 + ~10 单测,~0.5 周(原计划 1 周)

#### 2.5.1.15 启动前置(必须满足才动 6.3)

- [x] Phase 6.2 P0 完工 ✅
- [x] `MemoryFacade` 高层 API 已稳定(P0 已交付)
- [x] `LongTermStore` schema 稳定(支持 tag 过滤 + range query)
- [ ] LLM 评分 + 摘要 prompt 已固化(本阶段实现,先 mock 测,再换真 LLM)
- [ ] 用户真实使用 ≥ 2 周产生"记太多/记错"反馈(或团队判断需要提前)

**启动命令**: 写一个 `scripts/run_consolidation.py` 跑一次 daily 端到端,确认链路 OK 后再开 PR。

### 2.5.2 Phase 6.4 — 层次化总结(DEFERRED)

**触发条件**:用户开始问"管家总结一下我最近 3 个月偏好"或主动建议需要 history 摘要时。

**设计要点**(占位):
- **3 层 hierarchy**:`DAILY`(24h)/ `WEEKLY`(7d)/ `MONTHLY`(30d)
- **触发方式**:定时任务(每天凌晨 3 点跑 daily → weekly;周日凌晨跑 weekly → monthly)+ 暴露 `roll_up()` API
- **持久化**:用 LongTermStore + tag 区分 level(避免新建存储后端)
- **重要不变量**:
  - summary **不重写已存的** —— 只新增
  - summary **不删原始 fact** —— 只把 keys 记到 `source_keys`
  - **episodic 永远不动 LongTermStore** —— 独立表,独立检索路径
- **importance 衰减 + 压缩**:daily 7 天后压缩成 weekly,weekly 30 天后压缩成 monthly,monthly 永久

**新增文件**:
- `emotion/memory/summarizer.py`(~150 行,3 层 hierarchy)
- `emotion/memory/episodic.py`(~150 行,SQLite JSON 对话轨迹)

**预估**:~300 行 + ~15 单测,~1 周

### 2.5.3 Phase 6.5 — habit + cross_thread(DEFERRED)

**触发条件**:Phase 6b ProactiveLoop 真实化时,需要"用户习惯"作为主动建议的输入。

**设计要点**(占位):
- **habit 抽取**:扫描 30 天 fact + episodic,LLM 抽"周期性行为 + 时间窗"
- **触发阈值**:30 天 + ≥ 5 次同类行为(频率 + 持续时间双维度)
- **跨 thread 状态**:Phase 6b 的 ProactiveLoop 需要"用户最近一次主动建议"等跨 session 状态
- **MemoryHooks 接入**:6.5 时 `on_habit_detected` / `on_promote` 钩子挂真实实现

**新增文件**:
- `emotion/memory/habit.py`(~100 行)
- `emotion/memory/cross_thread.py`(~50 行)

**预估**:~150 行 + ~10 单测,~1 周

---

## 3. langmem 集成方案(Phase 6.2 关键决策)

> **已决策**:langmem 通过 `capabilities/langmem/` 抽象层接入,业务层不直接依赖 langmem API。详见附录 B 问题 2。

### 3.1 为什么装 langmem

| 优势 | 细节 |
|------|------|
| **官方记忆抽象** | LangGraph 团队维护,API 稳定后不会大幅改动 |
| **事实抽取模板** | 预置 prompt 模板,减少自研工作量(~50 行省) |
| **TTL/重写策略** | 内置 forgetting 策略,直接复用 |
| **BaseStore 接口** | Qdrant 适配器未来可能官方支持(issue 跟踪) |

### 3.2 集成边界

`memory/` 业务层只依赖 `capabilities/langmem/base.py` 的 `BaseLangmemAdapter`:

```python
# smartbutler/emotion/memory/_langmem_bridge.py(业务层薄包装)
from smartbutler.capabilities.langmem import build_default_adapter

adapter = build_default_adapter()  # 自动选 _v0_0_30 或 _fallback

async def extract_facts(self, conversation: list[dict]) -> list[dict]:
    return await adapter.extract_facts(conversation)
```

### 3.3 装不上时降级(README R1)

`pyproject.toml` 加 `langmem>=0.0.30,<0.1` 到 dependencies。

`build_default_adapter()` 内部 try-import langmem;若 `pip install` 失败:

- `capabilities/langmem/_fallback.py` 的正则抽事实接管("我叫"/"我住在"/"我喜欢"等触发词)
- 记 WARNING 日志,不阻塞启动
- 业务层 `memory/` 无感知

---

## 4. Embedder 决策(已定)

**已决策**:`qwen3.7-text-embedding-flash` (MTEB 68.36,1024 维)。详见附录 B 问题 1。

---

## 5. 测试 + 验收标准

### 5.1 P0 阶段行数 / 测试用例目标(本阶段)

| 阶段 | 自研行数 | 单测用例 | 关键验收 |
|------|----------|----------|----------|
| **6.2 P0**(本次) | ~630 行 + ~340 测试 | ~28 | thinking 接入 + facade 全链路 + Embedder/langmem 抽象层 + 4 降级路径全测 |
| **6.3 P1**(后续) | ~180 | ~25 | 3 触发器各 2 case + forget 4 API + 软删除/恢复 |
| **6.4 P1**(后续) | ~300 | ~15 | daily/weekly/monthly 总结 + episodic 跨用户隔离 |
| **6.5 P1**(后续) | ~150 | ~10 | habit 抽取 + cross-thread + 钩子接入 |
| **P1 合计** | **~630** | **~50** | 全部 DEFERRED,启动前再开 ADR |

### 5.2 P0 跨阶段验收(本阶段完成后)

- [ ] `MemoryFacade.format_for_prompt` 一行调用 = 召回 + 格式化 + 截断
- [ ] thinking `decide_node` 注入 `memory_block` 到 system prompt
- [ ] QwenEmbedder 调通 `qwen3.7-text-embedding-flash` 返回 1024 维向量
- [ ] langmem 装/不装两种路径单测都过
- [ ] Qdrant 嵌入式挂掉时整套降级到 SQLiteStorage,管家 ainvoke 仍可工作
- [ ] 集成测试:模拟 1 轮对话,验证 LLM 收到的 system_prompt 含 `## 相关历史记忆` 段
- [ ] `MemoryHooks` 注册/触发/异常隔离 3 测过(为 Phase 6b/11 留 hook)
- [ ] multi-user 隔离:source A 写入不会在 source B 召回中出现(用 tag 过滤)

### 5.3 性能基线(P0 阶段,可降级,非阻塞)

- `format_for_prompt`: < 300ms(1000 条记忆下,Top-5,含 1 次 embed + 1 次 Qdrant 检索)
- 单条 `remember`: < 100ms(本地启发式评分 + 1 次 embed + 1 次 Qdrant upsert)
- 集成测试端到端: < 1s(整轮 mock 对话含召回)

---

## 6. 风险 + 降级矩阵(P0 阶段)

| 风险 | 触发 | 降级方案 | P0 阶段影响 |
|------|------|----------|-------------|
| **R1 langmem 装不上** | pip install 失败 | `_fallback.RegexFactExtractor` 接抽事实 | facade 降级,但 thinking 接入照常工作 |
| **R2 Qdrant 嵌入式 fd 泄漏** | Windows 高频写 | 切 server 模式 + docker | `retrieval.recall` 改走 `SQLiteStorage` LIKE 检索 |
| **R3 QwenEmbedder API 限流** | Qwen 429 / 401 / 超时 | `StubEmbedder`(hash → 1024 维)接管 | recall 准确度下降,但不崩 |
| **R4 StubEmbedder 召回不准** | 降级路径下,hash 相似 ≠ 语义相似 | P0 接受精度降级;P1 阶段考虑加本地 sentence-transformers 兜底 | 用户感受到"管家记不准"但不崩 |
| **R5 Hook callback 抛异常** | 用户注册了有 bug 的 callback | `try/except` 隔离 + warn 日志 | 主流程不受影响 |
| **R6 多用户 source 标签冲突** | 同名 source("") | P0 强制 `source:""` 退化到无 source tag(全可见);P1 加 `user_id` payload 隔离 | 单用户场景无影响 |

**P0 阶段不涉及的 R(降级后到 P1)**:
- 固化过度/欠固化(R3 旧)—— 6.3 才有 consolidation,本阶段不触发
- 长期记忆污染(R6 旧)—— 6.3 才有 promote,本阶段 remember 全是显式
- 习惯抽取风险—— 6.5 才有 habit,本阶段不触发

---

## 7. 后续阶段(只标记,本方案不实现)

- **Phase 6.6 Personality**:`emotion/personality/traits.py` + `state.py` + `injector.py`
- **Phase 6.7 Learn**:`emotion/learn/feedback_log.py` + `signal.py` + `evolution_hook.py`
- **Phase 6b ProactiveLoop 真实化**:`LLMProactiveReasoning` 接 Memory + Persona + Skill 注入
- **Phase 7**:`EventNormalizer` 真实实现 + Redis Streams EventBus + HomeAssistant adapter
- **Phase 8**:`HomeAgent` 接 HomeAssistant / `ScheduleAgent` / `SearchAgent`

---

## 附录 A:文件清单(本阶段)

```
smartbutler/emotion/memory/
├── __init__.py                    (更新,暴露新 API)
├── short_term.py                  (6.1 不动)
├── long_term.py                   (6.1 + 6.2 补 range query 暴露)
├── _langmem_bridge.py             (新,6.2,可选依赖)
├── embedder.py                    (新,6.2,~50 行)
├── importance.py                  (新,6.2,~30 行)
├── retrieval.py                   (新,6.2,~30 行)
├── consolidation.py               (新,6.3,~80 行)
├── forgetting.py                  (新,6.3,~70 行)
├── summarizer.py                  (新,6.4,~120 行)
├── episodic.py                    (新,6.4,~130 行)
├── habit.py                       (新,6.5,~80 行)
├── cross_thread.py                (新,6.5,~30 行)
└── hooks.py                       (新,6.5,~40 行)

smartbutler/storage/qdrant.py      (补丁,6.2,~10 行加 range query)
smartbutler/config/memory.py       (新,6.2,~40 行配置)

tests/unit/emotion/memory/         (新)
├── test_embedder.py               (6.2,~5 用例)
├── test_importance.py             (6.2,~5 用例)
├── test_retrieval.py              (6.2,~8 用例)
├── test_qdrant_range.py           (6.2,~3 用例)
├── test_langmem_bridge.py         (6.2,~4 用例)
├── test_consolidation.py          (6.3,~10 用例)
├── test_forgetting.py             (6.3,~10 用例)
├── test_summarizer.py             (6.4,~6 用例)
├── test_episodic.py               (6.4,~6 用例)
├── test_habit.py                  (6.5,~3 用例)
├── test_cross_thread.py           (6.5,~4 用例)
└── test_hooks.py                  (6.5,~3 用例)

tests/integration/memory/          (新,6.4-6.5)
└── test_e2e_memory_flow.py        (1 个端到端)
```

**总计**:12 个新 .py + 1 个补丁 + 12 个新测试文件 + 1 个 e2e,~650 行自研 + ~70 个单测。

---

## 附录 B:Open Questions

### 问题 1:Embedder 默认实现

**已决策** = `qwen3.7-text-embedding-flash`(MTEB 68.36,1024 维)。

架构:

```
smartbutler/capabilities/embedding/
├── __init__.py           # 公共导出:Embedder Protocol + build_default_embedder()
├── base.py              # Embedder Protocol (embed / embed_batch)
├── types.py             # EmbedResult 等数据模型
├── config.py            # EmbeddingSettings (key / base_url / model / dimension 等,.env 前缀 SMARTBUTLER_EMBEDDING_)
└── qwen.py              # QwenEmbedder 实现(调用 qwen3.7-text-embedding-flash)
```

`memory/` 业务层只 import `capabilities.embedding.base.Embedder`,不感知具体实现。

`.env` 配置项:

```bash
SMARTBUTLER_EMBEDDING_API_KEY=sk-xxx           # Qwen API Key
SMARTBUTLER_EMBEDDING_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/text-embedding
SMARTBUTLER_EMBEDDING_MODEL=qwen3.7-text-embedding-flash
SMARTBUTLER_EMBEDDING_DIMENSION=1024           # 必须与 qdrant_vector_size 一致
SMARTBUTLER_EMBEDDING_PROVIDER=qwen            # 当前固定 qwen;后续可扩展 local/stub
```

降级路径:若 Qwen 服务不可用,`capabilities/embedding/` 内部降级为 `StubEmbedder`(hash → 1024 维固定向量,无 API 调用);`memory/` 业务层无感知。

### 问题 2:langmem 版本与抽象层

**版本策略**:`pyproject.toml` 锁 `langmem>=0.0.30,<0.1`(beta 期锁定大版本)。

**抽象层设计**:
```
smartbutler/capabilities/langmem/
├── __init__.py           # 公共导出:BaseLangmemAdapter + build_default_adapter()
├── base.py               # BaseLangmemAdapter ABC + LangmemError 异常体系
├── types.py              # Fact / ExtractionResult / TTLPolicy Pydantic 模型
├── _v0_0_30.py          # Langmem0030Adapter (具体实现,软依赖 try-import)
└── _fallback.py          # RegexFactExtractor (langmem 装不上时正则抽事实)
```

架构要点:
1. `BaseLangmemAdapter` 是业务层唯一可见的抽象,**API 签名稳定**,langmem 升级只动 `_v0_0_30.py` → 以后 `_v0_1_x.py`
2. `_fallback.py` 是纯基础能力(正则抽"我叫"/"我住在"/"我喜欢"等),与 `memory/` 无关,放 `capabilities/langmem/` 内部
3. `memory/` 业务层只 import `capabilities.langmem.base.BaseLangmemAdapter`,不感知版本
4. 降级路径:`LangMemBridge` 优先用 `_v0_0_30.py`;`langmem` 装不上时自动切 `_fallback.py`;业务层无感知

> ⚠️ `_fallback.py` 为纯基础能力(正则抽事实),无 `memory/` 业务依赖,放在 `capabilities/langmem/` 而非 `emotion/memory/`。

> 确认后即可启动 6.2 实现,预计 1 周内完成首个 PR(Embedder + ImportanceScorer + MemoryRetriever + Qdrant range 补丁 + ~25 单测)。
