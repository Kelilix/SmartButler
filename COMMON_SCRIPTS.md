# COMMON_SCRIPTS — SmartButler 日常常用命令速查

> **目标**:把"启动 dev_chat 验证 → 翻记忆库 → 清理"这一条最常用工作流,所有命令汇总在一处,
> 复制即用。每个命令都标了**前置依赖**(是否要先关 dev_chat / 是否走真 Qdrant 等)。
>
> 适用范围:`D:\soft\0project\test\SmartButler`(本仓库根目录)。所有命令**默认在项目根目录执行**(`cd D:\soft\0project\test\SmartButler`)。

---

## 0. 一句话总结(最常用 3 条)

```bash
# 1) 启动 REPL(默认 mock LLM,不烧 token)
python -m scripts.dev_chat

# 2) 看完库里有啥
python -m smartbutler.scripts.memory_cli dump --all

# 3) 软删有多少该清的
python -m smartbutler.scripts.memory_cli age-summary
```

下面所有命令都是这 3 条的展开。

---

## 1. dev_chat 启动与验证(7.4 真实 EventBus)

### 1.1 最小启动(mock LLM,默认)

```bash
cd D:\soft\0project\test\SmartButler
python -m scripts.dev_chat
```

进入 REPL 后可用命令(`/help` 也能看到):

| 命令 | 作用 |
|------|------|
| `/help` | 显示帮助 |
| `/remember <text>` | 显式写一条长期记忆 |
| `/recall <query>` | 召回 3 条相关记忆 |
| `/stats` | 短期/长期记忆条数 |
| `/quit` / `/exit` | 退出 |

### 1.2 带 user 维度启动(多 user 场景,推荐)

```bash
# 方式 A:命令行
python -m scripts.dev_chat --user u-male --session dev-u-male-1

# 方式 B:环境变量(写到 .env 最省事)
set SMARTBUTLER_DEV_USER_ID=u-male
set SMARTBUTLER_DEV_SESSION_ID=dev-u-male-1
python -m scripts.dev_chat
```

### 1.3 走真实 LLM(烧 token,调试用)

```bash
# .env 必须配 SMARTBUTLER_LLM_* (API key / base_url / model)
python -m scripts.dev_chat --live --user u-male
```

### 1.4 关闭 memory 注入(只跑基础 chat,排查"是不是记忆污染"用)

```bash
python -m scripts.dev_chat --no-memory
# 或
set SMARTBUTLER_DEV_NO_MEMORY=1
python -m scripts.dev_chat
```

### 1.5 7.4 验证流程(EventBus 端到端 5 case)

启动 dev_chat 后,在**另一个终端**跑 memory_cli 验证持久化:

```bash
# 1) 启动 dev_chat(必须先启,否则库里没数据)
python -m scripts.dev_chat --user u-test --session dev-u-test-1

# 2) 另一个终端 —— 看长期库
python -m smartbutler.scripts.memory_cli dump --all

# 3) 跑语义检索
python -m smartbutler.scripts.memory_cli recall "上次聊的事" --k 5

# 4) 退出 dev_chat(必须先退!),再清软删
python -m smartbutler.scripts.memory_cli age-summary
python -m smartbutler.scripts.memory_cli vacuum --dry-run
```

### 1.6 dev_chat 启动失败排查(7.4 已知问题清单)

| 症状 | 原因 | 解决 |
|------|------|------|
| `_repl() missing 1 required positional argument: 'task_scheduler'` | 6.5 加了 task_scheduler 参数,旧测试用例没同步 | **已遗留**,不在 7.4 范围;短期解:用最新 `scripts/dev_chat.py` |
| `AlreadyLocked` / `PermissionError(WinError 32)` / `another instance` | dev_chat 还活着,锁住 `data/qdrant/` 目录 | **关掉 dev_chat 再跑 CLI**;CLI 有兜底:截断 `storage.sqlite` 让下次启动自动重头初始化 |
| `Qdrant dimension mismatch` | `.env` 里 `vector_size` 跟老 collection 不一致 | 看 §4 "改向量维度" 走 `clear-all --include-task-index` 重头 |

---

## 2. memory_cli — 翻库 / 检索(只读)

**入口**:`python -m smartbutler.scripts.memory_cli <cmd>`

**前置**:dev_chat 可以**同时**跑(只读不锁),但写操作(vacuum / purge / clear-all)要**先关 dev_chat**。

### 2.1 长期记忆(Qdrant)

| 子命令 | 作用 | 典型命令 |
|--------|------|---------|
| `dump` | **全量翻页看库里到底存了什么** | `python -m smartbutler.scripts.memory_cli dump --all` |
| `dump` | 只看某个 level | `python -m smartbutler.scripts.memory_cli dump --level daily --all` |
| `dump` | 按 tag 过滤 | `python -m smartbutler.scripts.memory_cli dump --tag "user:u-male" --all` |
| `dump-fields` | payload 字段分布体检(level / tag / importance 分布) | `python -m smartbutler.scripts.memory_cli dump-fields` |
| `recall <query>` | 语义检索(facade.recall 包装) | `python -m smartbutler.scripts.memory_cli recall "宠物" --k 5` |
| `recall` | 按 source 过滤 | `python -m smartbutler.scripts.memory_cli recall "宠物" --source u-male` |
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
| `stm-dump` | 只看某个 thread | `python -m smartbutler.scripts.memory_cli stm-dump --thread dev-u-test-1` |
| `stm-dump -v` | 加上 state 字段名 | `python -m smartbutler.scripts.memory_cli stm-dump -v --limit 3` |
| `stm-stats` | 短期库容量体检 | `python -m smartbutler.scripts.memory_cli stm-stats` |
| `st-raw <prefix>` | 打印某个 checkpoint 的原始 state JSON(深排查) | `python -m smartbutler.scripts.memory_cli st-raw 0c12345 -v` |

---

## 3. memory_cli — 清理 / 升格(写操作,需先关 dev_chat)

> ⚠️ **所有写操作前必做**:
> 1. **关掉 dev_chat**(否则 Qdrant 锁冲突,CLI 走文件级兜底,可能丢点)
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
python -m smartbutler.scripts.memory_cli purge --key "mem:u-male:abc123" --confirm yes

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
```

---

## 4. 改向量维度 / 重头开始(常见迁移场景)

`vector_size` 跟老 collection 不一致时,只能**删 collection 重建**(Qdrant 不支持改维度)。

```bash
# 1) 关掉 dev_chat
# 2) 备份(可选)
copy data\qdrant data\qdrant.bak.20261014

# 3) clear-all(连同 short_term.db / archive.db 一起清)
python -m smartbutler.scripts.memory_cli clear-all --confirm yes --yes-i-know-this-deletes-everything --no-dry-run --include-task-index

# 4) 改 .env 的 vector_size
#    SMARTBUTLER_STORAGE_QDRANT_VECTOR_SIZE=1024  ->  768

# 5) 启动 dev_chat,会按新 vector_size 重建 collection
python -m scripts.dev_chat
```

---

## 5. 排查"为什么这个没召回" / "为什么这个被记成 soft-deleted"

### 5.1 召回问题(命不中 / 命太多 / 命错 source)

```bash
# 1) 直接用 recall 复现(绕开 dev_chat,排除前端干扰)
python -m smartbutler.scripts.memory_cli recall "原 query" --k 10

# 2) 调 min-score 看是不是门槛把"正确但弱相关"的也挡了
python -m smartbutler.scripts.memory_cli recall "原 query" --k 10 --min-score 0.0

# 3) 看库里实际有哪些
python -m smartbutler.scripts.memory_cli dump --all --width 300

# 4) 按 source 拆开看
python -m smartbutler.scripts.memory_cli dump --tag "source:u-male" --all
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
│                     dev_chat 启动                              │
├──────────────────────────────────────────────────────────────┤
│ 基础      python -m scripts.dev_chat                          │
│ 带 user   python -m scripts.dev_chat --user u1 --session s1   │
│ 走 LLM    python -m scripts.dev_chat --live --user u1         │
│ 关 memory python -m scripts.dev_chat --no-memory              │
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
├──────────────────────────────────────────────────────────────┤
│                     memory_cli 写操作(先关 dev_chat)         │
├──────────────────────────────────────────────────────────────┤
│ 升格日报  python -m smartbutler.scripts.memory_cli consolidate │
│ vacuum   python -m smartbutler.scripts.memory_cli vacuum      │
│ 删 1 条   python -m smartbutler.scripts.memory_cli purge --key │
│              X --confirm yes                                   │
│ 批量删   ... purge-before --older-than-days 30 --confirm yes  │
│ 一键清空 ... clear-all --confirm yes --yes-i-know-this-       │
│              deletes-everything --no-dry-run                   │
└──────────────────────────────────────────────────────────────┘
```

---

## 7. 常见坑(踩过的)

| 坑 | 原因 | 规避 |
|---|------|------|
| 写操作报 `another instance` | dev_chat 锁着 `data/qdrant` | **先关 dev_chat** |
| `dump` 看到旧数据,以为 `clear-all` 没生效 | 嵌入式 Qdrant 删 collection 只清内存,`storage.sqlite` 残留 | 7.4 已加物理兜底:truncate `storage.sqlite` + 删 `.lock` / `meta.json`;**关掉 dev_chat** 走完 |
| `recall` 啥都召回不出来 | `min-score=0.55` 把弱相关全挡了 | 加 `--min-score 0.0` 看真基线 |
| 7.4 `_repl() missing 'task_scheduler'` | dev_chat 测试用例没跟 6.5 同步 | 不在 7.4 范围,用最新 dev_chat.py 即可 |
| `--live` 模式不响应 | `.env` 没配 `SMARTBUTLER_LLM_*` | 配 `OPENAI_API_KEY` / `OPENAI_BASE_URL` / `OPENAI_MODEL` |
| 短期库 stm-dump 显示"0 含 messages 的 checkpoint" | 反序列化失败(LangChain 对象 vs dict 判错) | 7.4 已修(走 `model_dump()` 降级);如仍 0,试 `--all-checkpoints` |
| 改了 `vector_size` 后报 dimension mismatch | Qdrant collection 不能改维度 | 走 §4 "改向量维度" 重建 |

---

## 8. 详细文档

- **架构 / 设计**:`docs/PHASE_6.2-6.5_TECHNICAL_DESIGN.md` §2.5.1(含 §2.5.1.16 vacuum 治理)
- **CLI 源码**:`smartbutler/scripts/memory_cli.py`(顶部 docstring 列出 7+5+2 个子命令)
- **7.4 实施**:`docs/PHASE_7_TECHNICAL_DESIGN.md` §3 + §12.6
- **dev_chat 源码**:`scripts/dev_chat.py`(顶部 docstring 列出所有启动方式 + REPL 命令)
- **变更历史**:`README.md` §10 变更记录
