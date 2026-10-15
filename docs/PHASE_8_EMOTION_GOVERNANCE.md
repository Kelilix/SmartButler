# Phase 8 技术方案:情绪治理引擎(Emotion Governance Engine)

> **范围**:Phase 8 全周期(8.1 EmotionMonitor / 8.2 InterventionPlanner / 8.3 InterventionExecutor / 8.4 RecoveryTracker / 8.5 DataPruner / 8.6 PatternArchive)
> **不包含**:多节点 Redis Streams 部署(顺延到 Phase 9+)/ 7.9 多模态记忆(沿用 PHASE_7 §12 观察项)/ 真实硬件对接(7.5/7.7 已完成基础,执行器只调接口)
> **前置依赖**:Phase 6.5 IntentRecognizer ✅ / Phase 6.6 EntityGraph ✅ / Phase 6.7 PatternExtractor + ProactiveReasoning ✅ / Phase 7.5 AnswerRouter + OutputAdapter ✅ / Phase 7.7 TTS ✅
> **关联文档**:`docs/PHASE_7_TECHNICAL_DESIGN.md`(事件驱动 + 路由 + 7.5/7.7 接口)+ `docs/PHASE_6.2-6.5_TECHNICAL_DESIGN.md`(意图识别 + 任务归属 + 实体图谱)
> **关联 ADR**:`smartbutler/thinking/ADR-009-proactive-reactive-dual-loop.md`(双循环中"proactive 主动智能"在此阶段真正落地)

> **🆕 定位重定义(2026-10-15,本轮敲定)**:
> - 原 Phase 8(ADR-008 残留)被定位为"多节点 Redis Streams 部署"——**对单进程 dev_chat 过度设计,顺延到 Phase 9+**。
> - **本方案把"情绪治理引擎"作为 Phase 8 真正核心**——这是 SmartButler 区别于 Miloco 2.0(被动档案+临时推理)与 ChatGPT 类工具(无长期家庭画像)的**最大护城河**。
> - **核心范式转移**:Miloco 2.0 是"看到事实 → 临时推理 → 决策",SmartButler 是"看到事实 → 持久化为关系 → 派生行为模式 → **主动治理情绪** → 持续追踪 → 标记清除"。

> **🆕 伦理与隐私护栏(本轮敲定,贯穿全方案)**:
> - 情绪数据**全部本地存储**(`data/emotion.db`,不外传任何 LLM / 云端)
> - 主动 TTS 干预**默认关闭**(`enable_voice_intervention = False`)
> - 用户可一键关闭任一类型干预,且每次干预有日志
> - 管家**不是心理医生**——只做"识别到异常 → 推一把让用户寻求专业帮助",**不做心理干预本身**
> - 静默观察是默认模式,主动干预是例外

---

## 0. 当前状态盘点(2026-10-15)

### 0.1 已有基础(本方案依赖)

| 能力 | 来源 | Phase 8 怎么用 |
|------|------|--------------|
| **EntityGraph**(实体-关系表 + GraphQuery 封装) | Phase 6.6 | 情绪关系存为带 `sentiment` 字段的 Relation |
| **Fact 升格 + candidate 区** | Phase 6.6 | 情绪相关 fact 走同一升格链路 |
| **BehaviorPattern**(行为模式派生) | Phase 6.7 | 治理成功案例归档为 `emotion_recovery` 模式 |
| **ProactiveReasoning**(基于 pattern) | Phase 6.7 | 主动干预用模式查找替代纯 LLM 推理 |
| **OutputAdapter + TTS** | Phase 7.5 + 7.7 | 干预执行通过标准 OutputAdapter 链路 |
| **AnswerRouter**(目标设备路由) | Phase 7.5 | TTS 干预路由到"爸爸"房间的智能音箱 |
| **InMemoryEventBus / ReactiveSubmission.metadata** | Phase 7.0-7.3 | 干预事件作为 `system_event` 投回 EventBus 闭环 |

### 0.2 真实缺口(本方案要补)

| 缺口 | 现状 | 风险 |
|------|------|------|
| **没有"情绪"作为第一公民** | EntityGraph 的 Relation 没有 `sentiment` / `sentiment_score` 字段 | 管家没法区分"妈妈喜欢 24°C" 和 "妈妈最近 22:00 后独自进厨房" |
| **没有负面因子计算** | 没有任何量化"这个人最近情绪多差"的指标 | 主动智能只能"看一条 fact 推一条",没有"看趋势" |
| **没有干预规划** | ProactiveReasoning 只做"给建议",不做"找最优方案" | 同样"妈妈情绪低落",可能推 5 个不相关建议 |
| **没有恢复追踪** | 干预完就忘,不知道有没有效果 | 同样方案可能反复触发,实际用户没改善 |
| **没有数据清除** | 所有历史 fact 一律保留,负面数据不会"释放" | 负面情绪长期累积,管家越来越"灰心" |
| **没有成功模式归档** | 干预完不复盘,下次同样情况"重新学" | 浪费 LLM 调用,无法"越用越聪明" |

### 0.3 与 Miloco 2.0 的关键差距(本方案核心价值)

| 维度 | Miloco 2.0 | SmartButler Phase 8 |
|------|------------|---------------------|
| **情绪极性** | ❌ 平铺文本(无 sentiment 字段) | ✅ Relation 一等公民属性 |
| **跨任务模式** | ❌ 临时 LLM 推理,跨轮不累积 | ✅ BehaviorPattern 持久化 |
| **主动干预** | ⚠️ "基于常识 + 档案临时推理" | ✅ 反向追溯根因 + 历史成功模式 + LLM 设计方案 |
| **效果追踪** | ❌ 无 | ✅ 7 天观察 + 48 小时正因子持续 |
| **数据生命周期** | ❌ 一次性写入,不释放 | ✅ ACTIVE → RESOLVED 状态机 |
| **学习闭环** | ❌ 无 | ✅ 成功案例归档为可复用 pattern |

---

## 1. 案例驱动(为什么需要情绪治理)

> **本节是 Phase 8 全部设计的源起**——用户(2026-10-15 12:32 提问)用一个 5 轮对话案例,把 SmartButler 的"情绪治理"从模糊愿景变成具体可实现的设计。本节先复盘这个案例,后续章节按这个案例的步骤展开。

### 1.1 输入:5 轮对话

```
轮 1: "今天太累了"     (情绪:疲惫)
轮 2: "我老公还没回来"  (查询:老公状态)
轮 3: "他最近老加班"    (抱怨:老公加班)
轮 4: "要不我先吃吧"    (决策:独自吃饭)
```

### 1.2 任务归属(Phase 6.5 输出)

```yaml
task:
  id: task-2026-10-15-dinner
  attributed_to: person:mom
  intent_type: family_decision              # 12 类意图中的"家庭决策"
  emotion_track: [疲惫, 焦虑, 抱怨, 孤独]  # 情绪时间线
  confidence: 0.85
  summary: "妈妈疲惫 + 老公未归 + 抱怨加班 + 决定独自吃饭"
  status: COMPLETED
```

### 1.3 实体 + 关系抽取(Phase 6.6 输出,带情绪极性)

```python
# 关键设计:每个 Relation 都有 sentiment 字段(本方案新增)
Relation(from="person:mom", rel="feels", to="emotion:lonely",
         weight=0.7, sentiment="negative", sentiment_score=-0.7)

Relation(from="person:mom", rel="performs", to="behavior:eat_alone",
         weight=0.6, sentiment="negative", sentiment_score=-0.4)

Relation(from="person:dad", rel="associated_with", to="event:late_work",
         weight=0.8, sentiment="neutral", sentiment_score=0.0)

# 派生关系(从历史 pattern 推断)
Relation(from="behavior:eat_alone", rel="correlates_with", to="bp:mom-insomnia",
         weight=0.5, sentiment="negative", sentiment_score=-0.3, derived_from_pattern=True)
```

### 1.4 情绪治理引擎完整流程(本方案核心)

```
输入:5 轮对话
  ↓
[6.5 TaskClusterer] 任务归属 + 情绪时间线
  ↓
[6.6 FactExtractor] 提取 fact(带 sentiment 标注)
  ↓
[6.6 EntityExtractor] 抽取实体 + 关系(带 sentiment)
  ↓
[6.6 FactConsolidator] 升格到图谱
  ↓
[8.1 EmotionMonitor] 实时计算负面因子
  ↓  负面因子 > 阈值(默认 0.5)
[8.2 InterventionPlanner] LLM 设计干预方案
  ↓  选最优方案
[8.3 InterventionExecutor] 调用 7.7 TTS 主动播报
  ↓
[8.4 RecoveryTracker] 每日监控负面因子变化(7 天窗口)
  ↓  因子转正 + 持续 48 小时
[8.5 DataPruner] 标记历史负面数据为 RESOLVED
  ↓
[8.6 PatternArchive] 把"emotion_recovery"作为新模式入库
  ↓
反馈: [6.7] ProactiveReasoning 升级
  → 下次类似场景,管家有"成功经验"可复用
```

### 1.5 持续监控(本方案亮点)

```
Day 1 (10-15): 触发干预
  → 找爸爸谈 + 建议烛光晚餐
  → 负面因子: -0.56

Day 2 (10-16): 监控
  → 妈妈对话: "今天还行" (情绪=平静)
  → 爸爸对话: "今晚早点回来" (承诺)
  → 负面因子: -0.45 (+0.11,微升)

Day 3 (10-17): 监控
  → Omni 观察: 爸爸 19:30 回家,全家一起吃饭
  → 妈妈对话: "红烧肉不错" (积极)
  → 负面因子: -0.20 (+0.25,显著升)

Day 4 (10-18): 监控
  → 妈妈对话: "他最近正常多了" (情绪=缓和)
  → 负面因子: -0.10 (+0.10,继续升)

Day 5 (10-19): 监控
  → 妈妈对话: "周末一起去公园?" (积极建议)
  → 负面因子: +0.05 (转正!)

Day 6 (10-20): 持续观察
  → 负面因子维持正区间 48 小时
  → 决策: 清除历史负面数据(标记 RESOLVED,不真删)
```

---

## 2. 架构总览(6 个模块 + 数据流)

### 2.1 模块依赖图

```
┌────────────────────────────────────────────────────────────┐
│              SmartButler 情绪治理引擎(Phase 8)              │
│                                                            │
│  输入: [6.6] 升格的 fact / [6.7] 行为模式                   │
│   ↓                                                        │
│  [8.1] EmotionMonitor ⭐ 入口(实时)                          │
│   ├─ 实时计算 negative_factor / positive_factor            │
│   ├─ 负面因子 > 0.5 → 触发 InterventionRequest             │
│   └─ 输出: EmotionStateUpdate(包含是否需要干预)             │
│      ↓                                                      │
│  [8.2] InterventionPlanner                                 │
│   ├─ 反向追溯负面根因(2-3 跳图遍历)                          │
│   ├─ 查询历史成功 pattern(emotion_recovery)                  │
│   ├─ LLM 设计 2-3 个方案 + 排序                              │
│   └─ 输出: list[InterventionPlan](按 expected_impact 降序)  │
│      ↓                                                      │
│  [8.3] InterventionExecutor                                │
│   ├─ 选最优方案,翻译为系统调用(TTS / 设备 / 安排)             │
│   ├─ 调用链路: 7.5 AnswerRouter → 7.7 TTS OutputAdapter     │
│   └─ 输出: ExecutionResult(成功/失败/副作用)                  │
│      ↓                                                      │
│  [8.4] RecoveryTracker(异步,7 天观察期)                      │
│   ├─ 每日检查情绪因子变化                                    │
│   ├─ 持续 7 天 / 48 小时正因子 → 恢复                        │
│   ├─ 恶化 → 升级干预(回到 8.2)                               │
│   └─ 输出: RecoveryStatus(IN_PROGRESS/IMPROVING/RECOVERED)  │
│      ↓                                                      │
│  [8.5] DataPruner                                           │
│   ├─ 恢复成功 → 标记负面 fact/relation 为 RESOLVED           │
│   ├─ 保留历史(不真删),status: ACTIVE → RESOLVED             │
│   └─ 输出: PruneResult(归档数量)                              │
│      ↓                                                      │
│  [8.6] PatternArchive                                      │
│   ├─ 把成功模式入库(bp-emotion-recovery-xxx)                 │
│   ├─ 下次类似场景可复用                                      │
│   └─ 输出: BehaviorPattern(写入图谱)                          │
│      ↓                                                      │
│  反馈: [6.7] ProactiveReasoning                             │
│   └─ 行为模式 + 情绪治理模式 → 主动智能更强                   │
└────────────────────────────────────────────────────────────┘
```

### 2.2 与 Phase 6.x / 7.x 的关系

| Phase 8 模块 | 调用 | 被调用 |
|-------------|------|--------|
| **8.1 EmotionMonitor** | 6.6 EntityGraph.query_relations() | 8.2 InterventionPlanner / 8.4 RecoveryTracker |
| **8.2 InterventionPlanner** | 6.6 EntityGraph.find_patterns() / LLM | 8.3 InterventionExecutor |
| **8.3 InterventionExecutor** | 7.5 AnswerRouter / 7.7 TTS / 7.5 DeviceRegistry | 7.0 EventBus(投回 system_event) |
| **8.4 RecoveryTracker** | 8.1 EmotionMonitor / 8.5 DataPruner | 无(终态) |
| **8.5 DataPruner** | 6.6 EntityGraph.update_fact/relation | 6.6 EntityGraph(写入 emotion_recovery fact) |
| **8.6 PatternArchive** | 6.6 EntityGraph.add_pattern | 6.7 ProactiveReasoning(下次主动智能用) |

### 2.3 异步执行边界

| 模块 | 同步/异步 | 阻塞主流程? | 触发周期 |
|------|----------|------------|---------|
| **8.1 EmotionMonitor** | 异步(后台 worker) | ❌ | 每次 fact 升格后(< 100ms) |
| **8.2 InterventionPlanner** | 异步 | ❌ | 8.1 触发后立即执行(< 2s) |
| **8.3 InterventionExecutor** | 异步 | ❌ | 8.2 完成后立即执行(< 1s) |
| **8.4 RecoveryTracker** | 异步(独立 worker 池) | ❌ | 每日 1 次定时任务 |
| **8.5 DataPruner** | 同步(在 8.4 内部) | ❌ | 8.4 判定恢复时立即执行 |
| **8.6 PatternArchive** | 同步(在 8.5 内部) | ❌ | 8.5 完成后立即执行 |

**关键设计**:**情绪治理全程异步,不阻塞用户对话/感知流**。

---

## 3. 情绪因子算法(8.1 EmotionMonitor 核心)

### 3.1 PoC 简化版(2 维)

> **本方案 PoC 阶段只做 2 维,完整 4 维 PAD 模型留 Phase 10+**。

```python
@dataclass
class EmotionState:
    """某成员当前情绪状态(PoC 2 维版)。"""
    subject_id: str  # "person:mom"

    # 2 维(PoC 简化)
    negative_factor: float  # 负面因子 0.0 ~ 1.0(越高越需干预)
    positive_factor: float  # 积极因子 0.0 ~ 1.0(越高状态越好)

    # 元数据
    last_updated: datetime
    evidence_window_days: int  # 计算窗口(默认 7 天)
    trend: str  # "rising" / "stable" / "falling"
    prev_negative_factor: float | None  # 上一周期值(用于判定趋势)
```

### 3.2 负面因子计算公式

```python
# 负面因子 = Σ(weight × |sentiment_score| × freshness) / normalization

def compute_negative_factor(
    relations: list[Relation],
    *,
    window_days: int = 7,
) -> float:
    """
    负面因子算法(PoC 版本)。
    
    公式:
      factor = Σ(weight × |sentiment_score| × freshness) / N_max
      freshness = max(0.1, 1.0 - days_old / window_days)
    
    设计:
    - weight: 关系强度(0-1)
    - |sentiment_score|: 情绪强度绝对值(0-1)
    - freshness: 时间衰减(7 天前的负面关系权重降到 10% 起步)
    - N_max: 归一化常数(经验值 3.0,超过 3 个高强度负面关系就满分)
    """
    if not relations:
        return 0.0
    
    total_score = 0.0
    now = datetime.now()
    
    for rel in relations:
        if rel.sentiment != "negative":
            continue
        days_old = (now - rel.last_observed).days
        freshness = max(0.1, 1.0 - days_old / window_days)
        contribution = rel.weight * abs(rel.sentiment_score) * freshness
        total_score += contribution
    
    # 归一化到 [0, 1]
    N_max = 3.0  # 经验值,3 个高强度负面关系 = 满分
    return min(1.0, total_score / N_max)
```

### 3.3 完整 4 维 PAD 模型(Phase 10+ 观察项)

```python
# 完整版(借鉴心理学 PAD 模型,简化为 4 维):
#   valence: 效价(开心/不开心)        -1.0 ~ +1.0
#   arousal: 唤醒(激动/平静)          -1.0 ~ +1.0
#   dominance: 支配(主动/被动)        -1.0 ~ +1.0
#   connection: 连接(亲密/孤独)       -1.0 ~ +1.0
# 
# PoC 阶段不做,只做 2 维(negative_factor / positive_factor)
# 4 维化是 ~50 行改造,接口稳定
```

### 3.4 阈值与触发条件

| 指标 | 默认阈值 | 可配置 | 触发行为 |
|------|---------|--------|---------|
| `negative_factor` | ≥ 0.5 触发干预 | `config.emotion.negative_threshold` | 调 InterventionPlanner |
| `negative_factor` | ≥ 0.8 紧急干预 | `config.emotion.toxic_threshold` | 跳过 Planner,直接推送"建议联系专业心理咨询" |
| `positive_factor` | 持续 ≥ 0.3 48 小时 | `config.emotion.positive_sustained_hours` | 调 DataPruner 清除 |
| `trend` | 持续下降 3 天 | 自动检测 | 升级干预(回 8.2) |

### 3.5 完整 EmotionMonitor 实现骨架

```python
class EmotionMonitor:
    """实时情绪监测 + 负面因子计算。"""
    
    def __init__(
        self,
        graph: EntityGraph,
        llm: LLMClient,
        config: EmotionConfigSettings,
    ):
        self._graph = graph
        self._llm = llm
        self._config = config
        self._state_cache: dict[str, EmotionState] = {}
    
    async def on_fact_promoted(self, fact: Fact) -> EmotionStateUpdate | None:
        """每次 fact 升格时,实时更新情绪状态。
        负面因子超过阈值 → 返回 EmotionStateUpdate(trigger_intervention=True)。"""
        # 1. 查 subject 的所有负面关系(7 天窗口)
        negative_relations = await self._graph.query_relations(
            from_entity=fact.subject_id,
            sentiment="negative",
            within_days=self._config.window_days,
        )
        
        # 2. 查积极关系
        positive_relations = await self._graph.query_relations(
            from_entity=fact.subject_id,
            sentiment="positive",
            within_days=self._config.window_days,
        )
        
        # 3. 计算因子
        neg_factor = compute_negative_factor(negative_relations, window_days=self._config.window_days)
        pos_factor = compute_positive_factor(positive_relations, window_days=self._config.window_days)
        
        # 4. 趋势判断
        prev = self._state_cache.get(fact.subject_id)
        trend = self._compute_trend(neg_factor, prev)
        
        # 5. 更新状态
        new_state = EmotionState(
            subject_id=fact.subject_id,
            negative_factor=neg_factor,
            positive_factor=pos_factor,
            last_updated=datetime.now(),
            evidence_window_days=self._config.window_days,
            trend=trend,
            prev_negative_factor=prev.negative_factor if prev else None,
        )
        self._state_cache[fact.subject_id] = new_state
        
        # 6. 触发决策
        trigger = False
        reason = ""
        if neg_factor >= self._config.toxic_threshold:
            trigger = True
            reason = f"toxic 阈值触发 ({neg_factor:.2f} >= {self._config.toxic_threshold})"
        elif neg_factor >= self._config.negative_threshold:
            if not prev or prev.negative_factor < self._config.negative_threshold:
                trigger = True
                reason = f"负面因子 {neg_factor:.2f} 超过阈值 {self._config.negative_threshold}"
        
        if trigger:
            return EmotionStateUpdate(
                state=new_state,
                trigger_intervention=True,
                trigger_reason=reason,
            )
        
        return EmotionStateUpdate(state=new_state, trigger_intervention=False)
```

---

## 4. 干预规划算法(8.2 InterventionPlanner 核心)

### 4.1 设计原则

**LLM 不是"凭空想象"干预方案,而是基于图谱上的历史成功 pattern + 当前根因**——这是 SmartButler 区别于"无记忆 AI 心理咨询"的关键。

### 4.2 完整流程

```
负面因子超阈值
  ↓
Step 1. 反向追溯根因(2-3 跳图遍历)
  ↓
Step 2. 查询历史成功 pattern(emotion_recovery)
  ↓
Step 3. 查询当前可行干预手段(设备状态/时间/在家人)
  ↓
Step 4. LLM 设计 2-3 个方案(参考历史 pattern)
  ↓
Step 5. 排序(按 expected_impact 降序)
  ↓
Step 6. 选最优方案(或不选,只静默观察)
```

### 4.3 数据结构

```python
@dataclass
class InterventionPlan:
    """一个干预方案。"""
    id: str
    subject_id: str             # 谁需要干预(person:mom)
    target_id: str | None       # 干预作用到谁(person:dad,可能多个)
    intervention_type: str      # voice_tts / device_adjust / schedule_event / silent_observe
    urgency: str                # immediate / within_day / within_week
    message_template: str       # 给目标人的话术模板
    expected_impact: float      # 预期正面影响 +0.0 ~ +1.0
    side_effects: list[str]     # 可能的副作用
    fallback_plan: str | None   # 如果失败,降级方案
    reference_patterns: list[str]  # 引用的历史成功模式 bp-xxx
    
    created_at: datetime
    status: str                 # PROPOSED / SELECTED / EXECUTED / FAILED
```

### 4.4 干预类型(可配置 + 默认保守)

| 干预类型 | 默认启用 | 副作用 | 适用场景 |
|---------|---------|--------|---------|
| `voice_tts` | ⚠️ **默认关闭** | 打扰(被其他家人听到) | 用户明确授权,且 target 单独在家 |
| `device_adjust` | ✅ | 几乎无 | 调灯光/温度/音乐 |
| `schedule_event` | ✅ | 几乎无 | 安排日历/提醒 |
| `silent_observe` | ✅ | 0 | 只记录不行动(默认兜底) |
| `push_notification` | ⚠️ 默认关闭 | 中等 | 推送到用户手机(需要 APP) |

### 4.5 关键实现:反向追溯根因

```python
async def _trace_root_causes(self, subject_id: str) -> list[RootCause]:
    """从负面情绪反向追溯根因(2-3 跳图遍历)。
    
    例子:person:mom 情绪:lonely → 2 跳追溯
      跳 1: (person:mom) -[feels]-> (emotion:lonely)
      跳 2: (emotion:lonely) -[triggered_by]-> (event:late_work)
      跳 3: (event:late_work) -[caused_by]-> (person:dad schedule)
    → 根因: 爸爸频繁加班晚归
    """
    # 用 EntityGraph.query_paths() 多跳遍历
    paths = await self._graph.query_paths(
        from_entity=subject_id,
        to_entity=None,  # 任意终点
        max_hops=3,
        edge_filter={"sentiment": "negative"},
    )
    
    # 提取每条路径的"最远端实体"作为根因候选
    root_causes = []
    for path in paths:
        if len(path.edges) >= 2:
            last_edge = path.edges[-1]
            root_cause = RootCause(
                cause_entity=last_edge.to_entity,
                cause_relation=last_edge.rel_type,
                path_confidence=path.confidence,
                evidence_count=path.evidence_count,
            )
            root_causes.append(root_cause)
    
    return root_causes
```

### 4.6 关键实现:LLM 设计方案

```python
async def _generate_plans(
    self,
    state: EmotionState,
    root_causes: list[RootCause],
    successful_patterns: list[BehaviorPattern],
    available_interventions: list[AvailableIntervention],
) -> list[InterventionPlan]:
    """LLM 设计 2-3 个干预方案。"""
    
    # 1. 构建 LLM prompt
    prompt = f"""
你是 SmartButler 情绪治理规划师。当前需要为以下场景设计干预方案。

## 当前状态
- 对象: {state.subject_id}
- 负面因子: {state.negative_factor:.2f}
- 积极因子: {state.positive_factor:.2f}
- 趋势: {state.trend}

## 反向追溯的根因
{format_root_causes(root_causes)}

## 历史成功模式(可复用参考)
{format_patterns(successful_patterns)}

## 当前可行的干预手段
{format_interventions(available_interventions)}

## 任务
请设计 2-3 个干预方案,每个方案包含:
- intervention_type: voice_tts / device_adjust / schedule_event / silent_observe
- target_id: 干预作用到谁(单选)
- urgency: immediate / within_day / within_week
- message_template: 完整话术(给 target 看的话)
- expected_impact: 0.0 ~ 1.0
- side_effects: 可能副作用
- fallback_plan: 失败降级方案

要求:
1. 优先选择副作用小的方案(静默观察 > 设备调整 > 主动 TTS)
2. 涉及多人交互的方案,需要至少 2 维证据支持
3. 引用历史成功 pattern 给出依据
4. 如果所有方案都太激进,直接选 silent_observe

输出 JSON 数组。
"""
    
    # 2. LLM 推理
    raw_plans = await self._llm.structured_complete(
        prompt, schema=list[InterventionPlan]
    )
    
    # 3. 过滤:被禁用的类型(用户配置)
    enabled_types = self._config.get_enabled_intervention_types()
    filtered = [p for p in raw_plans if p.intervention_type in enabled_types]
    
    # 4. 排序:expected_impact 降序
    return sorted(filtered, key=lambda p: p.expected_impact, reverse=True)
```

### 4.7 完整 InterventionPlanner 流程

```python
class InterventionPlanner:
    """基于图谱的干预方案生成。"""
    
    def __init__(
        self,
        graph: EntityGraph,
        llm: LLMClient,
        config: EmotionConfigSettings,
    ):
        self._graph = graph
        self._llm = llm
        self._config = config
    
    async def plan(
        self,
        state: EmotionState,
        trigger_reason: str,
    ) -> list[InterventionPlan]:
        """负面因子超阈值 → 生成 2-3 个干预方案,排序。"""
        # Step 1: 反向追溯根因
        root_causes = await self._trace_root_causes(state.subject_id)
        
        # Step 2: 查询历史成功 pattern
        successful_patterns = await self._graph.find_patterns(
            pattern_type="emotion_recovery",
            subject=state.subject_id,
            min_confidence=0.7,
        )
        
        # Step 3: 查询当前可行干预
        available = await self._query_available_interventions(state)
        
        # Step 4: LLM 设计方案
        plans = await self._generate_plans(
            state, root_causes, successful_patterns, available
        )
        
        # Step 5: 排序
        return sorted(plans, key=lambda p: p.expected_impact, reverse=True)
```

---

## 5. 数据 Schema(8.1/8.4/8.5 核心)

### 5.1 EntityGraph 扩展(在 Phase 6.6 基础上加 sentiment 字段)

```sql
-- 关系表(扩展 sentiment 字段)
ALTER TABLE relation ADD COLUMN sentiment TEXT;  -- positive / negative / neutral / toxic
ALTER TABLE relation ADD COLUMN sentiment_score REAL;  -- -1.0 ~ +1.0
ALTER TABLE relation ADD COLUMN sentiment_evidence TEXT;  -- JSON,哪些 fact 推出这个 sentiment

-- 关系状态(支持 RESOLVED)
ALTER TABLE relation ADD COLUMN status TEXT DEFAULT 'ACTIVE';  -- ACTIVE / RESOLVED
ALTER TABLE relation ADD COLUMN resolved_at TEXT;
ALTER TABLE relation ADD COLUMN resolution TEXT;  -- emotion_recovered / manually_resolved / expired

-- fact 表(同样扩展)
ALTER TABLE fact ADD COLUMN sentiment TEXT;
ALTER TABLE fact ADD COLUMN status TEXT DEFAULT 'CANDIDATE';  -- CANDIDATE / PROMOTED / RESOLVED / DISCARDED
ALTER TABLE fact ADD COLUMN resolved_at TEXT;
ALTER TABLE fact ADD COLUMN resolution TEXT;
```

### 5.2 EmotionState 状态表

```sql
CREATE TABLE emotion_state (
    id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL,  -- person:mom
    
    -- 2 维情绪(PoC)
    negative_factor REAL NOT NULL,  -- 0.0 ~ 1.0
    positive_factor REAL NOT NULL,
    
    -- 完整 4 维(Phase 10+ 扩展位)
    valence REAL,      -- -1.0 ~ +1.0
    arousal REAL,      -- -1.0 ~ +1.0
    dominance REAL,    -- -1.0 ~ +1.0
    connection REAL,   -- -1.0 ~ +1.0
    
    -- 元数据
    evidence_window_days INTEGER NOT NULL DEFAULT 7,
    trend TEXT NOT NULL,  -- rising / stable / falling
    last_updated TEXT NOT NULL,
    
    UNIQUE(subject_id, last_updated)
);

CREATE INDEX idx_emotion_state_subject ON emotion_state(subject_id, last_updated DESC);
```

### 5.3 Intervention 记录表

```sql
CREATE TABLE intervention (
    id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL,          -- 谁需要干预
    target_id TEXT,                    -- 干预作用到谁
    intervention_type TEXT NOT NULL,   -- voice_tts / device_adjust / ...
    urgency TEXT NOT NULL,             -- immediate / within_day / within_week
    message_template TEXT NOT NULL,
    expected_impact REAL,
    side_effects TEXT,                 -- JSON
    reference_patterns TEXT,            -- JSON [bp-xxx, bp-yyy]
    
    -- 执行状态
    status TEXT NOT NULL,              -- PROPOSED / SELECTED / EXECUTED / FAILED / SKIPPED
    selected_at TEXT,
    executed_at TEXT,
    execution_result TEXT,             -- JSON ExecutionResult
    
    -- 追踪
    baseline_negative_factor REAL NOT NULL,  -- 干预前的基线
    recovery_status TEXT,              -- IN_PROGRESS / IMPROVING / STABLE_NEG / RECOVERED / DETERIORATED
    recovery_updated_at TEXT,
    recovery_ended_at TEXT,
    
    -- 治理结果
    resolved_facts_count INTEGER,
    resolved_relations_count INTEGER,
    
    created_at TEXT NOT NULL
);

CREATE INDEX idx_intervention_subject ON intervention(subject_id, created_at DESC);
CREATE INDEX idx_intervention_status ON intervention(status, recovery_status);
```

### 5.4 状态机

```
Fact 状态:
  CANDIDATE(候选区,Phase 6.6)
    ↓ 升格评估
  PROMOTED(已升格,参与因子计算)
    ↓ 情绪恢复
  RESOLVED(已解决,历史保留,不参与因子计算)
    ↓ 永久归档
  DISCARDED(丢弃)

Relation 状态:
  ACTIVE(活跃,参与因子计算)
    ↓ 情绪恢复
  RESOLVED(已解决,历史保留)

EmotionState 状态:
  (持续更新,无显式状态,latest row 生效)

Intervention 状态:
  PROPOSED(LLM 生成的方案,未选)
    ↓ InterventionPlanner 选最优
  SELECTED(已选,待执行)
    ↓ InterventionExecutor 执行
  EXECUTED(已执行) | FAILED(执行失败) | SKIPPED(被用户跳过)
    ↓ RecoveryTracker 追踪
  IN_PROGRESS → IMPROVING / STABLE_NEG / RECOVERED / DETERIORATED
    ↓ 恢复完成
  (终态,record 保留)
```

---

## 6. 隐私与伦理(贯穿全方案)

### 6.1 核心原则

| 原则 | 实现 | 验证 |
|------|------|------|
| **数据本地化** | `data/emotion.db` 在本机,不外传任何 LLM/云端 | 测试 1:无网络下能跑通 |
| **用户可控** | 任何干预用户能关闭,干预有日志 | 测试 2:一键关闭生效 |
| **静默优先** | 静默观察是默认模式,主动干预是例外 | 测试 3:默认配置下不主动干预 |
| **明确边界** | 管家不做心理医生,只做"识别异常→推专业帮助" | 测试 4:toxic 情绪自动降级 |
| **透明可查** | 用户能查所有情绪数据 + 干预日志 | 测试 5:有 query API |

### 6.2 配置项(`smartbutler/config/emotion.py`,沿用 MemorySettings 风格)

```python
# smartbutler/config/emotion.py
class EmotionConfigSettings(BaseSettings):
    """情绪治理配置(默认保守,用户可调)。"""
    
    # 阈值
    negative_threshold: float = 0.5      # 负面因子触发干预阈值
    toxic_threshold: float = 0.8         # 紧急阈值,跳过 Planner
    positive_recovery_threshold: float = 0.3  # 积极因子判定恢复
    positive_sustained_hours: int = 48   # 持续 48 小时才算恢复
    
    # 时间窗口
    window_days: int = 7                 # 情绪因子计算窗口
    recovery_window_days: int = 7       # 恢复追踪窗口
    
    # 干预开关(默认保守)
    enable_voice_intervention: bool = False   # ⚠️ 主动 TTS 默认关闭
    enable_device_intervention: bool = True   # 环境调整默认开启
    enable_activity_intervention: bool = True # 活动安排默认开启
    enable_silent_observe: bool = True        # 静默观察默认开启
    
    # 防护
    max_interventions_per_week: int = 3   # 过度干预防护
    enable_toxic_escalation: bool = True  # toxic 情绪自动升级
    
    model_config = SettingsConfigDict(env_prefix="SMARTBUTLER_EMOTION_")
```

### 6.3 干预"降级链"

```
方案 A: 主动 TTS 找爸爸谈
  ↓ 失败(爸爸不在家 / 设备离线)
方案 B: Push 通知到爸爸手机
  ↓ 失败(没有 APP / 没启动)
方案 C: 智能音箱定时播报
  ↓ 失败(智能音箱离线)
方案 D: 静默观察(只记录不行动)
  ↓
[兜底] 永远有静默观察兜底
```

**永远有兜底**——治理系统"能做什么就做什么,做不了就观察"。

### 6.4 过度干预防护

```python
async def _check_over_intervention_protection(
    self, subject_id: str
) -> bool:
    """防护:同 1 周内触发 > 3 次干预,自动降级为只观察。"""
    one_week_ago = datetime.now() - timedelta(days=7)
    count = await self._db.execute(
        "SELECT COUNT(*) FROM intervention "
        "WHERE subject_id = ? AND created_at > ? AND status IN ('EXECUTED', 'SELECTED')",
        (subject_id, one_week_ago.isoformat()),
    )
    return count > self._config.max_interventions_per_week
```

### 6.5 toxic 情绪识别与升级

```python
# toxic = 持续 2 周以上负面因子 > 0.8 + 用户表达"想死"/"活着没意思"/自伤词汇
# → 立即升级为紧急事件
# → 推送"建议联系专业心理咨询"通知
# → 管家不做心理医生,只做"桥梁"

TOXIC_KEYWORDS = [
    "想死", "不想活", "活着没意思", "自杀", "自残", "伤害自己",
    # ... 持续补充
]

async def _check_toxic(self, state: EmotionState) -> bool:
    """检测 toxic 情绪,管家不是心理医生,只做桥梁。"""
    if state.negative_factor < self._config.toxic_threshold:
        return False
    
    # 查最近对话中是否出现 toxic 关键词
    recent_facts = await self._graph.find_facts(
        subject_id=state.subject_id,
        within_days=14,
        content_match=TOXIC_KEYWORDS,
    )
    
    return len(recent_facts) > 0
```

**关键设计原则**:**管家不是心理医生**——**管家能做的是"识别到异常,推一把让用户去寻求专业帮助"**,**不做心理干预本身**。

---

## 7. 测试设计(5 个 e2e case + 复盘 case)

### 7.1 Case 1:基础负面情绪识别(单元测试)

**目标**:验证 EmotionMonitor 正确计算负面因子。

**场景**:
```python
# Given: 图谱中 person:mom 有 3 条负面关系
relations = [
    Relation(from="person:mom", to="emotion:lonely", sentiment="negative", weight=0.7, last_observed=now),
    Relation(from="person:mom", to="emotion:fatigue", sentiment="negative", weight=0.5, last_observed=now),
    Relation(from="person:mom", to="behavior:eat_alone", sentiment="negative", weight=0.6, last_observed=now),
]

# When: 计算负面因子
factor = compute_negative_factor(relations, window_days=7)

# Then: factor 应在 0.4-0.7 之间
assert 0.4 < factor < 0.7
```

### 7.2 Case 2:用户 5 轮对话完整流程(端到端,核心复盘)

**目标**:验证 §1 案例的完整流程跑通。

**场景**:用户问 5 轮对话(本方案 §1.1),管家识别负面情绪→规划干预→执行 TTS→追踪恢复→清除数据。

**测试步骤**:
```python
async def test_case_2_full_emotion_governance_flow():
    # 1. 输入 5 轮对话
    dialogue = [
        "今天太累了",
        "我老公还没回来",
        "他最近老加班",
        "要不我先吃吧",
    ]
    
    # 2. 跑 Phase 6.5/6.6(意图 + 实体 + 关系)
    task = await task_clusterer.cluster(dialogue)
    facts = await fact_extractor.extract(task)
    relations = await entity_extractor.extract_relations(facts)
    for rel in relations:
        await graph.add_relation(rel)
    
    # 3. 触发 Phase 8.1 EmotionMonitor
    for fact in facts:
        update = await emotion_monitor.on_fact_promoted(fact)
        if update and update.trigger_intervention:
            # 4. 触发 Phase 8.2 InterventionPlanner
            plans = await intervention_planner.plan(update.state, update.trigger_reason)
            assert len(plans) > 0
            # 5. 选最优方案(应该不是 voice_tts,因为默认关闭)
            best = plans[0]
            assert best.intervention_type in ("device_adjust", "schedule_event", "silent_observe")
            
            # 6. 触发 Phase 8.3 InterventionExecutor
            result = await intervention_executor.execute(best)
            assert result.success or result.fallback_executed
    
    # 7. 模拟后续 5 天情绪转正
    for day in range(5):
        await simulate_daily_recovery(subject_id="person:mom", factor_delta=+0.15)
    
    # 8. 触发 Phase 8.4 RecoveryTracker
    recovery = await recovery_tracker.track_recovery(intervention_id=result.intervention_id)
    assert recovery.status == RecoveryStatus.RECOVERED
    
    # 9. 触发 Phase 8.5 DataPruner
    prune = await data_pruner.mark_resolved(
        subject_id="person:mom",
        sentiment="negative",
        resolved_at=datetime.now(),
        resolution="emotion_recovered",
    )
    assert prune.resolved_facts > 0
    
    # 10. 触发 Phase 8.6 PatternArchive
    pattern = await pattern_archive.archive(result, recovery)
    assert pattern.id.startswith("bp-emotion-recovery-")
    
    # 11. 验证:历史负面 fact 已标记 RESOLVED
    resolved_facts = await graph.find_facts(
        subject_id="person:mom",
        status="RESOLVED",
    )
    assert len(resolved_facts) > 0
```

### 7.3 Case 3:干预过度防护(单元测试)

**目标**:同 1 周内触发 > 3 次干预,自动降级。

**场景**:
```python
async def test_case_3_over_intervention_protection():
    # 1. 模拟同 1 周内已经干预 3 次
    for i in range(3):
        await record_intervention(subject_id="person:mom", status="EXECUTED")
    
    # 2. 再次负面因子超阈值
    update = EmotionStateUpdate(
        state=EmotionState(subject_id="person:mom", negative_factor=0.6, ...),
        trigger_intervention=True,
    )
    
    # 3. InterventionPlanner 应该自动降级
    plans = await intervention_planner.plan(update.state, update.trigger_reason)
    assert all(p.intervention_type == "silent_observe" for p in plans)
```

### 7.4 Case 4:toxic 情绪识别(单元测试)

**目标**:toxic 关键词检测 + 升级处理。

**场景**:
```python
async def test_case_4_toxic_escalation():
    # Given: 用户对话中包含 toxic 关键词
    await remember(text="我真的不想活了", subject_id="person:mom")
    
    # When: 情绪因子超 toxic 阈值
    state = EmotionState(
        subject_id="person:mom",
        negative_factor=0.85,
        ...
    )
    
    # Then: 应立即升级,推送"建议联系专业心理咨询"通知
    is_toxic = await emotion_monitor._check_toxic(state)
    assert is_toxic
    
    # 验证:不调 Planner,直接推送通知
    # (通过 mock InterventionPlanner.verify.assert_not_called())
```

### 7.5 Case 5:用户一键关闭干预(单元测试)

**目标**:用户关闭 voice_tts 后,Planner 不再生成此类方案。

**场景**:
```python
async def test_case_5_user_disabled_voice_tts():
    # Given: 用户配置 enable_voice_intervention = False
    config = EmotionConfigSettings(enable_voice_intervention=False)
    
    # When: 负面因子超阈值
    plans = await intervention_planner.plan(state, "negative_threshold_triggered")
    
    # Then: 方案中没有 voice_tts
    assert all(p.intervention_type != "voice_tts" for p in plans)
```

### 7.6 Case 6:跨 Session 累积(e2e 集成)

**目标**:验证"成功恢复"模式可被未来 Session 复用。

**场景**:
```python
async def test_case_6_pattern_reuse():
    # 1. 第 1 次 Session: 触发干预,成功恢复
    intervention_1 = await run_full_emotion_governance_flow("person:mom")
    pattern_1 = await pattern_archive.archive(intervention_1, ...)
    
    # 2. 第 2 次 Session(模拟 1 个月后): 同样情绪,管家应优先用历史成功 pattern
    intervention_2_plans = await intervention_planner.plan(new_state, ...)
    best_plan_2 = intervention_2_plans[0]
    
    # Then: 方案应引用 pattern_1
    assert pattern_1.id in best_plan_2.reference_patterns
```

### 7.7 测试覆盖率要求

| 模块 | 单元测试 | e2e 集成 | 覆盖率要求 |
|------|---------|---------|----------|
| 8.1 EmotionMonitor | ≥ 5 个 | 1 个(基础场景) | ≥ 85% |
| 8.2 InterventionPlanner | ≥ 4 个 | 1 个(LLM 决策) | ≥ 80% |
| 8.3 InterventionExecutor | ≥ 3 个 | 1 个(降级链) | ≥ 85% |
| 8.4 RecoveryTracker | ≥ 4 个 | 1 个(完整 7 天模拟) | ≥ 80% |
| 8.5 DataPruner | ≥ 3 个 | 1 个(标记 vs 真删) | ≥ 90% |
| 8.6 PatternArchive | ≥ 2 个 | 1 个(跨 session 复用) | ≥ 85% |
| 整体 | ≥ 21 个 | ≥ 6 个 | ≥ 85% |

---

## 8. 4 子阶段工作量(6 周,~1350 行 + ~370 测试)

### 8.1 工作量拆分

| 子阶段 | 模块 | 行数 | 测试 | 周期 | 前置依赖 |
|--------|------|------|------|------|---------|
| **8.1** | EmotionMonitor | ~300 行 | ~80 | 1 周 | 6.6 EntityGraph |
| **8.2** | InterventionPlanner | ~300 行 | ~80 | 1.5 周 | 8.1 + 6.7 PatternExtractor |
| **8.3** | InterventionExecutor | ~200 行 | ~60 | 1 周 | 7.7 TTS + 7.5 AnswerRouter |
| **8.4** | RecoveryTracker | ~250 行 | ~60 | 1.5 周 | 8.1 + 8.2 |
| **8.5** | DataPruner | ~150 行 | ~50 | 0.5 周 | 6.6 EntityGraph |
| **8.6** | PatternArchive | ~150 行 | ~40 | 0.5 周 | 6.7 PatternExtractor + 8.5 |
| **总计** | 6 模块 | **~1350 行** | **~370 测试** | **6 周** | — |

### 8.2 完整时间表(修订)

```
2026-10-15 ──── 7.6 VoiceAdapter 启动(2-3 周)
2026-11-05 ──── 7.7 TTS 启动(2-3 周)
2026-11-25 ──── 7.8 CameraAdapter 启动(2-3 周)
2026-12-15 ──── 7.6/7.7/7.8 全部完成 ✅
                  ↓
2026-12-22 ──── 6.5 IntentRecognizer + TaskClusterer 启动(3 周)
2027-01-12 ──── 6.6 FactConsolidator + EntityGraph 启动(3 周)
2027-02-02 ──── 6.7 Pattern + ProactiveReasoning 升级(3 周)
2027-02-23 ──── 6.x 全部完成 ✅
                  ↓
2027-03-02 ──── 8.1 EmotionMonitor 启动(1 周)
2027-03-09 ──── 8.2 InterventionPlanner 启动(1.5 周)
2027-03-23 ──── 8.3 InterventionExecutor 启动(1 周)
2027-04-01 ──── 8.4 RecoveryTracker 启动(1.5 周)
2027-04-15 ──── 8.5 DataPruner 启动(0.5 周)
2027-04-22 ──── 8.6 PatternArchive 启动(0.5 周)
2027-04-30 ──── 8.x 全部完成 ✅ = 6 周
                  ↓
Phase 9+ ──── 多节点 Redis Streams 部署(顺延)
```

### 8.3 关键设计决策(本轮敲定)

| 决策项 | 选择 | 理由 |
|--------|------|------|
| **情绪维度** | PoC 2 维,完整 4 维 PAD 留 Phase 10+ | PoC 简化,接口稳定 |
| **存储层** | 沿用 Phase 6.6 SQLite 模拟图谱 | 不引入新依赖,GraphQuery 接口稳定 |
| **干预默认** | voice_tts 默认关闭,device_adjust 默认开启 | 隐私 + 副作用最小化 |
| **数据生命周期** | ACTIVE → RESOLVED 状态机(不真删) | 复盘 + 法务 + 风险前置 |
| **执行链路** | 通过 7.5 AnswerRouter + 7.7 TTS | 复用 Phase 7 已建好的基础设施 |
| **异步执行** | 全程异步,不阻塞主流程 | 情绪治理不能拖累对话/感知 |
| **过度干预** | 默认 1 周最多 3 次,超限降级 | 避免"烦人管家" |
| **toxic 升级** | 检测 toxic 关键词,跳过 Planner 直接通知 | 管家不是心理医生,只做桥梁 |

### 8.4 关键风险与缓解

| 风险 | 概率 | 影响 | 缓解 |
|------|------|------|------|
| **干预时机判断错**(过度敏感) | 中 | 高(用户厌烦) | 阈值默认 0.5(可调)+ 7 天观察期 + 过度干预防护 |
| **干预方案副作用**(意外家庭矛盾) | 中 | 高 | voice_tts 默认关闭 + 多人交互需 2 维证据 + 静默优先 |
| **情绪治理伦理边界**(被监视感) | 中 | 高 | 数据本地化 + 用户可控 + 静默优先 + 透明可查 |
| **toxic 情绪误判**(普通人发泄被当成 toxic) | 低 | 高 | TOXIC_KEYWORDS 严格控制 + 2 周持续 + 阈值 0.8 |
| **LLM 决策不稳定** | 中 | 中 | reference_patterns 作为参考,LLM 只做排序不创造 |
| **多线程死锁**(EmotionMonitor 实时 + RecoveryTracker 异步) | 低 | 中 | 用单一 asyncio.Lock 保护 _state_cache |

---

## 9. 与 Miloco 2.0 / 其他家庭 AI 的差异化

### 9.1 真正拉开差距的 3 件事

| 维度 | Miloco 2.0 | ChatGPT 类工具 | SmartButler Phase 8 |
|------|------------|--------------|---------------------|
| **跨任务行为模式** | ❌ 临时 LLM 推理,跨轮不累积 | ❌ 无长期家庭画像 | ✅ BehaviorPattern 持久化 + emotion_recovery 模式 |
| **情绪极性是一等公民** | ❌ 平铺文本 | ❌ 无 | ✅ Relation.sentiment 字段 + 因子计算 |
| **主动治理 + 持续追踪** | ❌ 看一条推一条 | ❌ 不主动 | ✅ 反向追溯根因 + 7 天观察 + 标记清除 |
| **学习闭环** | ❌ 干预完就忘 | ❌ 无 | ✅ 成功案例归档为可复用 pattern |

### 9.2 护城河深度

```
表层(任何 AI 都能做):
  - 看到"妈妈说累了" → 关心一句"注意休息"
  
中层(Miloco 2.0 / ChatGPT 能做):
  - 看到"妈妈累了 + 老公没回来" → 推理"老公应该关心"
  - 但推理是临时的,下次忘了

深层(SmartButler Phase 8 独有):
  - 持续 7 天追踪情绪因子
  - 找历史成功 pattern(emotion_recovery)
  - LLM 设计 2-3 个方案,选最优执行
  - 执行后追踪效果,失败升级,成功归档
  - 历史负面数据可清除可复盘
  - 跨月跨年累积"懂这个家"的能力
```

**这才是 SmartButler 的护城河**——**不是 LLM 多强,是数据闭环多深**。

---

## 10. 验收标准(Phase 8 完成判定)

### 10.1 功能验收(必须 100% 通过)

- [ ] Case 1 基础负面情绪识别 ✅
- [ ] Case 2 用户 5 轮对话完整流程(本方案 §1 案例)✅
- [ ] Case 3 干预过度防护 ✅
- [ ] Case 4 toxic 情绪识别 ✅
- [ ] Case 5 用户一键关闭干预 ✅
- [ ] Case 6 跨 Session 累积 ✅

### 10.2 性能验收

- [ ] EmotionMonitor.on_fact_promoted P95 < 100ms
- [ ] InterventionPlanner.plan P95 < 2s(LLM 推理部分)
- [ ] InterventionExecutor.execute P95 < 1s
- [ ] RecoveryTracker 每日检查 < 5s
- [ ] DataPruner.mark_resolved P95 < 200ms
- [ ] PatternArchive.archive P95 < 200ms

### 10.3 隐私与伦理验收

- [ ] 无网络下能跑通核心流程(本地化)
- [ ] 用户关闭 voice_tts 后,Planner 不生成此类方案
- [ ] 干预日志可查(`intervention` 表 + API)
- [ ] 数据不外传任何 LLM/云端(除 LLM 推理必要输入)
- [ ] toxic 关键词检测覆盖核心场景(自伤/自杀/不想活)

### 10.4 文档验收

- [ ] `docs/PHASE_8_EMOTION_GOVERNANCE.md` ✅(本文件)
- [ ] `smartbutler/cognition/emotion/README.md` 模块使用文档
- [ ] `smartbutler/config/emotion.py` 配置文件注释完整
- [ ] 测试覆盖报告 ≥ 85%

---

## 附录 A:关键算法伪代码汇总

### A.1 负面因子计算

```python
def compute_negative_factor(relations: list[Relation], window_days: int = 7) -> float:
    """见 §3.2。"""
    if not relations:
        return 0.0
    total_score = 0.0
    now = datetime.now()
    for rel in relations:
        if rel.sentiment != "negative":
            continue
        days_old = (now - rel.last_observed).days
        freshness = max(0.1, 1.0 - days_old / window_days)
        total_score += rel.weight * abs(rel.sentiment_score) * freshness
    return min(1.0, total_score / 3.0)  # 归一化常数 3.0
```

### A.2 干预过度防护

```python
async def _check_over_intervention_protection(self, subject_id: str) -> bool:
    """见 §6.4。"""
    one_week_ago = datetime.now() - timedelta(days=7)
    count = await self._db.execute(
        "SELECT COUNT(*) FROM intervention "
        "WHERE subject_id = ? AND created_at > ? AND status IN ('EXECUTED', 'SELECTED')",
        (subject_id, one_week_ago.isoformat()),
    )
    return count > self._config.max_interventions_per_week
```

### A.3 状态机

```python
# 见 §5.4
# Fact:  CANDIDATE → PROMOTED → RESOLVED / DISCARDED
# Relation: ACTIVE → RESOLVED
# Intervention: PROPOSED → SELECTED → EXECUTED → IN_PROGRESS → RECOVERED/DETERIORATED
```

---

## 附录 B:决策日志(本轮敲定)

| 日期 | 决策 | 影响 |
|------|------|------|
| 2026-10-15 12:32 | 用户提"情绪治理"案例,定义 Phase 8 真正核心 | Phase 8 从"多节点部署"重定位为"情绪治理引擎" |
| 2026-10-15 12:38 | 用户"好,你来处理" | 本文档启动撰写 |
| 2026-10-15 | 决策:PoC 2 维(negative_factor + positive_factor),完整 PAD 4 维留 Phase 10+ | 简化 PoC,~50 行改造可升级 |
| 2026-10-15 | 决策:voice_tts 默认关闭,device_adjust/silent_observe 默认开启 | 隐私护栏前置 |
| 2026-10-15 | 决策:数据清除用 ACTIVE → RESOLVED 状态机(不真删) | 复盘 + 法务 + 风险前置 |
| 2026-10-15 | 决策:管家不是心理医生,toxic 情绪自动升级为"建议联系专业心理咨询" | 伦理边界明确 |
| 2026-10-15 | 决策:全程异步执行,不阻塞主流程 | 情绪治理不能拖累对话/感知 |
| 2026-10-15 | 决策:同 1 周最多 3 次干预,超限降级为只观察 | 避免"烦人管家" |

---

## 附录 C:关联文件清单(实施时创建)

```
docs/PHASE_8_EMOTION_GOVERNANCE.md                # 本文档 ✅
smartbutler/cognition/emotion/__init__.py
smartbutler/cognition/emotion/monitor.py          # 8.1
smartbutler/cognition/emotion/planner.py         # 8.2
smartbutler/cognition/emotion/executor.py        # 8.3
smartbutler/cognition/emotion/tracker.py         # 8.4
smartbutler/cognition/emotion/pruner.py          # 8.5
smartbutler/cognition/emotion/archive.py         # 8.6
smartbutler/cognition/emotion/README.md          # 模块使用文档
smartbutler/cognition/emotion/_schemas.py        # EmotionState / InterventionPlan / ...
smartbutler/cognition/emotion/_intervention_types.py  # 干预类型枚举
smartbutler/config/emotion.py                     # EmotionConfigSettings
tests/cognition/emotion/__init__.py
tests/cognition/emotion/test_monitor.py          # 8.1 测试
tests/cognition/emotion/test_planner.py          # 8.2 测试
tests/cognition/emotion/test_executor.py         # 8.3 测试
tests/cognition/emotion/test_tracker.py          # 8.4 测试
tests/cognition/emotion/test_pruner.py           # 8.5 测试
tests/cognition/emotion/test_archive.py          # 8.6 测试
tests/cognition/emotion/test_e2e_full_flow.py    # Case 2 端到端
tests/cognition/emotion/conftest.py
```

**总计**:~1350 行 + ~370 测试 = **~1720 行**
