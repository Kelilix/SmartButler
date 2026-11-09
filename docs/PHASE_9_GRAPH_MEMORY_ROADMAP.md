# Phase 9 记忆图开发路线图

> **状态**:草稿,待用户确认后实施
> **日期**:2026-11-09
> **配套文档**:`docs/PHASE_9_GRAPH_MEMORY_DESIGN.md`(技术方案 —— 节点/边/存储/绑定/召回的完整设计)
> **范围**:Step 3.1 → 3.8 的实施顺序、每步产出、验收标准、风险门禁
> **前置**:Phase 6.2-6.5(已交付)+ Step 1/2 时间抽取(已交付)+ Phase 7.6 声纹采集(已交付)

---

## 0. 一页速览

```
3.1 存储层 ──→ 3.2 LLM 抽取 ──→ 3.3 节点解析 ──→ 3.4 落库
                                                │
                    ┌───────────────────────────┴──────────────┐
                    ↓                                          ↓
          3.5 矛盾消解                            3.6 声纹绑定
                    │                                          │
                    └────────────────┬─────────────────────────┘
                                     ↓
                              3.7 resolve_person tool
                                     ↓
                              3.8 召回融合
                                     ↓
                              3.9 观测与开关
```

**建议顺序**:`3.1 → 3.2 → 3.3 → 3.4 → (3.6 + 3.9) → 3.5 → 3.7 → 3.8`

- **3.6 声纹绑定**提前 —— 它是"人物图 ↔ 声纹"的闭环,验证了 §8 的绑定机制
- **3.5 矛盾消解**风险最高,放在主干跑通后
- **3.8 召回融合**最后 —— 需要图里有真实数据后才知道怎么融合最合适

| Step | 依赖 LLM | 可独立验证 | 风险 | 产出物 |
|------|:---------:|:----------:|------|--------|
| 3.1 存储层 | ❌ | ✅ | 低 | `graph/store.py`, `graph/models.py` |
| 3.2 LLM 抽取 | ✅ | ✅(mock) | 中 | `graph/extractor.py` |
| 3.3 节点解析 | 🟡部分 | ✅ | 中 | `graph/resolver.py`(人物专用,非通用消解) |
| 3.4 落库 | ❌ | ✅ | 中 | 批写 + 幂等 |
| 3.6 声纹绑定 | 🟡部分 | ✅ | 中 | 绑定 + 合并 |
| 3.5 矛盾消解 | 🟡部分 | ✅ | **高** | 历史关系修正闭环 |
| 3.7 tool | ❌ | ✅ | 中 | `resolve_person` |
| 3.8 召回融合 | ❌ | 🟡 | 中 | 图通道 |
| 3.9 观测开关 | ❌ | ✅ | 低 | 日志 + 配置开关 |

---

## 1. Step 3.1 — 存储层

**目标**:不依赖 LLM 的纯存储层,可独立测试验证。

### 1.1 任务清单

- [ ] `graph/models.py`:`GraphNode` / `GraphEdge` / `GraphEdgeCandidate` dataclass
- [ ] `graph/store.py`:`GraphStore` 类
  - [ ] 建表(`graph_entities` / `graph_edges` / `graph_task_log`)
  - [ ] 节点 CRUD:`upsert_node` / `get_node` / `find_by_user_id` / `find_by_name`
  - [ ] 别名追加:`add_alias(node_id, alias)`
  - [ ] 边 CRUD:`upsert_edge` / `find_edges_by_pair(source_id, target_id)`
  - [ ] 活跃边过滤:`invalid_at IS NULL`
  - [ ] 邻居查询:`neighbors(node_id, depth=1|2)` —— **递归 CTE**
  - [ ] 溯源追加:`append_edge_task(edge_id, task_id)`
  - [ ] **节点合并**:`merge_nodes(main_id, dup_id)`(§6.5)
  - [ ] 幂等日志:`task_log` 读写
- [ ] `graph/__init__.py`
- [ ] `config/memory.py`:新增配置项(默认全关)
- [ ] `wiring.py`:接线(受 `graph_enabled` 控制)
- [ ] 单测 `tests/unit/emotion/memory/test_graph_store.py`

### 1.2 验收标准

- [ ] 建表幂等(重复初始化不报错)
- [ ] 节点插入/更新/按 `user_id` 查/按 `name` 查正确
- [ ] 别名追加后可被 `find_by_name` 命中
- [ ] 边按 `(source_id, target_id)` 查得到
- [ ] `neighbors(depth=1)` 返回直接邻居;`depth=2` 返回两跳
- [ ] 已失效边(`invalid_at` 非空)不出现在活跃邻居查询中
- [ ] `append_edge_task` 不产生重复 task_id
- [ ] `merge_nodes` 后:被合并节点的边全部改指向主节点、aliases 合并、被合并节点删除
- [ ] `merge_nodes` 主节点选择遵循规则(优先 `voice_bound=1`,其次边多,再次早建)
- [ ] 递归 CTE 在 3 层深度下性能可接受(< 50ms)

### 1.3 关键设计

**边 ID**:`sha256(source_id | relation_type | target_id | normalize(fact))` —— 天然幂等。

**节点 ID**:
```
person:<user_id>              ← 声纹锚定(已绑定)
person:name:<hash>            ← 名字锚定(未绑定)
```

**递归 CTE 邻居查询**:
```sql
WITH RECURSIVE reach(node_id, depth) AS (
    SELECT ?, 0
    UNION
    SELECT CASE WHEN e.source_id = r.node_id THEN e.target_id ELSE e.source_id END,
           r.depth + 1
    FROM graph_edges e
    JOIN reach r ON e.source_id = r.node_id OR e.target_id = r.node_id
    WHERE r.depth < ? AND e.invalid_at IS NULL
)
SELECT DISTINCT node_id FROM reach WHERE depth > 0;
```

---

## 2. Step 3.2 — LLM 抽取

**目标**:一次 LLM 调用产出人物节点 + 人物关系边。

### 2.1 任务清单

- [ ] `graph/extractor.py`:`GraphExtractor`
  - [ ] 输入拼接(**决策① A3**):fact 骨架 + 原始消息细节
  - [ ] combined prompt(节点+边一次出)
  - [ ] JSON 解析 + schema 校验
  - [ ] 端点校验(必须命中本次人物列表)
  - [ ] 谓词白名单校验(词表外 → 降级 `RELATES_TO`)
  - [ ] **端点类型校验:非 Person → 整条边丢弃**(决策⑤)
  - [ ] `fact_ref` 标注
  - [ ] 超时/异常 → 降级返回空
- [ ] 边数上限 `graph_edge_max_per_task`
- [ ] 单测 `tests/unit/emotion/memory/test_graph_extractor.py`(mock LLM)

### 2.2 验收标准

- [ ] mock LLM 返回标准 JSON → 正确解析节点与边
- [ ] 端点不在人物列表的边**被丢弃**
- [ ] 谓词在词表外 → 降级 `RELATES_TO` 且保留 `fact`
- [ ] **LLM 抽出"上海""XX公司"作为端点 → 该边被丢弃**
- [ ] LLM 超时 → 返回空,不抛异常
- [ ] JSON 畸形 → 返回空,不抛异常
- [ ] 超过边数上限 → 截断 + 告警日志

### 2.3 Prompt 要点(抄 Graphiti + 本期特化)

```
1. ALWAYS extract the speaker as a Person entity.
2. Extract only PEOPLE as entities.
3. 亲属称谓也是 Person 实体,亲属关系表达为边。
4. DO NOT create nodes for relationships or actions.
5. DO NOT create nodes for temporal information.
6. Each fact must relate two DISTINCT Person nodes.
7. relation_type 从给定词表里选,SCREAMING_SNAKE_CASE。
8. 端点必须来自已抽取的人物列表。
9. 每条边标注 fact_ref。
10. 涉及非人实体的关系不要建边。
```

> ⚠️ **注意**:prompt 里含 JSON 示例,**不能用 `.format()`**(花括号会被当占位符)。用 `.replace()`。

---

## 3. Step 3.3 — 人物节点解析

**目标**:把候选人名映射到稳定的 `node_id`。**这不是通用实体消解**,是人物专用的三路径解析。

### 3.1 任务清单

- [ ] `graph/resolver.py`:`PersonResolver`
  - [ ] 路径 A:查声纹档案 `registry.get_by_display_name(name)` → `person:<user_id>`
  - [ ] 路径 B:查图内节点(`name` / `aliases` 归一化匹配)
  - [ ] 路径 C:名字归一化哈希 → `person:name:<hash>`
  - [ ] `normalize_person_name()`:去空格 / 全半角 / 去括号修饰 / lower
  - [ ] 需要注入 `SpeakerRegistry` 引用(经 `wiring.py`)
- [ ] 单测 `tests/unit/emotion/memory/test_graph_resolver.py`

### 3.2 验收标准

- [ ] 声纹档案命中 → `node_id = "person:<user_id>"`
- [ ] 图内 `name` 命中 → 复用已有节点
- [ ] 图内 `aliases` 命中 → 复用已有节点
- [ ] `"李雷(同事)"` 与 `"李雷"` 归一为同一节点
- [ ] 全部未命中 → 新建 `person:name:<hash>`
- [ ] 声纹注册表不可用时(降级)不崩,退到路径 B/C
- [ ] `"小李"` 与 `"李雷"` **不合并**(保守策略,已知局限)

### 3.3 风险

误合并比漏合并危害大 —— 合并错了关系就错连。归一化**保守处理**,宁可漏合不误合。

---

## 4. Step 3.4 — 落库

**目标**:批内消解 + 单事务批写 + 幂等。

### 4.1 任务清单

- [ ] 批内消解:task 内的边在**内存**做两两判重(绕开 Graphiti 批内互不可见的坑)
- [ ] 落节点:新节点插入;复用节点更新 `updated_at` / 追加 `aliases`
- [ ] 落边:
  - [ ] `edge_id` 已存在 → **只追加 `task_ids`**,不新增(零写压力)
  - [ ] `edge_id` 不存在 → 插入
- [ ] 写 `graph_task_log` 保证幂等
- [ ] `GraphExtractHook`:挂 `ON_TASK_COMPLETE`,fire-and-forget(与 `AutoExtractHook` 同模式)
- [ ] `wiring.py` 注入 `GraphExtractHook`
- [ ] 单测 `tests/unit/emotion/memory/test_graph_store_write.py`

### 4.2 验收标准

- [ ] 同一 task 抽两次 → 库中 **1 条边**,`task_ids` 有 2 个
- [ ] 不同 task 抽到同一事实 → 合并为 **1 条边**,`task_ids` 有 2 个
- [ ] `graph_task_log` 阻止同一 task 重复抽取
- [ ] 整个落库是**单事务**,中途失败不留下半截数据
- [ ] 落库失败**不影响** task 主流程(不阻塞用户对话)
- [ ] 边落库时 `valid_at` 取自 `fact_ref` 指向的 fact(决策④)

---

## 5. Step 3.6 — 声纹绑定 + 同名消歧(提前做)

**目标**:把新采集的声纹绑定到图里已有的人物节点;遇到同名时**立即询问**用户消歧。

### 5.1 任务清单

- [ ] `graph/binding.py`:`VoiceBinding`
  - [ ] 按名匹配节点(`name` / `aliases` 归一化)→ `find_candidates_by_name()`
  - [ ] **未命中** → 新建 `person:<user_id>` 节点(声纹锚点)
  - [ ] **命中 ≥1** → **立即询问用户**(三态,见 §8.4)
    - [ ] 用户答"是" → 绑定到该候选(`node_id` 不变,`voice_bound=1`)
    - [ ] 用户答"不是/同名" → 建新节点 `person:name:<name>#N` 区分
  - [ ] 候选描述来自节点 `summary`(活跃边 fact 句聚合)
  - [ ] 用户不在/询问失败 → **放弃本次绑定**,声纹照常注册(不阻塞)
  - [ ] 绑定失败 → 记日志,不阻塞声纹注册
- [ ] 节点 `summary` 聚合:Step 3.4 落库时更新(边变动 → 重算)
- [ ] 在声纹注册完成回调处接入
- [ ] 单测 `tests/unit/emotion/memory/test_graph_voice_binding.py`

### 5.2 验收标准

- [ ] 图里有"李xx"节点 → 绑定后 `node_id` **不变**,`user_id` 填上,`voice_bound=1`
- [ ] 绑定后**所有已有边仍然指向该节点**(无需重连)
- [ ] 图里无此人 → 新建 `person:spk_xxx` 节点
- [ ] **命中同名候选 → 返回询问,而不是直接绑定**
- [ ] 用户答"是" → 绑定到候选
- [ ] 用户答"不是" → 建 `person:name:张伟#2`,且**原节点不受影响**
- [ ] 多个节点同名 → 候选列表含全部,用户选定后绑定
- [ ] 用户不在/询问失败 → 放弃绑定,声纹注册仍成功,图侧仅记日志
- [ ] 重复绑定(再次采集)→ 幂等,`user_id` 不变

### 5.3 关键验收(技术基石)

> **"绑定声纹后,已有的边一条都不用改"** —— 这是 §2.3 节点 id 设计成立的核心判据。
> 专门的回归测试:`绑定前后边数量、边指向的 node_id 完全一致`。

### 5.4 本期不做的(留后续优化)

| 项 | 说明 |
|----|------|
| 延迟确认(pending 表) | 早期草案设计,现按用户决策取消 —— 立即询问,用户不在就放弃 |
| 定时任务主动询问 | 定时任务系统尚未实现,后续需要时再接(`submit_proactive` 已预留"定时器"入口) |

---

## 6. Step 3.9 — 观测与开关(全程并行)

**目标**:可观测、可灰度、可回滚。

### 6.1 任务清单

- [ ] 配置项:
  - [ ] `graph_enabled: bool = False`(默认关)
  - [ ] `graph_edge_max_per_task: int = 20`
  - [ ] `graph_dedupe_threshold: float = 0.85`
  - [ ] `graph_recall_max_edges: int = 10`
- [ ] 日志:每 task 的节点数 / 边数 / 复用数 / 矛盾数 / 耗时 / 丢弃数(按原因)
- [ ] 降级统计:LLM 失败次数 / 声纹绑定失败次数
- [ ] `.env` 示例补充

### 6.2 验收标准

- [ ] `graph_enabled=False` 时零副作用(不建表、不抽取)
- [ ] 开启后日志能完整还原一次抽取的全过程
- [ ] 关闭后已建数据不受影响

---

## 7. Step 3.5 — 矛盾消解(最高风险)

**目标**:历史关系修正闭环 —— "张女士不再是我朋友了"。

### 7.1 任务清单

- [ ] 同端点边检索(`find_edges_by_pair`,含对称关系的反向查)
- [ ] 判重两阶段(抄 Graphiti):
  - [ ] **fast path**:同端点 + `normalize(fact)` 完全相同 → 复用,**零 LLM**
  - [ ] **slow path**:同端点 + 向量 ≥ `graph_dedupe_threshold` → 调 LLM 判「重复 / 矛盾 / 无关」
- [ ] 矛盾 → 旧边打 `invalid_at` + `expired_at`(**不删**),新边写入
- [ ] 新边带 `invalidates` 标记(呼应 Step 2 的 `invalidates:1` tag)
- [ ] 时间比较:**新的 `valid_at` 更晚才赢**(不按到达顺序)
- [ ] 单测 `tests/unit/emotion/memory/test_graph_invalidation.py`

### 7.2 验收标准

- [ ] 重复陈述 → **零写操作**,只追加溯源
- [ ] 矛盾陈述 → 旧边 `invalid_at` 有值、新边入库、**旧边仍在库**
- [ ] 无矛盾 → 不误伤(不同关系的边不受影响)
- [ ] 时间上更早的新边**不覆盖**更晚的旧边
- [ ] 失效边不出现在活跃邻居查询中
- [ ] 对称关系(`IS_FRIEND_OF` 反向)也能正确识别矛盾

### 7.3 风险门禁

这是**唯一会改写历史数据**的步骤,风险最高。

- [ ] 必须有完整单测覆盖上述 6 条验收标准
- [ ] 失效操作**只加时间戳,绝不 DELETE**
- [ ] 上线前在测试库跑一遍完整回归

---

## 8. Step 3.7 — `resolve_person` tool

**目标**:对话中的人物登记与关系建立。

### 8.1 任务清单

- [ ] `capabilities/tools/graph/resolve_person.py`
  - [ ] `@register_tool(name="resolve_person", scope=ToolScope.GLOBAL)`
  - [ ] 参数:`display_name`(必填) / `relation_type`(必填) / `speaker`(必填) / `candidate_node_id`(可选,第二轮确认用)
  - [ ] 行为:解析节点 → 建边 → 返回 `status` / `person` / `edge_created` / `known`
  - [ ] **三态**:
    - [ ] `status="ok"` —— 已建/复用,完成
    - [ ] `status="needs_confirmation"` —— 图里已有同名候选,返回 `candidates`(含 `summary` 描述),**不建边**
    - [ ] `status="error"` —— 失败,不抛异常
  - [ ] 幂等:重复调用不产生重复边
  - [ ] 失败降级:返回错误信息,不抛异常
- [ ] tool description 写清调用时机(LLM 靠它决定何时问用户)+ 两步调用说明
- [ ] 单测 `tests/unit/emotion/memory/test_resolve_person_tool.py`

### 8.2 验收标准

- [ ] 首次登记 → 建节点 + 建边
- [ ] 重复登记同一人 → 复用节点,不重复建
- [ ] 声纹已绑定的人 → 节点 `voice_bound=1`
- [ ] 幂等:连续两次调用 → 库中 1 条边
- [ ] **命中同名候选 → 返回 `needs_confirmation` + 候选描述,不建边**
- [ ] 传 `candidate_node_id` → 绑定/建边到该节点
- [ ] 不传 `candidate_node_id` 且用户答"不是" → 建 `person:name:<name>#N` 区分节点
- [ ] tool description 明确说明"系统不知道这个人是谁时应先问用户"

### 8.3 分工确认

- **代码(tool)**:提供"查询/登记人物 + 建边"能力 + 三态返回
- **LLM**:决定**何时**问用户"你妈是谁",并把候选的 `summary` 转述给用户

澄清话术由 LLM 生成,贴合上下文;代码不写状态机追踪"待澄清的指代"。

### 8.4 与 Step 3.6 的共用逻辑

两个场景(声纹绑定 / 对话中建关系)共用:
- `find_candidates_by_name()` —— 按名查候选节点
- 三态确认处理 —— "是"绑定 / "不是"建新节点区分

**区别只在触发时机**,不在逻辑。

---

## 9. Step 3.8 — 召回融合(最后做)

**目标**:图通道作为**补充**接入现有召回。

### 9.1 任务清单

- [ ] `facade.py`:新增图通道
  - [ ] 从向量命中 fact 提取 `user:` tag → 说话人 `user_id`
  - [ ] 查 1 跳活跃邻居边
  - [ ] 对称关系双向查询
  - [ ] 渲染成可读文本(复用 `_render_time_hint()` 的失效标注逻辑)
  - [ ] 与向量结果合并去重
- [ ] 独立条数上限 `graph_recall_max_edges`
- [ ] 单测 `tests/unit/emotion/memory/test_graph_recall.py`

### 9.2 验收标准

- [ ] 向量召回逻辑**完全不变**(回归测试通过)
- [ ] 问"我妈是谁"能召回 `IS_MOTHER_OF` 关系
- [ ] 失效边带失效时间标注呈现
- [ ] 历史问题("张女士以前是我朋友吗")能召回已失效边
- [ ] 图通道失败 → 静默降级到纯向量召回
- [ ] 图通道条数不超 `graph_recall_max_edges`

---

## 10. 里程碑与验证节奏

| 里程碑 | 包含 Step | 验证方式 | 产出 |
|--------|-----------|---------|------|
| **M1** | 3.1 | 单测 | 存储层可用,能存能查能合并 |
| **M2** | 3.1 + 3.2 + 3.3 | 单测(mock LLM) | 能从 task 抽出人物+关系候选并解析 |
| **M3** | + 3.4 + 3.9 | 单测 + 手工跑一个 task | 端到端:task → 图里有人物节点和关系边 |
| **M4** | + 3.6 | 单测 + 场景验证 | **声纹绑定闭环**:已有节点绑定声纹,边不用改;同名 → 询问消歧 |
| **M5** | + 3.7 | 手工对话验证 | "我妈是谁"→ 询问 → 登记 → 建边;同名 → 二轮确认 |
| **M6** | + 3.5 | 单测 + 场景验证 | 改口场景:旧边失效,新边入库,历史保留 |
| **M7** | + 3.8 | 手工验证召回 | 问"我妈"能召回关系 |

**M7 完成后**,进入 §12 的 task 切分能力优化。

---

## 11. 全局风险清单

| 风险 | 影响 | 应对 | 门禁 |
|------|------|------|------|
| LLM 抽了非人实体 | 图谱污染 | 非 Person 端点的边整条丢弃 | 3.2 |
| 单 task 边数爆炸 | 写库压力 | `graph_edge_max_per_task` 上限 + 截断告警 | 3.2 |
| 同一人多节点 | 关系碎片化 | 别名链 + 声纹绑定合并;暂不做语义消解 | 3.3 / 3.6 |
| 同名不同人被合并 | 关系错连 | ✅ **立即询问消歧**(§8.4):三态确认,不靠算法猜 | 3.6 / 3.7 |
| 声纹绑定时用户不在 | 漏掉一次绑定 | 放弃本次绑定(声纹仍注册);下次对话建关系时重走确认 | 3.6 |
| 批内矛盾漏判 | 关系矛盾 | task 内内存消解 | 3.4 |
| `valid_at` 粒度不够 | 边时间不准 | `fact_ref` 选归属 fact | 3.4 |
| 边事实句被 LLM 编造 | 脏数据进图 | 必须可在输入中找到依据 | 3.2 |
| 矛盾消解误伤历史 | 历史丢失 | 只加时间戳绝不 DELETE;完整单测 | 3.5 |
| 图召回挤占 prompt 预算 | 影响主流程 | 独立条数上限 + 按分数截断 | 3.8 |
| 图层故障影响主流程 | 管家不可用 | 全链路降级:抽取失败→跳过;召回失败→纯向量;绑定失败→仅记日志 | 全部 |

**总原则:图是增强层。任何环节失败都不能影响主对话流程。**

---

## 12. 后续议题(不在 Step 3 范围)

### 12.1 task 切分能力优化(决策③的延伸)

**背景**:task 边界直接决定图质量 —— task 切错了,关系就挂到错误上下文。

| # | 问题 | 改进方向 |
|---|------|---------|
| 1 | `judge_window_size=2` 上下文太少 | 调大需权衡 LLM 成本,先测效果 |
| 2 | **缺实体延续性信号** | 新消息提到当前 task 已有人物 → 大概率不是新 task;图建好后可用图节点分布精确判断 |
| 3 | 改口 vs 换话题难分 | 需人物+意图双重信号 |

**闭环路径**:Step 3 完成后 → 图里的人物分布反哺 task 切分 → 切分更准 → 图质量更高。

### 12.2 非人实体的引入时机

用户决策:先把人际关系做透,再拓展其他实体。引入时需评估:

| 引入时机 | 触发条件 |
|---------|---------|
| 数据驱动 | 人际关系跑通后,统计 fact 中涉及非人实体的比例 |
| 需求驱动 | 出现明确需求(如"我朋友的公司是哪家") |
| 技术准备 | 通用实体消解(三级漏斗)代码到位 —— 当前 Person 专用解析不覆盖 |

### 12.3 边的精化

| 项 | 说明 |
|----|------|
| 边级时间抽取 | 若 `fact_ref` 方案不够精确,再升级为边单独抽时间 |
| 边属性抽取 | Graphiti 支持自定义 edge type 带结构化属性 |
| 2 跳召回 | 先做 1 跳,2 跳收益/成本比待验证 |

### 12.4 社区层

**不需要独立实现** —— 日报/周报/月报已是 Graphiti Community 层的等价物。

---

## 13. 参考

- 技术方案:`docs/PHASE_9_GRAPH_MEMORY_DESIGN.md`
- 业界实践:见技术方案 §16 参考资料
