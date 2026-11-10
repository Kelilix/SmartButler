# COMMON_SCRIPTS — SmartButler 日常常用命令速查

> **目标**:把"启动语音服务验证 → 翻记忆库 → 清理"这一条最常用工作流,所有命令汇总在一处,
> 复制即用。每个命令都标了**前置依赖**(是否要先关语音服务 / 是否走真 Qdrant 等)。
>
> 适用范围:`D:\soft\0project\test\SmartButler`(本仓库根目录)。所有命令**默认在项目根目录执行**(`cd D:\soft\0project\test\SmartButler`)。
>
> ⚠️ **2026-11-09 更新**:`scripts/dev_chat.py` 已从仓库移除(dev_chat 是开发期 REPL,
> 生产入口是 `smartbutler.scripts.voice_service`)。本文档已全部改成 voice_service 的命令。

---

## 0. 一句话总结(最常用 3 条)

```bash
# 1) 启动语音服务(真硬件 + 真 LLM)
python -m smartbutler.scripts.voice_service --no-fake

# 2) 看完库里有啥
python -m smartbutler.scripts.memory_cli dump --all

# 3) 软删有多少该清的
python -m smartbutler.scripts.memory_cli age-summary
```

**清理(全套清空,含图):**

```bash
python -m smartbutler.scripts.memory_cli clear-all --include-graph \
    --confirm yes --yes-i-know-this-deletes-everything --no-dry-run
```

下面所有命令都是这 3 条的展开。

---

## 1. voice_service 启动与验证

> **唯一生产入口**。`scripts/dev_chat.py` 已移除(dev_chat 是开发期 REPL,不再维护)。

### 1.1 最小启动(真硬件)

```bash
cd D:\soft\0project\test\SmartButler
python -m smartbutler.scripts.voice_service --no-fake
```

| 参数 | 默认 | 说明 |
|------|------|------|
| `--no-fake` | 关 | **必须加** —— 不加则用 Fake 组件(麦克风/ASR/TTS 都是假的) |
| `--device-id` | `speaker_01` | 设备 ID |
| `--user-id` | `gaotianyu` | 默认用户 ID(声纹未识别时的兜底) |
| `--tts` | `edge` | TTS 实现(`edge` / `piper`) |
| `--embedder` | `resemblyzer` | 声纹实现(`resemblyzer` / `ecapa`) |
| `--wake-word` | `hey_butler` | 唤醒词 |
| `--energy-threshold` | `0.0` | 唤醒能量阈值(0-1,0=任何声音都触发) |
| `--vad-threshold` | env | VAD 语音概率阈值(默认 0.02,**不要低于 0.01**) |
| `--silence-timeout-ms` | env | 静音超时(默认 1500) |
| `--max-segment-ms` | env | 单段最大时长(默认 30000) |
| `--stale-window-seconds` | env | 代次过期宽松窗口(默认 0=严格模式,见 §1.6) |
| `--device` | 系统默认 | 音频输入设备 ID |
| `--list-devices` | - | 列出所有音频设备后退出 |

### 1.2 开发模式(Fake 组件,不碰硬件)

```bash
python -m smartbutler.scripts.voice_service
```

麦克风/ASR/TTS/声纹全是 Fake —— 只用来验证链路能不能跑通,不产生真实语音。

### 1.3 查音频设备

```bash
python -m smartbutler.scripts.voice_service --list-devices
```

### 1.4 验证流程(端到端)

启动 voice_service 后,在**另一个终端**跑 memory_cli 验证持久化:

```bash
# 1) 启动语音服务(必须先启,否则库里没数据)
python -m smartbutler.scripts.voice_service --no-fake

# 2) 另一个终端 —— 看长期库
python -m smartbutler.scripts.memory_cli dump --all

# 3) 跑语义检索
python -m smartbutler.scripts.memory_cli recall "上次聊的事" --k 5

# 4) 看人物关系图
python -m smartbutler.scripts.memory_cli graph-stats
python -m smartbutler.scripts.memory_cli graph-dump

# 5) 退出语音服务(必须先退!),再清软删
python -m smartbutler.scripts.memory_cli age-summary
python -m smartbutler.scripts.memory_cli vacuum --dry-run
```

### 1.5 启动失败排查

| 症状 | 原因 | 解决 |
|------|------|------|
| `AlreadyLocked` / `PermissionError(WinError 32)` / `another instance` | 语音服务还活着,锁住 `data/qdrant/` 目录 | **关掉语音服务再跑 CLI**;CLI 有兜底:截断 `storage.sqlite` 让下次启动自动重头初始化 |
| `Qdrant dimension mismatch` | `.env` 里 `vector_size` 跟老 collection 不一致 | 看 §4 "改向量维度" 走 `clear-all --include-task-index` 重头 |
| 说话没反应 / 一直等 | VAD 阈值太低,把静音当语音 | 调高 `SMARTBUTLER_VOICE_VAD_THRESHOLD`(别低于 0.01) |
| 回复很碎 / 一次说半句 | 静默超时太短 | 调大 `SMARTBUTLER_VOICE_SILENCE_TIMEOUT_MS` |
| 管家说话不像"英国贵族管家" | Personality 没注入 orchestrator | 已修(2026-11-09)。看启动日志有没有 `personality.loaded name=杰贝兹`,见 §2.5 |

### 1.6 打断策略(L1/L2 代次过期)

**背景**:用户连续说话时,管家会把旧回复陆续播出来(L1 队列积压 + L2 播过期回复)。
已通过**代次号(generation)** 机制解决:每次发言分配一个严格单调递增的
**毫秒时间戳**,下游用它判断"我这条是不是已经过时了"。

| 层 | 位置 | 行为 |
|---|---|---|
| **L1** | `InMemoryEventBus` 队列层 | 队列里的过期事件直接丢弃,**不进 LLM**(省算力) |
| **L2** | `TTSOutputAdapter` 播放层 | LLM 算完后若已过期,**跳过 TTS 合成**(省时省钱,不播) |

**两种过期模式**(`--stale-window-seconds` / `SMARTBUTLER_VOICE_STALE_WINDOW_SECONDS`):

| 模式 | 值 | 语义 | 适合 |
|---|---|---|---|
| **严格** | `0`(默认) | 用户说了**任何**新话 → 旧回复不播 | 用户明确换话题,想立刻听到新回答 |
| **宽松** | `>0`(秒) | 只有"**最近该窗口内**"说了新话 → 旧回复才作废 | 用户会随口说"嗯/对",不想让这些把回复作废 |

**宽松模式的关键语义**(2026-11-10 修):

判定看的是「**最新那次更新距今多久**」,而不是「两个代次间隔了多久」。

```
T=0   用户:"帮我查天气"   → gen1
T=5   用户:"嗯"            → gen2
T=100 LLM 算完 gen1

- 若按"间隔"判(5s ≤ window) → gen1 过期 ❌ (白等 100 秒啥也没听到)
- 按"距今"判(95s > window)  → gen1 不过期 ✅
```

**怎么调**:

```bash
# 严格模式(默认)
python -m smartbutler.scripts.voice_service --no-fake

# 宽松模式(3 秒窗口):随口说"嗯"不打断,真正换话题才打断
python -m smartbutler.scripts.voice_service --no-fake --stale-window-seconds 3

# 或者写进 .env
# SMARTBUTLER_VOICE_STALE_WINDOW_SECONDS=3
```

**怎么确认生效**:启动日志会打印

```
代次追踪器: stale_window=0.0s (严格模式)
```

**⚠️ 已知局限**:L1/L2 只管"管家播报之间"。管家**正在说话时**插嘴,
当前这句会**播完**(播报已开始,L2 只在播报**前**检查)。要真正截断正在播的音频,
需要后续的 waveOut 打断(L3)。

---

## 2. memory_cli — 翻库 / 检索(只读)

**入口**:`python -m smartbutler.scripts.memory_cli <cmd>`

**前置**:语音服务可以**同时**跑(只读不锁),但写操作(vacuum / purge / clear-all)要**先关语音服务**。

### 2.1 长期记忆(Qdrant)

| 子命令 | 作用 | 典型命令 |
|--------|------|---------|
| `dump` | **全量翻页看库里到底存了什么** | `python -m smartbutler.scripts.memory_cli dump --all` |
| `dump` | 只看某个 level | `python -m smartbutler.scripts.memory_cli dump --level daily --all` |
| `dump` | 按 tag 过滤 | `python -m smartbutler.scripts.memory_cli dump --tag "user:gaotianyu" --all` |
| `dump-fields` | payload 字段分布体检(level / tag / importance 分布) | `python -m smartbutler.scripts.memory_cli dump-fields` |
| `recall <query>` | 语义检索(facade.recall 包装) | `python -m smartbutler.scripts.memory_cli recall "宠物" --k 5` |
| `recall` | 按 source 过滤 | `python -m smartbutler.scripts.memory_cli recall "宠物" --source gaotianyu` |
| `recall` | 调相似度门槛(默认 0.55) | `python -m smartbutler.scripts.memory_cli recall "宠物" --min-score 0.3` |
| `list-deleted` | 列出软删除的事实 | `python -m smartbutler.scripts.memory_cli list-deleted --limit 20` |
| `list-deleted --by-age` | 按软删除时长分桶 | `python -m smartbutler.scripts.memory_cli list-deleted --by-age` |
| `age-summary` | **快查**:分桶统计("该清的还有多少") | `python -m smartbutler.scripts.memory_cli age-summary` |
| `stats` | 容量统计(active/deleted/archived + 磁盘占用) | `python -m smartbutler.scripts.memory_cli stats` |

### 2.2 短期记忆(LangGraph SQLite,`short_term.db`)

| 子命令 | 作用 | 典型命令 |
|--------|------|---------|
| `stm-threads` | 列所有 thread + checkpoint 数 | `python -m smartbutler.scripts.memory_cli stm-threads` |
| `stm-threads --detail` | 加上 checkpoint_id 范围 | `python -m smartbutler.scripts.memory_cli stm-threads --detail` |
| `stm-dump` | 全量翻对话消息流(按 checkpoint) | `python -m smartbutler.scripts.memory_cli stm-dump --limit 5` |
| `stm-dump` | 只看某个 thread | `python -m smartbutler.scripts.memory_cli stm-dump --thread <thread_id>` |
| `stm-dump -v` | 加上 state 字段名 | `python -m smartbutler.scripts.memory_cli stm-dump -v --limit 3` |
| `stm-stats` | 短期库容量体检 | `python -m smartbutler.scripts.memory_cli stm-stats` |
| `st-raw <prefix>` | 打印某个 checkpoint 的原始 state JSON(深排查) | `python -m smartbutler.scripts.memory_cli st-raw 0c12345 -v` |

### 2.3 记忆图(Phase 9 人物关系图,`data/memory/graph.db`)

> 图是独立子系统,读图不锁语音服务。

| 子命令 | 作用 | 典型命令 |
|--------|------|---------|
| `graph-stats` | 图规模体检 | `python -m smartbutler.scripts.memory_cli graph-stats` |
| `graph-dump` | 翻出节点与关系边 | `python -m smartbutler.scripts.memory_cli graph-dump` |
| `graph-dump --all` | 连已失效的边一起显示 | `python -m smartbutler.scripts.memory_cli graph-dump --all` |

### 2.4 记忆图的开关(`.env`)
图默认**关闭**(`graph_enabled=False`)。开启方式:

```bash
# .env 里加一行
SMARTBUTLER_MEMORY_GRAPH_ENABLED=true
```

| 环境变量 | 默认值 | 说明 |
|---------|-------|------|
| `SMARTBUTLER_MEMORY_GRAPH_ENABLED` | `false` | 总开关。开启后:task 完成时 LLM 抽人物关系入库 + 召回注入 + 注册 `resolve_person` tool;关闭时**零副作用**(不建表、不抽取、不召回,tool 对 LLM 不可见) |
| `SMARTBUTLER_MEMORY_GRAPH_EDGE_MAX_PER_TASK` | `20` | 单 task 抽边上限(防边数爆炸) |
| `SMARTBUTLER_MEMORY_GRAPH_DEDUPE_THRESHOLD` | `0.85` | 慢路径(向量)判重阈值 |
| `SMARTBUTLER_MEMORY_GRAPH_RECALL_MAX_EDGES` | `10` | 召回时注入 prompt 的图边条数上限 |
| `SMARTBUTLER_MEMORY_GRAPH_DB_PATH` | `graph.db` | 图库路径(相对路径基准 = `data/memory/`,见 §3.5) |

开启后建议先观察日志里的 `graph_extract.summary` / `graph_writer.written` 两行,确认 LLM 抽边质量。

### 2.5 Personality(管家形象/人设)

人设配置文件 = 仓库根的 **`personality.md`**(YAML frontmatter,body 是自由注释)。

默认人设是 **杰贝兹(Jeeves)** —— 英国伍斯特郡管家世家出身的资深总管家。

**加载顺序**(找到即停):

1. `<cwd>/personality.md`
2. `~/.smartbutler/personality.md`
3. 都找不到 → 用内存默认值(也是杰贝兹)

**怎么改**:直接编辑 `personality.md` 的 YAML 部分:

| 字段 | 含义 |
|------|------|
| `name` | 管家称呼(如"杰贝兹") |
| `identity` | 人设文本,回答"我是谁",注入 system prompt 最前方(推荐 2-5 句) |
| `formality` | 0=口语化,1=正式用敬语(默认 0.85) |
| `humor` | 0=严肃,1=爱开玩笑(默认 0.30) |
| `verbosity` | 0=一句话答完,1=长篇大论(默认 0.55) |
| `warmth` | 0=公事公办,1=热情体贴(默认 0.75) |
| `proactiveness` | 0=只答所问,1=主动建议(默认 0.65) |
| `guidelines` | 行为准则列表(独立于 trait) |

**怎么确认人设生效**:

```bash
# 看启动日志有没有这一行(personality.loaded)
python -m smartbutler.scripts.voice_service --no-fake 2>&1 | Select-String "personality"
```

应输出类似 `personality.loaded name=杰贝兹 traits=formality,humor,...`。

> ⚠️ **已修的坑(2026-11-09)**:之前入口脚本构造 orchestrator 时没传 `personality`,
> 导致 `personality_block` 为空、回落内置的通用人设("你是 SmartButler 管家…"),
> **用户配的人设完全不生效**。现在统一走 `emotion/personality/bootstrap.py::build_orchestrator`。

---

## 3. memory_cli — 清理 / 升格(写操作,需先关语音服务)

> ⚠️ **所有写操作前必做**:
> 1. **关掉语音服务**(否则 Qdrant 锁冲突,CLI 走文件级兜底,可能丢点)
> 2. **先跑 `--dry-run`**(默认就是 dry-run,但显式写出来更稳)
> 3. **加 `--confirm yes`**(必须,否则直接 exit 2)

### 3.1 升格(consolidation,3 层聚合)

把某时间窗的 fact 聚合成 1 条摘要(daily → weekly → monthly)。**不删除源 fact**。

```bash
# 1) dry-run 看看候选数 + 预计压缩比
python -m smartbutler.scripts.memory_cli consolidate --level daily --dry-run

# 2) 真跑(默认 level=daily)
python -m smartbutler.scripts.memory_cli consolidate --level daily

# 3) 周/月升格
python -m smartbutler.scripts.memory_cli consolidate --level weekly
python -m smartbutler.scripts.memory_cli consolidate --level monthly

# 4) 强制不调 LLM(对比效果 / 省 token)
python -m smartbutler.scripts.memory_cli consolidate --level daily --no-llm --dry-run
```

### 3.2 vacuum(双时间窗:7+30 天)

把软删除 >7 天的移到 archived SQLite(还能恢复 30 天),>30 天的物理删。

```bash
# 1) dry-run(默认就是)
python -m smartbutler.scripts.memory_cli vacuum

# 2) 看 age-summary 决策("该清的还有多少")
python -m smartbutler.scripts.memory_cli age-summary

# 3) 真跑(确认后)
python -m smartbutler.scripts.memory_cli vacuum --no-dry-run
```

### 3.3 purge — 单条 / 批量(危险)

```bash
# 单条:立即物理删某条(必须 --confirm yes)
python -m smartbutler.scripts.memory_cli purge --key "mem:gaotianyu:abc123" --confirm yes

# 批量按软删除时长:Qdrant 主体里 deleted > N 天的全清(N 必须 >= 7)
python -m smartbutler.scripts.memory_cli purge-before --older-than-days 30 --dry-run
python -m smartbutler.scripts.memory_cli purge-before --older-than-days 30 --confirm yes --no-dry-run

# 批量按 archived 时长:SQLite 备份里 > N 天的全清(默认 30)
python -m smartbutler.scripts.memory_cli purge-archived --older-than-days 90 --dry-run
python -m smartbutler.scripts.memory_cli purge-archived --older-than-days 90 --confirm yes --no-dry-run
```

### 3.4 clear-all — 一键清空所有长短期记忆(终极危险)

> 三重安全检查:`--confirm yes` + `--yes-i-know-this-deletes-everything` + 默认 `--dry-run`。

```bash
# 1) dry-run:看会清什么
python -m smartbutler.scripts.memory_cli clear-all --confirm yes

# 2) 真跑:清长短期(不动 task_index.db)
python -m smartbutler.scripts.memory_cli clear-all --confirm yes --yes-i-know-this-deletes-everything --no-dry-run

# 3) 含 task_index.db(6.5 task scheduler 状态)也一并清
python -m smartbutler.scripts.memory_cli clear-all --confirm yes --yes-i-know-this-deletes-everything --no-dry-run --include-task-index

# 4) 含记忆图(Phase 9 人物关系图)也一并清
python -m smartbutler.scripts.memory_cli clear-all --confirm yes --yes-i-know-this-deletes-everything --no-dry-run --include-graph
```

### 3.5 记忆图(Phase 9 人物关系图)— 只清图,不动记忆

图是**独立子系统**(独立 SQLite 文件 `data/memory/graph.db`),所以 `clear-all` 默认**不动**它。

| 子命令 | 作用 | 典型命令 |
|--------|------|---------|
| `graph-stats` | 图规模体检(节点/边/活跃边/声纹绑定数) | `python -m smartbutler.scripts.memory_cli graph-stats` |
| `graph-dump` | 翻出节点与关系边(看存了什么关系) | `python -m smartbutler.scripts.memory_cli graph-dump` |
| `graph-dump --all` | 连已失效的边一起显示 | `python -m smartbutler.scripts.memory_cli graph-dump --all --limit 200` |
| `graph-clear` | **只清图**,长短期记忆全保留 | `python -m smartbutler.scripts.memory_cli graph-clear --confirm yes --no-dry-run` |

```bash
# 1) 先看看图里有什么
python -m smartbutler.scripts.memory_cli graph-stats
python -m smartbutler.scripts.memory_cli graph-dump

# 2) dry-run(默认就是,只打印会清什么)
python -m smartbutler.scripts.memory_cli graph-clear --confirm yes

# 3) 真跑(只清图)
python -m smartbutler.scripts.memory_cli graph-clear --confirm yes --no-dry-run
```

> ⚠️ **清图会连节点上的声纹绑定一起丢**(`user_id` / `voice_bound`)。
> 声纹库 `data/speakers.db` **本身不受影响** —— 重新采集声纹后可再次绑定。

图路径解析(相对路径基准 = 运行时数据目录 `data/memory/`):

| 配置值 | 实际路径 |
|--------|---------|
| `graph.db`(默认) | `data/memory/graph.db` |
| `data/graph.db` | `<项目根>/data/graph.db` |
| 绝对路径 | 原样使用 |

---

## 4. 改向量维度 / 重头开始(常见迁移场景)

`vector_size` 跟老 collection 不一致时,只能**删 collection 重建**(Qdrant 不支持改维度)。

```bash
# 1) 关掉语音服务
# 2) 备份(可选)
#    PowerShell:
Copy-Item -Recurse data\qdrant data\qdrant.bak.20261014
#    cmd:
copy data\qdrant data\qdrant.bak.20261014

# 3) clear-all(连同 short_term.db / archive.db 一起清)
python -m smartbutler.scripts.memory_cli clear-all --confirm yes --yes-i-know-this-deletes-everything --no-dry-run --include-task-index

# 4) 改 .env 的 vector_size
#    SMARTBUTLER_STORAGE_QDRANT_VECTOR_SIZE=1024  ->  768

# 5) 启动语音服务,会按新 vector_size 重建 collection
python -m smartbutler.scripts.voice_service --no-fake
```

---

## 5. 排查"为什么这个没召回" / "为什么这个被记成 soft-deleted"

### 5.1 召回问题(命不中 / 命太多 / 命错 source)

```bash
# 1) 直接用 recall 复现(绕开语音服务,排除前端干扰)
python -m smartbutler.scripts.memory_cli recall "原 query" --k 10

# 2) 调 min-score 看是不是门槛把"正确但弱相关"的也挡了
python -m smartbutler.scripts.memory_cli recall "原 query" --k 10 --min-score 0.0

# 3) 看库里实际有哪些
python -m smartbutler.scripts.memory_cli dump --all --width 300

# 4) 按 source 拆开看
python -m smartbutler.scripts.memory_cli dump --tag "source:gaotianyu" --all
```

### 5.2 软删 / 恢复(被自动 TTL / conflict 删了)

```bash
# 1) 看哪些被软删
python -m smartbutler.scripts.memory_cli list-deleted --limit 50

# 2) 看删除原因
python -m smartbutler.scripts.memory_cli list-deleted --limit 50
#     → 看 reason 字段:ttl / conflict / promote / user_request / key

# 3) 恢复(走 facade 内部 API,目前 CLI 不直提供,临时方案:写脚本调 MemoryFacade.restore(key))
#    详见 docs/PHASE_6.2-6.5_TECHNICAL_DESIGN.md §2.5.1
```

---

## 6. 一页速查卡(打印贴墙)

```
┌──────────────────────────────────────────────────────────────┐
│                   voice_service 启动                        │
├──────────────────────────────────────────────────────────────┤
│ 真硬件   python -m smartbutler.scripts.voice_service         │
│              --no-fake                                       │
│ Fake     python -m smartbutler.scripts.voice_service         │
│ 查设备   python -m smartbutler.scripts.voice_service         │
│              --list-devices                                  │
├──────────────────────────────────────────────────────────────┤
│                     memory_cli 只读                            │
├──────────────────────────────────────────────────────────────┤
│ 看长库    python -m smartbutler.scripts.memory_cli dump --all  │
│ 体检      python -m smartbutler.scripts.memory_cli dump-fields │
│ 检索      python -m smartbutler.scripts.memory_cli recall "x" │
│ 看短库    python -m smartbutler.scripts.memory_cli stm-dump    │
│ 短库容量  python -m smartbutler.scripts.memory_cli stm-stats   │
│ 容量统计  python -m smartbutler.scripts.memory_cli stats       │
│ 该清的    python -m smartbutler.scripts.memory_cli age-summary │
│ 看记忆图  python -m smartbutler.scripts.memory_cli graph-stats │
│ 翻关系边  python -m smartbutler.scripts.memory_cli graph-dump  │
├──────────────────────────────────────────────────────────────┤
│                   memory_cli 写操作(先关语音服务)             │
├──────────────────────────────────────────────────────────────┤
│ 升格日报  python -m smartbutler.scripts.memory_cli consolidate │
│ vacuum   python -m smartbutler.scripts.memory_cli vacuum      │
│ 删 1 条   python -m smartbutler.scripts.memory_cli purge --key │
│              X --confirm yes                                   │
│ 批量删   ... purge-before --older-than-days 30 --confirm yes  │
│ 一键清空 ... clear-all --confirm yes --yes-i-know-this-       │
│              deletes-everything --no-dry-run                   │
│ 只清图   ... graph-clear --confirm yes --no-dry-run           │
└──────────────────────────────────────────────────────────────┘
```

---

## 7. 常见坑(踩过的)

| 坑 | 原因 | 规避 |
|---|------|------|
| 写操作报 `another instance` | 语音服务锁着 `data/qdrant` | **先关语音服务** |
| `dump` 看到旧数据,以为 `clear-all` 没生效 | 嵌入式 Qdrant 删 collection 只清内存,`storage.sqlite` 残留 | 7.4 已加物理兜底:truncate `storage.sqlite` + 删 `.lock` / `meta.json`;**关掉语音服务** 走完 |
| `recall` 啥都召回不出来 | `min-score=0.55` 把弱相关全挡了 | 加 `--min-score 0.0` 看真基线 |
| 语音服务不响应 / 一直等 | `.env` 没配 `SMARTBUTLER_LLM_*` | 配 `OPENAI_API_KEY` / `OPENAI_BASE_URL` / `OPENAI_MODEL` |
| 说话没反应、回复很碎 | VAD 阈值太低(把静音当语音)/ 静默超时太短 | 调 `SMARTBUTLER_VOICE_VAD_THRESHOLD`(≥0.01)、`SMARTBUTLER_VOICE_SILENCE_TIMEOUT_MS` |
| 短期库 stm-dump 显示"0 含 messages 的 checkpoint" | 反序列化失败(LangChain 对象 vs dict 判错) | 7.4 已修(走 `model_dump()` 降级);如仍 0,试 `--all-checkpoints` |
| 改了 `vector_size` 后报 dimension mismatch | Qdrant collection 不能改维度 | 走 §4 "改向量维度" 重建 |
| `clear-all` 跑完图还在 | 图是**独立子系统**(`data/memory/graph.db`),默认不动 | 加 `--include-graph`,或用 `graph-clear` 只清图 |
| `graph-clear` 后管家认不出人了 | 清图连**声纹绑定**(`user_id`)一起丢 | 声纹库没坏;重新采集声纹 → `resolve_person` 会重新绑定 |
| 找不到图库 / 图库路径对不上 | 相对路径基准是运行时数据目录 `data/memory/` | 用 `graph-stats` 看 CLI 解析出的实际路径;配置见 §3.5 |
| **管家说话不像"英国贵族管家"** | Personality **没注入** orchestrator → 回落内置通用人设 | 已修(`bootstrap.build_orchestrator`)。自查:启动日志应出现 `personality.loaded name=杰贝兹`;见 §2.5 |
| 插一句嘴,等了半天**啥也没听到** | 严格模式下"任何新话"都作废旧回复 | 用户随口说的"嗯/对"也算新话 → 调 `--stale-window-seconds 3` 改宽松模式;见 §1.6 |

---

## 8. 详细文档

- **架构 / 设计**:`docs/PHASE_6.2-6.5_TECHNICAL_DESIGN.md` §2.5.1(含 §2.5.1.16 vacuum 治理)
- **记忆图设计**:`docs/PHASE_9_GRAPH_MEMORY_DESIGN.md`(§6 存储 / §8 声纹绑定 / §15 运维清理)
- **记忆图路线图**:`docs/PHASE_9_GRAPH_MEMORY_ROADMAP.md`
- **CLI 源码**:`smartbutler/scripts/memory_cli.py`(顶部 docstring 列出所有子命令)
- **语音服务源码**:`smartbutler/scripts/voice_service.py`(顶部 docstring 有架构图 + 启动方式)
- **人设配置**:`personality.md`(仓库根,YAML frontmatter)
- **7.4 实施**:`docs/PHASE_7_TECHNICAL_DESIGN.md` §3 + §12.6
- **变更历史**:`README.md` §10 变更记录
