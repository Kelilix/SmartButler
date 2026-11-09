# Phase 9 技术方案:记忆图(Graph Memory)

> **状态**:草稿,待用户确认后实施
> **日期**:2026-11-09
> **范围**:在现有 fact 长期记忆之上,新增**人物关系图** —— 节点(人)/ 边(人与人之间的关系)/ 时态消解 / 图召回
> **不包含**:非人物实体(公司/小区/物品/事件 —— 后续再议)、社区层(日报/周报/月报已等价)、知识图谱问答的产品化
> **前置依赖**:Phase 6.2-6.5(记忆完整化,已交付)+ Step 1/2(时间抽取 `temporal.py`,已交付)+ Phase 7.6(声纹采集,已交付)
> **关联文档**:`docs/PHASE_9_GRAPH_MEMORY_ROADMAP.md`(开发路线图)/ `docs/PHASE_6.2-6.5_TECHNICAL_DESIGN.md`(记忆整体架构)/ `docs/PHASE_7_6_VOICE_ENROLLMENT_DESIGN.md`(声纹采集)
> **业界参考**:Graphiti / Zep([arXiv:2501.13956](https://arxiv.org/html/2501.13956))、Mem0 Graph Memory、HippoRAG
> **关联 ADR**:无新增 ADR(本文档是设计文档;若矛盾消解引入状态机再补)

> **🆕 五项关键决策(2026-11-09 用户拍板)**:
> - **① 抽取输入 = A3**:task 的 fact(骨架:谁说了什么)+ 原始消息(细节),两者拼接,**不增加抽取轮次**
> - **② 批处理粒度 = 一个 task 一次 LLM**:节点+边合并成 **1 次 LLM 调用**;task 内的边一次性抽出后在**内存**做批内消解
> - **③ 不细分 task**:图边界的粒度就等于 task 边界;改进方向转向**提升 task 切分能力**(见 §11,独立议题)
> - **④ 时间字段复用 task/fact**:边不单独抽时间;用 fact 的 `valid_at`(Step 2 成果)+ task 的 `start_ts` / fact 的 `spoken_at`
> - **⑤ 只做人 —— "一切皆图和边"**:节点**只有 Person**;边**只有 Person↔Person**;非人关系的边**整条丢弃**(信息留在 fact 层)。**没有独立的"人物表"** —— 人就是图里的节点,人物关系就是图里的边
>
> **🆕 节点范围收敛(2026-11-09)**:早期草案中的 `Organization` / `Location` / `Project` **全部移除**。
> - `Project` —— task 是工作单元不是世界实体,不应进图;task 只做边的溯源属性
> - `Organization` / `Location` —— 本期不做,先把人际关系做透(用户决策:"先专注把人之间的关系做好再考虑拓展")
> - 因此**不需要通用实体消解**(三级漏斗 / 向量近邻 / LLM 判重)—— Person 有声纹锚点,见 §7

---

## 0. 当前状态盘点(2026-11-09)

### 0.1 已有能力(全部可复用,无需重做)

| 能力 | 位置 | 状态 | 在图方案中的角色 |
|------|------|------|------------------|
| task 切分(LLM judge) | `emotion/memory/task_segmenter.py` | ✅ | **Episode 层等价物** —— 图的批处理边界 |
| task 事件钩子 | `emotion/memory/hooks.py` `ON_TASK_COMPLETE` | ✅ | 图抽取的触发点 |
| fact 抽取(按说话人分桶) | `emotion/memory/auto_extract.py` | ✅ | 图抽取的输入源之一 |
| 时间抽取 `spoken_at`/`valid_at` | `emotion/memory/temporal.py` | ✅ | **边的时间字段来源**(决策④) |
| fact 归属 `user:` tag | `emotion/memory/levels.py` | ✅ | 边的 `speaker` 字段 |
| fact 溯源 `task:` tag | `emotion/memory/levels.py` | ✅ | 边的 `task_ids` 溯源列表 |
| **声纹 user_id** | `events/routing/speaker_registry_*.py` | ✅ | **Person 节点的锚点** |
| **按名查声纹档案** | `registry.get_by_display_name()` | ✅ | 声纹绑定时匹配已有节点 |
| **声纹注册保 id** | `enroll_speaker(user_id=...)` | ✅ | 绑定时不产生新 id |
| 向量召回 | `emotion/memory/{retrieval,facade}.py` | ✅ | 图通道的**补充**,不替换 |
| tool 机制 | `capabilities/tools/decorator.py` | ✅ | 人物关系确认的接入点 |

### 0.2 真实缺口

| 缺口 | 现状 | 风险 |
|------|------|------|
| **人物之间没有结构化关系** | "张女士是我朋友"只是纯文本 | 无法回答"我妈是谁""谁是我同事""我朋友的老婆是谁" |
| **关系无归属** | 事实里的"谁"混在正文里 | 无法区分"我说我妈" vs "我妈自己说" |
| **无关系演化** | "张女士不再是我朋友了"只能作为新 fact 追加 | 召回时两条矛盾事实同时进 prompt,LLM 自行猜 |
| **关系无溯源粒度** | 知道 fact 来自哪个 task,但不知道**这条关系**来自哪句话 | 无法引用、无法审计 |
| **未知人物无处安放** | 提到但没注册声纹的人("我同事李雷")只能留在文本里 | 关系无法沉淀 |

### 0.3 与 Graphiti 的能力对齐

| 能力 | Graphiti | 我们 | 说明 |
|------|---------|------|------|
| Episode 层(原始输入) | ✅ | ✅ **等价** | 我们的 task 就是 episode |
| Entity 层 | ✅ | 🟡 **仅 Person** | 本期收敛范围(决策⑤) |
| Edge 层(时态三元组) | ✅ | 🟡 **仅 Person↔Person** | 非人关系丢弃 |
| Community 层 | ✅ | ✅ **等价** | 我们的日报/周报/月报就是社区摘要 |
| 双时态(4 时间戳) | ✅ | 🟡 **部分** | `valid_at`/`created_at` 已有,`invalid_at`/`expired_at` Step 3.5 建 |
| 实体消解 | ✅ 三级漏斗 | ✅ **不需要** | Person 有声纹锚点,见 §7 |
| 声纹 ↔ 实体绑定 | ❌ 无此概念 | ✅ **我们有** | 结构性优势,见 §8 |
| 图遍历召回 | ✅ BFS | ❌ **本次要建** | SQLite 递归 CTE 足够 |

**结论**:我们不缺上层(社区摘要)和底层(task/时间/归属),**缺的正是中间那层人际关系图**。且因为有声纹这一层基础设施,我们可以跳过 Graphiti 最贵的实体消解环节。

---

## 1. 方案总览

### 1.1 目标数据流

```
┌────────────────────────────────────────────────────────────────────────┐
│ TaskSegmenter(LLM judge 切 task)                                       │
└────────────────────────────┬───────────────────────────────────────────┘
                             ↓ task 进入 completed / chitchat_done
┌────────────────────────────────────────────────────────────────────────┐
│ TaskScheduler.on_task_closed → MemoryEventType.ON_TASK_COMPLETE        │
└──────────────┬──────────────────────────────────┬──────────────────────┘
               ↓                                  ↓
┌──────────────────────────────┐  ┌──────────────────────────────────────┐
│ AutoExtractHook(已存在)        │  │ GraphExtractHook(Step 3 新增)         │
│  ├ 按说话人分桶                 │  │  ① 拼输入:fact + 原始消息 (决策①)      │
│  ├ TemporalExtractor → 时间     │  │  ② 1 次 LLM:节点 + 边 + fact_ref      │
│  └ 写 fact 到 Qdrant            │  │  ③ 节点解析(声纹锚点 / 名字锚点)       │
└──────────────────────────────┘  │  ④ 批内消解 + 矛盾检测                  │
                                  │  ⑤ 单事务批写 graph.db                 │
                                  └────────────────────┬─────────────────────┘
                                                       ↓
┌────────────────────────────────────────────────────────────────────────┐
│ GraphStore(SQLite: graph_entities / graph_edges / graph_task_log)       │
│  节点: Person,带 user_id(可空)+ aliases                                │
│  边: Person↔Person,带 valid_at / invalid_at / speaker / task_ids      │
└────────────────────────────┬───────────────────────────────────────────┘
                                 ↓
┌────────────────────────────────────────────────────────────────────────┐
│ MemoryFacade 召回:向量通道(不变)+ 图通道(补充) → 合并去重 → prompt      │
└────────────────────────────────────────────────────────────────────────┘

        ┌──────────────────────────────────────────────────────┐
        │ 另一条写入路径(对话中):                              │
        │  LLM 调 resolve_person tool → 建/复用节点 + 建边      │
        │  声纹采集完成 → 按名匹配 → 绑定 user_id 到已有节点     │
        └──────────────────────────────────────────────────────┘
```

### 1.2 三层设计原则

```
Community 层  ← 我们的日报/周报/月报(已存在,不重复建设)
     ↑
Entity 层    ← 本次建设:只有 Person 节点 + Person↔Person 边
     ↑
Episode 层  ← 我们的 task + task_messages(已存在)
```

---

## 2. 节点设计:只有 Person

### 2.1 节点判定标准

一个东西能当节点,当且仅当:

1. **能被独立指代** —— 有稳定身份,能用"他/她"指代而不必重复描述
2. **能独立承载关系** —— "张女士"能挂朋友/家人/同事等关系
3. **不是关系本身,也不是时间** —— "开会"是边;日期是边的属性

### 2.2 节点类型:唯一一种

| 类型 | 例子 | 锚点 | 说明 |
|------|------|------|------|
| `Person` | 我、妈妈、张女士 | **声纹 `user_id`** 或 **名字哈希** | 本期唯一类型(决策⑤) |

**不建的类型**(本期明确排除):

| 类型 | 排除原因 |
|------|---------|
| `Organization` | 用户决策:先专注人际关系,后续再拓展 |
| `Location` | 同上 |
| `Project` | task 是工作单元不是世界实体;task 只做边的溯源属性(`task_ids`) |
| `Object` / `Concept` / `Event` | 本期不涉及 |

### 2.3 节点 id 设计:锚点可升级,id 永不变

这是整个方案能成立的技术基石。

```
李xx 第一次出现(无声纹)
    node_id = "person:name:<hash(normalize("李xx"))>"   ← 名字锚点
    user_id = NULL
    voice_bound = 0
    edges: [我] --IS_MOTHER_OF--> [李xx]

后来采集了李xx的声纹(user_id=spk_b2c8)
    node_id 不变！                                        ← ⭐ 只补字段
    user_id = "spk_b2c8"
    voice_bound = 1
    edges: 一条都不用改 ✅
```

**为什么必须这样**:边永远指向 `node_id`。如果绑定声纹时改 `node_id`(比如从 `person:name:李xx` 换成 `person:spk_b2c8`),所有已有边都要重连 —— 一次绑定变成一次全图重写。

**因此:绑定声纹 = 纯粹的字段更新,零边改动。**

### 2.4 名字别名链

同一个人会被多种称呼提及:`李xx` / `李女士` / `小李` / `我妈`。节点存所有见过的称呼:

```json
{
  "node_id": "person:name:李xx",
  "name": "李xx",
  "aliases": ["李xx", "李女士", "我妈"],
  "user_id": "spk_b2c8",
  "voice_bound": 1
}
```

**匹配时查 `name` + `aliases`** —— 任意一个归一化后命中即为同一节点。

### 2.5 中文特有的坑:"我妈"是节点,不是关系标签

"我妈"既指人又表达血缘。正确拆法:

```
[我] --IS_MOTHER_OF--> [妈妈]      ← 关系在边上
[妈妈] --WORKS_WITH--> [张三]      ← 妈妈的关系也可查
```

而不是把"我妈"当抽象标签贴在"我"身上 —— 那样"她同事是谁"永远答不出来。

> **落地要求**:LLM prompt 必须显式声明「亲属称谓(妈妈/爸爸/奶奶/外公)也是 `Person` 实体,亲属关系表达为**边**」。

### 2.6 为什么"只把人当节点"在本期是可接受的

用户的原始诉求是"除了人还有公司、小区等实体也很核心"。**这个判断是对的**,但本期主动收敛:

- 人际关系是**记忆图最核心、也最难**的部分 —— 涉及归属、置信度、关系演化(吵架/离婚/换工作)
- 人与人之间的关系是**多跳价值最高**的:"我妈" → "妈妈的同事" → "同事的公司"
- 等人关系做透、跑出真实数据后,再决定加什么实体类型 —— **数据驱动的 ontology**,而不是提前猜

> ⚠️ **本期已知局限**:涉及非人的事实**不进图**。例如"我妈明天去上海"：
> - ✅ 保留 `[我] --IS_MOTHER_OF--> [妈妈]`(两端都是 Person)
> - ❌ 丢弃 `[妈妈] --TRAVELED_TO--> [上海]`(上海非 Person)
>
> **信息不丢失** —— 完整内容仍在 fact 层(Qdrant),仍可被向量召回。只是不参与图遍历。

---

## 3. 边设计

### 3.1 边 = 归一化谓词 + 自然语言事实句(两层)

```python
@dataclass(frozen=True)
class GraphEdgeCandidate:
    source: str          # 人名(待解析成 node_id)
    relation_type: str   # IS_MOTHER_OF / IS_FRIEND_OF / ...
    target: str          # 人名
    fact: str            # 自然语言完整句(embedding 召回用)
    fact_ref: str = ""   # 归属的 fact key(决策④:取该 fact 的 valid_at)
```

| 层 | 例子 | 用途 |
|----|------|------|
| `relation_type` | `IS_MOTHER_OF` | 结构化 → 图算法、过滤、聚合 |
| `fact` | "张女士是我妈妈" | 自然语言 → embedding 召回、喂给 LLM |

**为什么不能只有谓词**:`IS_FRIEND_OF` 无法区分"我最好的朋友张女士"和"普通朋友张女士"。谓词给结构,事实句给细节。

### 3.2 代码 vs LLM 的分工(硬约束)

> **LLM 提议,代码裁决并落库。**

代码必须硬约束三件事(LLM 不可绕过):

1. **端点必须存在** —— 边的 `source`/`target` 必须命中本次已抽取的人物列表,否则丢弃(防 LLM 编造端点)
2. **谓词必须过白名单** —— `relation_type` 不在词表 → 降级为 `RELATES_TO`,保留 `fact` 句
3. **端点必须是 Person** —— 本期唯一类型,任何非 Person 端点 → **整条边丢弃**(决策⑤)

### 3.3 谓词词表(仅 Person↔Person)

```python
EDGE_TYPE_MAP = {
    ("Person", "Person"): [
        # 家庭
        "IS_FAMILY_OF",      # 泛家庭关系
        "IS_MOTHER_OF",      # 母亲
        "IS_FATHER_OF",      # 父亲
        "IS_PARENT_OF",      # 父母(泛)
        "IS_CHILD_OF",       # 子女(泛)
        "IS_SIBLING_OF",     # 兄弟姐妹
        "IS_SPOUSE_OF",      # 配偶
        "IS_PARTNER_OF",     # 伴侣(未婚/同居)
        # 社交
        "IS_FRIEND_OF",      # 朋友
        "KNOWS",             # 认识(泛)
        # 职场
        "WORKS_WITH",        # 同事/合作
        "REPORTS_TO",        # 上级下属
        "IS_COLLEAGUE_OF",   # 同事
        "MANAGES",           # 管理
    ],
}
```

**为什么用受控词表而非开放抽取**:
- 图算法和过滤需要稳定标签
- 中文谓词归一化困难("我妈"/"我母亲"/"母亲大人" 是同一个关系)—— 受控词表正好强制归一
- 代价:词表需人工维护。LLM 抽到词表外的 → 降级 `RELATES_TO` + 保留事实句

### 3.4 关系的方向性

```
[我] --IS_MOTHER_OF--> [妈妈]        ← 有向
[我] --IS_FRIEND_OF--> [张女士]      ← 有向但对称
```

**默认按说话人视角存有向边**。对称关系(`IS_FRIEND_OF` / `WORKS_WITH` / `IS_SIBLING_OF` / `IS_SPOUSE_OF`)在召回时**双向查询**,不必存两条边 —— 避免双写不一致。

### 3.5 边的语义细分:属性 vs 关系

Graphiti 的一条硬规则值得直接抄:

> **DO NOT create nodes for relationships or actions.**
> **Prefer DIRECT speaker-to-target edges over fragmenting a relationship through descriptive intermediaries.**

含义:**关系和属性不能当中间节点**,否则真正的关系被埋深,召回不到。

```
✅ [我] --IS_FRIEND_OF--> [张女士]
   事实句:"张女士是我最好的朋友,认识十年了"
   (认识十年 = 属性,写在事实句里)
```

### 3.6 说话人视角与置信度

**边的 source 永远是说话人**,不是被描述的人。这直接对应我们之前定的置信度原则:

```
用户说:"我妈明天去上海"
    → [用户] --IS_MOTHER_OF--> [妈妈]      ← source = 说话人(用户)

妈妈自己说:"我明天去上海"
    → [妈妈] --TRAVELED_TO--> [上海]       ← 丢弃(非 Person 端点)
```

即使用户转述别人的行为,图里记录的也是**"用户声称妈妈是..."**,而非"妈妈就是..."。归属在边的 `speaker` 字段里。

---

## 4. 时间模型(决策④:复用 task/fact)

### 4.1 双时态四时间戳

| 字段 | 语义 | 归属 | 来源 |
|------|------|------|------|
| `valid_at` | 关系在**现实世界**中成立的时间 | 世界时间 | ✅ 复用 fact 的 `valid_at`(Step 2) |
| `invalid_at` | 关系在**现实世界**中终止的时间 | 世界时间 | ⏳ Step 3.5 矛盾消解时写入 |
| `created_at` | 边**写入系统**的时间 | 系统时间 | ✅ task 的 `start_ts` / fact 的 `spoken_at` |
| `expired_at` | 边在**系统**中被替换的时间 | 系统时间 | ⏳ Step 3.5 矛盾消解时写入 |

### 4.2 valid_at 的四种形态(Step 2 已实现)

复用 `temporal.py` 的编码:

| 形态 | 编码 | 例 |
|------|------|-----|
| 精确点 | `2026-01-01` | "2026年1月1号是我领证了" |
| 区间 | `2026-11-09/2026-11-15` | "下周我想吃龙虾" |
| 周期 | `recurring:01-29` | "1月29号是我生日" |
| 永久 | `permanent` | "张女士是我朋友" |

### 4.3 已知局限与缓解

**局限**:Step 2 的 `valid_at` 是 **fact 级**的,一条 fact 可能含多个边、且各边时间不同。

**缓解(零额外 LLM 成本)**:LLM 抽取时为每条边标注 `fact_ref` —— 指向它归属的 fact。落库时取该 fact 的 `valid_at`。

### 4.4 矛盾消解的时间语义(Step 3.5)

对齐 Graphiti:

- 矛盾判定**比较时间,不看到达顺序**
- 新的 `valid_at` 更晚 → 新的赢
- 旧的**不删除**,打 `invalid_at` + `expired_at`
- 例:"张女士是我朋友"(permanent) → "张女士不再是我朋友了" → 旧边 `invalid_at` = 今天,新边写入且带 `invalidates` 标记

> 与 Step 2 已落地的 `invalidates:1` tag 呼应 —— Step 2 已在 fact 层标记"否定/修正"信号,Step 3.5 消费它做边级失效。

---

## 5. 批处理粒度(决策②:一个 task 一次)

### 5.1 为什么不能"每说一句建一次边"

| 问题 | 后果 |
|------|------|
| LLM 调用爆炸 | 每句 1 次抽取,成本与延迟不可接受 |
| 写库压力大 | 每条边一次事务 |
| **改口无法消解** | "她是我朋友…不对,我们掰了" —— 第 2 句到达时第 1 句的边可能还没落库,**永远看不到对方** |
| 边质量低 | 单句缺少上下文,抽不出可靠关系 |

**task 批处理天然解决第 3 点**:task 内的边一次性抽出,互相可见 → 内存内可直接判矛盾。

### 5.2 与 Graphiti 的对齐

Graphiti 的 `episode` 就是它的"task" —— 由上游切分,Graphiti 自己不切。我们有真正的 `TaskSegmenter`,**这是我们的优势**。

| Graphiti | 我们 |
|----------|------|
| Episode(原始输入单元) | **Task**(`TaskSegmenter` 切出) |
| `episode_body` | task 的 message 序列 |
| `reference_time` | task 的 `start_ts` / fact 的 `spoken_at` |
| `episodes` 溯源列表 | fact 的 `task:` tag ✅ 已有 |
| `CHUNK_SIZE = 10` | 按 task 自然分组 |

### 5.3 单次 LLM 调用的产出

```
1 次调用 → {
    "nodes": [ {name, ref_id?} ],                           # 人物候选
    "edges": [ {source, relation_type, target, fact, fact_ref} ]
}
```

**节点+边合并抽取**(对齐 Graphiti 的 `extract_nodes_and_edges` combined prompt),省一次调用。

### 5.4 批内消解(绕开 Graphiti 的已知缺陷)

DeepWiki 对 Graphiti bulk 模式的诚实评价:

> **Key constraint: bulk processing trades completeness of cross-episode temporal invalidation for throughput.** Edge invalidation and deduplication happen per episode within the batch, but episodes within the same batch **do not see each other's newly added edges** during resolution.

**我们的规避**:task 内的边在**落库前**先在内存做一轮两两消解与矛盾检测,再统一写库。

> 附注:Graphiti 至今有未修的 bug(#1872)—— bulk 路径下完全相同的 fact 会被写成两条边。说明这套机制本身不完美,我们的批内消解要自己写对。

---

## 6. 存储设计

### 6.1 选型:SQLite,不引入图数据库

**决策:独立文件 `data/graph.db`,不引入 Neo4j / FalkorDB / Amazon Neptune。**

理由:
- 个人管家场景,人物节点几百、边几千 —— SQLite 绰绰有余
- 已有 `task_index.db` 先例,架构一致
- 1-2 跳用递归 CTE 就够,不需要复杂图算法
- **零新部署依赖**,不增加运维面
- 写负载(图高频批写)与 task 索引(低频更新)不同 → **独立文件**(用户已确认)
- 向量仍在 Qdrant(已有),图结构在 SQLite,**按 id 关联**

### 6.2 表结构

```sql
-- data/graph.db(独立文件)
CREATE TABLE IF NOT EXISTS graph_entities (
    node_id      TEXT PRIMARY KEY,   -- "person:<user_id>" 或 "person:name:<hash>"
    name         TEXT NOT NULL,      -- 主显示名
    aliases      TEXT NOT NULL DEFAULT '[]',  -- JSON 数组:见过的所有称呼
    type         TEXT NOT NULL DEFAULT 'Person',
    user_id      TEXT,               -- 声纹锚点,未绑定时为 NULL
    voice_bound  INTEGER NOT NULL DEFAULT 0,
    summary      TEXT NOT NULL DEFAULT '',  -- 活跃边的 fact 句聚合(用于向用户描述这个人)
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_entity_user  ON graph_entities(user_id);
CREATE INDEX IF NOT EXISTS idx_entity_name  ON graph_entities(name);
CREATE INDEX IF NOT EXISTS idx_entity_bound ON graph_entities(voice_bound);

CREATE TABLE IF NOT EXISTS graph_edges (
    edge_id       TEXT PRIMARY KEY,
    source_id     TEXT NOT NULL,
    target_id     TEXT NOT NULL,
    relation_type TEXT NOT NULL,
    fact          TEXT NOT NULL,
    speaker       TEXT NOT NULL DEFAULT '',   -- 说话的 user_id
    valid_at      TEXT,
    invalid_at    TEXT,
    created_at    TEXT NOT NULL,
    expired_at    TEXT,
    task_ids      TEXT NOT NULL DEFAULT '[]', -- 溯源(可多)
    embedding     BLOB
);
CREATE INDEX IF NOT EXISTS idx_edge_src  ON graph_edges(source_id);
CREATE INDEX IF NOT EXISTS idx_edge_tgt  ON graph_edges(target_id);
CREATE INDEX IF NOT EXISTS idx_edge_pair ON graph_edges(source_id, target_id);
CREATE INDEX IF NOT EXISTS idx_edge_live ON graph_edges(invalid_at);

-- 幂等:同一 task 不重复抽图
CREATE TABLE IF NOT EXISTS graph_task_log (
    task_id      TEXT PRIMARY KEY,
    extracted_at TEXT NOT NULL,
    node_count   INTEGER NOT NULL DEFAULT 0,
    edge_count   INTEGER NOT NULL DEFAULT 0,
    status       TEXT NOT NULL DEFAULT 'ok'
);
```

### 6.3 关键索引说明

| 索引 | 服务的操作 |
|------|-----------|
| `idx_edge_pair(source_id, target_id)` | **判重的核心** —— 检索同端点的已有边 |
| `idx_edge_live(invalid_at)` | 活跃边过滤(`invalid_at IS NULL`) |
| `idx_entity_user(user_id)` | 声纹绑定时按 `user_id` 找节点 |
| `idx_entity_name(name)` | 按名查找节点(绑定 / 解析时) |
| `graph_task_log` 主键 | 幂等保证 —— 同一 task 重复触发不重复抽 |

### 6.4 边 ID 设计(借鉴 Graphiti 的 uuid 判据)

```
edge_id = sha256(source_id | relation_type | target_id | normalize(fact))
```

好处:
- 同源同目标同事实 → **天然幂等**
- 判重命中 → `edge_id` 相等 → 不新增,只追加 `task_ids`

**Graphiti 的对应机制**(源码 `edge_operations.py`):

```python
# An edge is new if the resolved edge UUID matches the extracted edge UUID
if resolved_edge.uuid == extracted_edge.uuid:
    new_edges.append(resolved_edge)
```

Graphiti 不判断"是不是新边",而是让 uuid 相等**暴露**答案。我们用 `edge_id` 哈希达到同样效果。

### 6.5 节点合并(声纹绑定时的去重)

当声纹绑定发现多个节点匹配同一名字时,需要合并:

```sql
-- 1. 选主节点(边最多的)
-- 2. 把被合并节点的边改指向主节点
UPDATE graph_edges SET source_id = :main WHERE source_id = :dup;
UPDATE graph_edges SET target_id = :main WHERE target_id = :dup;
-- 3. 合并 aliases
UPDATE graph_entities SET aliases = :merged WHERE node_id = :main;
-- 4. 删除被合并节点
DELETE FROM graph_entities WHERE node_id = :dup;
```

**主节点选择规则**:
1. 优先选 `voice_bound=1` 的(有声纹锚点的更可靠)
2. 同等条件下选**边最多**的(信息量最大)
3. 再同则选 `created_at` 更早的(更早建立 = 更可能是原始节点)

**主节点的 `node_id` 保持不变** —— 边少的一方的 id 被弃用,但**主 id 稳定**。

---

## 7. 人物节点解析(替代通用实体消解)

### 7.1 为什么不需要三级漏斗

Graphiti 的实体消解需要三级漏斗(归一化精确 → 向量近邻 → LLM 判重),因为它面对的是**任意类型**的实体,只有名字可用。

我们的 Person 有两条解析路径:

```
候选人名
    ↓
路径 A:声纹档案命中(registry.get_by_display_name)
    ├─ 命中 → node_id = "person:<user_id>"        ← ⭐ 硬锚定,零消解
    └─ 未命中 ↓
路径 B:图内名字匹配(name / aliases)
    ├─ 命中唯一 → 复用该节点
    ├─ 命中多个 → 需合并(见 §6.5)
    └─ 未命中 ↓
路径 C:名字归一化哈希
    → node_id = "person:name:<hash(normalize(name))>"   ← 新建
```

**路径 A 是我们的结构性优势** —— Graphiti 最贵的一环在我们这里对 Person 完全不需要。

### 7.2 路径 A 的实现

现有声纹注册表已有按名查询:

```python
# events/routing/speaker_registry_*.py
async def get_by_display_name(self, display_name: str) -> SpeakerProfile | None
```

解析时先查它:
```python
profile = await registry.get_by_display_name(name)
if profile:
    return f"person:{profile.user_id}", profile.user_id
```

**注意**:这一步需要 `GraphExtractor` 拿到 `SpeakerRegistry` 引用 —— 通过 `wiring.py` 注入。

### 7.3 路径 C 的名字归一化

轻量、纯字符串、无 LLM:

```python
def normalize_person_name(name: str) -> str:
    s = name.strip()
    s = 全角转半角(s)
    s = re.sub(r"\s+", "", s)          # 去所有空格
    s = re.sub(r"[（(].*?[)）]", "", s)  # 去括号修饰:"李雷(同事)" → "李雷"
    s = s.lower()
    return s
```

> ⚠️ **保守处理**:过度激进会把不同人合并。"小李" 和 "李雷" 归一化后仍不同 —— 这是**已知局限**,靠 §8 的别名链和后续声纹绑定来收敛。

### 7.4 已知局限

| 局限 | 表现 | 缓解 |
|------|------|------|
| 同一人不同称呼 | "李雷" / "李女士" / "小李" → 两个节点 | 别名链(`aliases`);声纹绑定时合并 |
| 同名不同人 | 两个"张伟" → 合并 | ✅ **主动询问消歧**(§8.4):用对话确认,而不是靠算法猜 |
| 纯改名 | "李雷"改叫"李雷鸣" | ⏳ 需要主动询问确认 |

> "同名不同人"是**唯一有确定解法**的局限 —— 因为有真人可以确认,不需要语义消解。
> 其余两项是**语义层**问题:Graphiti 用向量 + LLM 解决,但我们判断:**声纹 + 别名链 + 主动询问**的组合更可靠、更便宜。
> 等真实数据出来再评估是否需要补向量近邻。

---

## 8. 声纹 ↔ 图节点绑定(核心机制)

### 8.1 绑定总流程

```
声纹采集完成 → display_name="李xx", user_id="spk_b2c8"
    ↓
查图:有没有节点的 name / aliases 归一化后命中"李xx"?
    ├─ 未命中 → 直接建节点 "person:spk_b2c8"(声纹锚定)✅ 无需询问
    └─ 命中   → ⚠️ 可能同名 → **立即询问用户**(§8.4),两个场景统一
```

**关键设计:消歧用对话确认,不靠算法猜。**

用户是这个世界上最权威的"同一人判定器" —— 他知道"张伟"说的是不是同一个人。
把"同名不同人"这个**技术难题**转成**一次对话交互**,是本期消歧机制的核心思路。

### 8.2 命中的情况:为什么只找到 1 个也要确认

**即使只找到 1 个候选,也必须询问** —— 同名场景下"只找到 1 个"恰恰最危险:

```
图里已有:  张伟A(同事,在XX公司)
现在采集:  张伟B 的声纹,display_name="张伟"
查图结果:  命中 1 个(张伟A)
```

如果直接绑定 → 张伟B 的声音被认成张伟A,**这是最糟的失败**:声音认错人,后果比名字重复严重得多。

所以规则是:**只要命中 ≥1 个候选,就走确认流程**。

### 8.3 新建 vs 绑定的选择依据

| 情况 | 动作 | 理由 |
|------|------|------|
| 未命中任何候选 | 建 `person:spk_b2c8`(声纹锚点) | 声纹 id 更可靠 |
| 命中候选 + 用户确认"是" | 绑定到候选(`node_id` 不变) | 保留已有边 |
| 命中候选 + 用户确认"不是" | 建新节点 `person:name:张伟#2` | 区分同名两人 |

### 8.4 消歧机制:三态确认

#### 候选描述从哪来

向用户展示"是不是这个人"时,需要能描述候选人。**描述必须来自图里的真实关系**,不能编造:

```
节点 summary = 该节点所有**活跃边**的 fact 句(去重 + 取前 N 条)
```

例:
```
person:张伟A
  name: "张伟"
  aliases: ["小张"]
  user_id: NULL
  voice_bound: 0
  summary: "张伟是我的同事,我们在项目A一起做过事"   ← 来自边的 fact 句
```

**summary 的维护**:Step 3.4 落库时更新(节点的边变动 → 重算 summary)。

#### 三种回答与对应处理

| 用户回答 | 语义 | 处理 |
|---------|------|------|
| "是" / "对" / "就是他" | 确认同一人 | **绑定**到该候选节点(`node_id` 不变,边不动) |
| "不是" / "是另一个人" | 同名不同人 | **新建节点** `person:name:张伟#2` 区分 |
| "同名" / "有另一个也叫这名字" | 同名不同人 | **新建节点** `person:name:张伟#2` 区分 |

后两种是同一个动作 —— **新建节点**。区别只在用户是否明确知道有两个同名的。

#### 新节点的区分标识

```
person:name:张伟       ← 已存在
person:name:张伟#2     ← 新建(区分同名)
```

- **编号规则**:查已存在的 `person:name:张伟#N`,取最大值 +1
- **`name` 保持"张伟"**(可读),只有 `node_id` 带后缀
- `summary` 里记一句区分说明(如"与另一位张伟区分")

### 8.5 立即确认(统一策略)

**决策(用户拍板 2026-11-09):识别到同名问题就立即询问,不引入延迟机制。**

> 早期草案曾设计"延迟到下次对话再问"(pending 表 + 定时任务通知),现按用户决策**取消**,
> 改为最简逻辑:**立即询问 + 记录**。定时任务/主动询问的精细调度留待后续需要时再优化。

#### 统一状态机

```
触发点(声纹采集完成 / 对话中建立关系)
    ↓
按名查图 → 命中候选?
    ├─ 无 → 直接建节点 person:spk_b2c8 ✅ 结束
    └─ 有 → ⚠️ **立即询问用户**
              ↓
         "是不是这个人:张伟,我的同事,在XX公司工作?"
              ↓
         用户答"是"   → 绑定到候选节点(node_id 不变,边不动)
         用户答"不是" → 建新节点 person:name:张伟#2
```

**两个场景完全一致** —— 都是"查图 → 命中就问 → 三态处理"。**不区分场景,不引入额外存储。**

#### 立即询问的现实约束

声纹采集是**后台异步**的,采集完成时用户可能已经离开。因此:

| 约束 | 处理 |
|------|------|
| 用户不在旁边 | 询问**尽力而为** —— 通过 TTS/对话渠道发出;用户没回应就结束,不留悬挂状态 |
| 没有"待确认"记录 | 询问失败 → **放弃绑定**,该人的声纹照常注册(下次提到他时再走对话确认路径) |
| 复杂度 | **不引入 pending 表、不引入定时任务、不引入 prompt 注入** |

> **取舍说明**:立即询问在"用户不在"时会漏掉一次绑定机会。
> 但相比引入 pending 状态机 + 定时任务 + 过期策略的复杂度,这个代价可接受 ——
> 漏掉的绑定可以在**下次对话中建立关系时**重新走一遍 `resolve_person` 确认流程。

#### 两种场景的对比

| | 声纹采集后 | 对话中建立关系 |
|---|---|---|
| 触发 | `enroll_speaker` 完成 | `resolve_person` 调用 |
| 查图 | ✅ 同一套逻辑 | ✅ 同一套逻辑 |
| 确认时机 | **立即** | **立即** |
| 用户不在时 | 放弃本次绑定 | 不适用(用户正在对话) |
| 落地 | §8.5 | §9.3 |

**两个场景共用同一套查图 + 三态确认逻辑**(公共方法 `find_candidates_by_name` / `confirm_or_distinguish`)。

### 8.6 绑定的幂等性

```
再次采集李xx的声纹
    ↓
enroll_speaker(user_id="spk_b2c8")  ← 已有声纹会 update 并保留原 id
    ↓
图侧:user_id 已存在 → 无需变更(或仅更新 embedding 相关)
```

现有 `enroll_speaker` 已支持传 `user_id` 保留原 id(`voiceprint_enrollment.py` 的 `known_user_id` 参数),**声纹侧不需要改**。

### 8.7 绑定失败的兜底

绑定是**附加操作**,失败不影响主流程:
- 绑定失败 → 记日志,声纹照常注册成功
- 图侧节点保持原状(仍是无声纹锚点)
- 下次绑定会重试

---

## 9. 人物关系确认(对话中的主动澄清)

### 9.1 场景

```
用户:"我妈明天去上海,帮我给她订张票。"
      ↓
管家不知道"我妈"是谁
      ↓
🧠 "你妈是谁?请说出她的名字。"
      ↓
用户:"李xx"
      ↓
🧠 调 resolve_person(display_name="李xx", relation_type="IS_MOTHER_OF", speaker=<用户user_id>)
      ↓
图侧:建/复用 [李xx] 节点 + 建 [用户] --IS_MOTHER_OF--> [李xx] 边
```

### 9.2 tool 设计

```python
@register_tool(
    name="resolve_person",
    description=(
        "登记或查询一个人,并建立人与人之间的关系。\n"
        "场景:用户提到某个人(如'我妈''我同事李雷'),"
        "当需要建立人际关系、或系统不知道这个人是谁时调用。\n"
        "参数:\n"
        "  display_name      —— 用户的名字(必填)\n"
        "  relation_type     —— 关系类型(如 IS_MOTHER_OF/IS_FRIEND_OF/WORKS_WITH)\n"
        "  speaker           —— 说话人的 user_id(从上下文取)\n"
        "  candidate_node_id —— 第二轮确认时传:用户已确认是哪一个候选人\n"
        "返回 status=needs_confirmation 时,说明图里已有同名的人,"
        "需要先向用户确认身份(把 candidates 的描述转述给用户听)。\n"
        "用户确认后再调用一次,带上 candidate_node_id。\n"
        "若用户说'不是/同名',不要传 candidate_node_id,重新调用即可(会自动建新节点区分)。"
    ),
    scope=ToolScope.GLOBAL,
    timeout_seconds=10.0,
    idempotent=True,
)
async def resolve_person(
    display_name: str,
    relation_type: str,
    speaker: str,
    candidate_node_id: str | None = None,
) -> dict[str, Any]:
    ...
```

### 9.3 tool 行为(三态)

```
1. 解析 display_name → node_id(路径 A/B/C,见 §7)
2. 若未传 candidate_node_id 且查图命中同名候选:
     → 返回 needs_confirmation + 候选描述(不建边)
3. 若传了 candidate_node_id → 绑定到该节点(或建新节点区分)
4. 否则 → 建边 speaker --relation_type--> node
5. 返回:
   {
     "status": "ok",
       "person": {"node_id":..., "name":..., "voice_bound": 0/1},
       "edge_created": true/false,
       "known": true/false,      ← 图里是否已认识这个人
   }
```

#### needs_confirmation 的返回结构

```json
{
  "status": "needs_confirmation",
  "candidates": [
    {
      "node_id": "person:张伟A",
      "name": "张伟",
      "summary": "张伟是我的同事,我们在项目A一起做过事",
      "voice_bound": false
    }
  ]
}
```

**`summary` 是候选的描述**(来自该节点活跃边的 fact 句,见 §8.4)—— LLM 把它转述给用户听:
> "是不是这个人:张伟,我的同事,我们在项目A一起做过事?"

**LLM 的两步调用**:
```
第一轮:resolve_person("张伟", IS_COLLEAGUE_OF, speaker)
       → needs_confirmation + candidates
       → LLM 问用户:"是不是这个人:张伟,我的同事...?"
第二轮:用户答"是" → resolve_person("张伟", IS_COLLEAGUE_OF, speaker, candidate_node_id="person:张伟A")
       用户答"不是" → resolve_person("张伟", IS_COLLEAGUE_OF, speaker)   ← 不传 id,自动建新节点区分
```

> **为什么"不是"时重调而不传 id**:不传 `candidate_node_id` 时,工具走"未确认候选"的路径 → 建新节点 `person:name:张伟#2`。这与 §8.4 的三态处理一致。

### 9.4 澄清的触发方:LLM,不是代码

**关键分工**:
- **代码(tool)**:提供"查询/登记人物 + 建边"的能力
- **LLM**:决定**何时**该问用户"你妈是谁"

这样:
- 澄清话术自然(LLM 生成,贴合上下文)
- 代码不需要写状态机去追踪"待澄清的指代"
- 与既有 tool 机制(`enroll_speaker` / `decline_speaker_enrollment`)一致

> 参考:`enroll_speaker` 的 `confirm` 硬门槛 —— 那里是**代码**强制(涉及生物特征隐私必须显式同意);
> 这里只是**关系登记**,不涉及隐私,所以由 LLM 判断时机即可。

### 9.5 与声纹采集的联动

如果 LLM 发现某人**从未登记声纹**,可以顺势询问是否录入:

```
🧠 "李xx 是你妈妈吗?要不要我记住她的声音,以后能听出她是谁?"
      ↓ 用户同意 → enroll_speaker(display_name="李xx")
      ↓ 采集完成 → §8 绑定流程 → 节点升级为声纹锚定
```

**这条路径把"人物图"和"声纹"闭环了**:图里知道是谁 → 声纹绑定 → 以后说话能听出是谁 → 又能更新图。

---

## 10. LLM 抽取设计

### 10.1 输入(决策①:A3)

```
[对话片段]
[user=spk_a3f9] 我妈明天去上海,帮我给她订张票
[assistant] 好的,已经帮您预订

[已有事实]
- spk_a3f9: 我妈明天去上海,帮我给她订张票 (valid_at=2026-11-10)
```

两个来源拼接:
- **fact** → 提供骨架:谁说了什么 + `valid_at`(决策④的时间来源)
- **原始消息** → 提供细节(管家回复里的结论、上下文)

**不增加抽取轮次** —— 两者都是同一 task 的产物。

### 10.2 Prompt 硬规则(抄 Graphiti + 本期特化)

```
1. ALWAYS extract the speaker as a Person entity.
2. Extract only PEOPLE as entities — 只抽人,不要抽公司/地点/物品/事件。
3. 亲属称谓(妈妈/爸爸/奶奶/外公)也是 Person 实体,亲属关系表达为**边**。
4. DO NOT create nodes for relationships or actions.
5. DO NOT create nodes for temporal information.
6. Each fact must relate two DISTINCT Person nodes.
7. relation_type 用 SCREAMING_SNAKE_CASE,从给定词表里选。
8. 端点必须来自已抽取的人物列表。
9. 每条边标注 fact_ref,指向它依据的 fact。
10. 若关系涉及非人实体(上海/公司/龙虾),不要为它建边 —— 只保留纯人物关系边。
```

> ⚠️ **注意**:prompt 里含 JSON 示例,**不能用 `.format()`**(花括号会被当占位符)。用 `.replace()`。

### 10.3 输出 schema(示意)

```json
{
  "nodes": [
    {"name": "我",       "ref_id": "spk_a3f9"},
    {"name": "李xx",     "ref_id": ""}
  ],
  "edges": [
    {"source": "我", "relation_type": "IS_MOTHER_OF",
     "target": "李xx", "fact": "李xx是我妈妈", "fact_ref": "f_1"}
  ]
}
```

### 10.4 降级策略

| 失败点 | 行为 |
|--------|------|
| LLM 超时 / 报错 | 整体跳过该 task,记 `graph_task_log.status='failed'`,**不阻塞** task 流程 |
| JSON 解析失败 | 同上 |
| 端点不在人物列表 | **丢弃该边**(不降级、不猜) |
| 谓词不在词表 | 降级 `RELATES_TO`,保留 `fact` 句 |
| 端点非 Person(LLM 抽了公司等) | **整条边丢弃**(决策⑤) |
| 超过边数上限 | 截断 + 告警日志(`graph_edge_max_per_task`) |

**总原则**:图是**增强层**,任何环节失败都不能影响主对话流程。

---

## 11. 召回融合(Step 3.6)

### 11.1 定位:补充,不替换

现有向量召回**完全不变**。图通道作为**补充通道**加入:

```
向量召回 top-k ──┐
                 ├──→ 合并去重 → prompt
图通道 1 跳 ────┘
```

**为什么不替换**:现有召回已经过用户实际验证,语义模糊但重要的内容靠向量召回兜底。让图主导会牺牲这部分。

### 11.2 图通道流程

1. 从向量命中的 fact 提取 `user:` tag → 得到说话人 `user_id`
2. 查该人的 **1 跳活跃邻居边**(`invalid_at IS NULL`)
3. 渲染成可读文本:`"我 --IS_MOTHER_OF--> 李xx"`
4. 与向量结果合并去重

### 11.3 失效边的呈现

历史关系修正后,旧边仍在库但带 `invalid_at`。召回时:

- **活跃边** → 正常呈现
- **已失效边** → 标注失效时间,供 LLM 判断"曾经是,现在不是"
- **不做硬过滤** —— "张女士以前是我朋友吗"这类问题需要历史边

> 与 `facade.py` 已有的 `_render_time_hint()` 逻辑一致(Step 2 已实现 `invalidates:1` → "修正了此前的说法")。

### 11.4 对称关系的双向查询

对称关系(`IS_FRIEND_OF` / `WORKS_WITH` / `IS_SIBLING_OF` / `IS_SPOUSE_OF` / `IS_PARTNER_OF`)只存一条有向边,查询时两个方向都查。

---

## 12. 决策③的延伸:提升 task 切分能力

用户决策③ —— 不细分 task,而是去优化切分能力。**方向认同**:task 边界直接决定图的质量,task 切错了,关系就会挂到错误上下文里。

现有 `TaskSegmenter` 的可优化点(**独立议题,不在 Step 3 范围**):

| # | 问题 | 现状 | 改进方向 |
|---|------|------|---------|
| 1 | 上下文窗口太小 | `judge_window_size=2`(默认) | 切分判断需要更多上下文;调大需权衡 LLM 成本 |
| 2 | **缺实体延续性信号** | 纯靠 LLM judge 语义判断 | 若新消息提到当前 task 已有人物 → 大概率**不是**新 task(图建好后可用图的节点分布精确判断) |
| 3 | 改口 vs 换话题难分 | — | "她是我朋友…不对我们掰了"=同 task;"对了周末看房"=可能新 task。需人物+意图双重信号 |

**建议闭环**:Step 3 完成后,用图里的人物分布反哺 task 切分("这条消息提到的人是否在当前 task 出现过")。

---

## 13. 风险与应对

| 风险 | 影响 | 应对 |
|------|------|------|
| LLM 抽的节点类型飘移(抽了公司等) | 图谱污染 | 代码硬校验:非 Person 端点的边整条丢弃 |
| 单 task 边数爆炸 | 写库压力 | `graph_edge_max_per_task` 上限 + 截断告警 |
| 同一人多节点(称呼不一) | 关系碎片化 | 别名链 + 声纹绑定时合并;暂不做语义消解 |
| 同名不同人被合并 | 关系错连 | 本期无解;声纹绑定后可用不同 `user_id` 拆分 |
| 批内矛盾漏判 | 关系矛盾 | task 内一次抽出后内存消解(决策②的直接收益) |
| `valid_at` 粒度不够 | 边时间不准 | `fact_ref` 让边选归属 fact;后续可升级为边级抽取 |
| 边事实句被 LLM 编造 | 脏数据进图 | 事实句必须能在输入中找到依据 |
| 矛盾消解误伤历史 | 历史丢失 | 只加时间戳绝不 DELETE;完整单测 |
| 图召回挤占 prompt 预算 | 影响主流程 | 独立条数上限 + 按分数截断 |
| 图层故障影响主流程 | 管家不可用 | 全链路降级:抽取失败→跳过;召回失败→纯向量 |

**总原则:图是增强层。任何环节失败都不能影响主对话流程。**

---

## 14. 与既有决策的一致性检查

| 既有决策 | 是否冲突 | 说明 |
|----------|---------|------|
| 记忆以 task 维度组织 | ✅ 一致 | 图边界 = task 边界,溯源用 `task_ids` |
| fact 归属(who)不能丢 | ✅ 一致 | 边带 `speaker`;Person 节点带 `user_id` |
| 转述内容有置信度问题 | ✅ 一致 | 边的 `speaker` 是**说话人**,不是被描述者 |
| 代码强约束格式,LLM 做总结 | ✅ 一致 | LLM 提议节点/谓词/事实句,代码裁决并落库 |
| 时间用 `spoken_at` + `valid_at` | ✅ 一致 | 直接复用 Step 2 成果(决策④) |
| 5W1H 中 who 应有 | ✅ **强化** | who 从自由文本升级为图的 Person 节点 |
| 声纹解耦人物表,userId 用声纹哈希 | ✅ **强化** | 声纹 id 直接作为 Person 节点锚点 |
| 不引入不必要的重依赖 | ✅ 一致 | SQLite,零新部署依赖 |
| 不需要用户表/用户形象 | ✅ 一致 | 声纹库与图解耦,图按 `user_id` 引用 |

---

## 15. 已定事项汇总

| # | 事项 | 决定 |
|---|------|------|
| ① | 抽取输入 | **A3** —— fact + 原始消息拼接 |
| ② | 批处理粒度 | **一个 task 一次 LLM**,task 内内存消解 |
| ③ | task 是否细分 | **不细分**,转向优化切分能力 |
| ④ | 时间字段 | **复用 task/fact** 的 `valid_at` / `spoken_at` |
| ⑤ | 节点范围 | **只做 Person**;非人关系的边整条丢弃;无独立人物表 |
| ⑥ | 存储位置 | **独立 `data/graph.db`** |
| ⑦ | 未知人物 | 建**名字锚点**节点,后续声纹绑定升级(§8) |
| ⑧ | 人物关系确认 | LLM 主动询问 + `resolve_person` tool(§9) |
| ⑨ | 同名消歧 | **立即询问 + 三态确认**(§8.4):命中候选就问,不做延迟;用户不在时放弃本次绑定 |
| ⑩ | 定时任务/主动询问调度 | **本期不做** —— 定时任务系统尚未实现,留待后续需要时再优化(§8.5 取舍说明) |

---

## 16. 参考资料

| 来源 | 关键借鉴 |
|------|---------|
| [Graphiti (GitHub)](https://github.com/getzep/graphiti) | 三层图结构、combined prompt、边 uuid 判据、`edge_type_map` |
| [Zep 论文 (arXiv:2501.13956)](https://arxiv.org/html/2501.13956) | 双时态四时间戳、实体消解流程、矛盾消解按时间比较 |
| [Graphiti 边抽取与消解 (DeepWiki)](https://deepwiki.com/getzep/graphiti/5.3-edge-extraction-and-resolution) | 两阶段判重(fast path 精确 / slow path LLM)、同端点检索约束 |
| [Bulk Operations (DeepWiki)](https://deepwiki.com/getzep/graphiti/5.4-bulk-operations-and-optimization) | bulk 模式的批内可见性缺陷 —— 我们的规避依据 |
| [Graphiti issue #1872](https://github.com/getzep/graphiti/issues/1872) | bulk 去重的已知 bug —— 自研批内消解的必要性 |
| [Mem0 Graph Memory](https://docs.mem0.ai/platform/features/graph-memory) | 二分图方案对比 —— 无类型关系的局限 |
| [HippoRAG](https://arxiv.org/html/2405.14831v2) | OpenIE 无模式图 + PPR 多跳检索 |
