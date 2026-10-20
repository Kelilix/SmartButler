# Phase 7.6 声纹注册流程设计方案

> 状态：草稿，待用户确认后实施
> 日期：2026-10-16

## 1. 问题背景

FunASR 多说话人 ASR 结果只带 `spk_id`（说话人编号），无法直接映射到用户身份。
需要建立 **声纹 embedding → user_id** 的桥梁，让管家知道"现在说话的是谁"。

## 2. 整体架构

```
[麦克风音频]
    ↓
[VoiceAdapter._flush_buffer]
    ↓
[FunASR 多说话人 ASR] → 返回 "text + spk_id"
    ↓
[ECAPA-TDNN / Resemblyzer 声纹提取] → embedding (512维)
    ↓
[SpeakerRegistry.identify(embedding)] → SpeakerMatch(user_id, confidence)
    ↓
[VoiceEvent.speakers] → [Orchestrator.ainvoke(speakers=...)]
    ↓
[管家 LLM] 知道当前在跟谁对话
```

## 3. 声纹注册触发时机

| 触发方式 | 场景 | 实现 |
|---------|------|------|
| **手动触发** | 用户说"帮我建立人声档案" | LLM 识别意图 → 触发 `enroll_speaker` Tool |
| **陌生人自动触发** | `identify()` 返回 `is_known=False` | VoiceAdapter 拦截陌生人事件 → 询问是否注册 |

### 3.1 手动触发流程（主要）

```
用户: "小管家，帮我建立人声档案"
   ↓
LLM 识别意图 → 调用 Tool: enroll_speaker(display_name="妈妈")
   ↓
管家询问: "好的，请说几句话让我记住你的声音"
   ↓
用户: [说3-5句，每句5-10秒]
   ↓
VoiceAdapter 采集音频 → ECAPA 提取 embedding → 存入 SpeakerRegistry
   ↓
返回: "人声档案建立成功！已记住你的声音，user_id=xxx"
```

### 3.2 陌生人自动触发（辅助）

```
VoiceAdapter.identify() 返回 is_known=False
   ↓
VoiceEvent(topic="voice.command", speakers=[unknown:xxx])
   ↓
Orchestrator 收到事件，发现 speakers[0].is_known=False
   ↓
LLM 主动询问: "我好像不认识你，要建立声纹档案吗？"
   ↓
用户: "好" → 进入手动触发流程
用户: "不用了" → 忽略，继续正常对话
```

## 4. Tool 设计：enroll_speaker

### 4.1 Tool 签名

```python
class EnrollSpeakerTool(BaseTool):
    name = "enroll_speaker"
    description = "建立声纹档案。首次使用管家时调用，让管家记住你的声音。以后每次说话都能识别你的身份。"

    async def arun(
        self,
        display_name: str,       # 必填：用户自称的名字，如"妈妈"、"爸爸"、"奶奶"
        user_id: str | None,    # 可选：用户指定ID；None=自动生成
        confirm_enrollment: bool = False,  # 内部标记：确认采集完成
    ) -> dict:
        """
        第一次调用：启动声纹采集，返回采集说明。
        第二次调用（confirm_enrollment=True）：确认采集完成，保存声纹。
        """
```

### 4.2 Tool 状态机

```
[初始] → enroll_speaker(display_name="妈妈")
    ↓ 返回: {"status": "collecting", "message": "请说3-5句话，每句5-10秒"}
    ↓ 用户说完
[采集中] → enroll_speaker(confirm_enrollment=True, user_id=xxx)
    ↓ 返回: {"status": "enrolled", "user_id": "xxx", "display_name": "妈妈"}
```

### 4.3 采集参数

- **采集句数**：3-5 句（每句独立提取 embedding，取平均）
- **每句时长**：5-10 秒
- **VAD 模式**：SmartButler 现有的 VAD 自然适用
- **最终 embedding**：多句 embedding 取平均，存入 `SpeakerRegistry`

## 5. SpeakerRegistry 存储设计

### 5.1 存储方案：SQLite（已确认）

用户已有 SQLite 基础设施，选用 SQLite 而非 JSON：

```sql
CREATE TABLE speaker_profiles (
    user_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    embedding BLOB NOT NULL,           -- 512维 float32 pickle 序列化
    enrolled_at TEXT NOT NULL,          -- ISO 8601 时间戳
    sample_count INTEGER DEFAULT 0,
    metadata TEXT                      -- JSON 字符串
);
```

优势：
- 与 `SqliteSaver`（LangGraph checkpointer）共用连接管理
- 结构化查询：按时间查、按 display_name 模糊搜索
- embedding BLOB 存储避免 JSON 的精度问题

**已确认：应用重启后声纹档案不丢失。**

### 5.2 扩展方案（未来）

```python
# 未来持久化扩展点（Phase 7.7）
class SQLiteSpeakerRegistry(EmbeddingSpeakerRegistry):
    def __init__(self, embedder, db_path: str = "data/speakers.db"):
        super().__init__(embedder)
        self._db_path = db_path
        self._init_db()
        self._load_all()  # 启动时从 SQLite 加载到内存 dict

    async def register(self, profile: SpeakerProfile) -> None:
        await super().register(profile)  # 写内存 dict
        self._save_to_db(profile)         # 持久化到 SQLite

    async def unregister(self, user_id: str) -> bool:
        deleted = await super().unregister(user_id)
        if deleted:
            self._delete_from_db(user_id)
        return deleted
```

本方案暂不实现 SQLite，直接用内存 dict（Phase 7.7+ 再扩展）。

## 6. user_id 生成规则

| 场景 | user_id | 示例 |
|------|---------|------|
| 用户指定 | 用户提供的值 | `mommy`, `dad`, `grandma` |
| 自动生成 | `speaker_<short_hash>` | `speaker_a3f2b1` |

- 自动生成时取 embedding 前 8 维的 hash 的前 7 位
- user_id **唯一约束**：重复注册抛 `ValueError`

## 7. 声纹采集时的音频隔离

### 问题

声纹采集时（`enroll_speaker`），麦克风被 Tool 层占用，`VoiceAdapter` 的主循环还在跑。

### 方案

在 `VoiceAdapter` 中新增**声纹采集模式**：

```python
class VoiceAdapter:
    async def start_voice_enrollment(self, user_id: str) -> None:
        """进入声纹采集模式：暂停主 loop，专注采集音频。"""
        self._enrollment_mode = True
        self._enrollment_user_id = user_id
        self._enrollment_audio: list[np.ndarray] = []
        # 通知主 loop 暂停

    async def stop_voice_enrollment(self) -> list[np.ndarray]:
        """退出采集模式，返回采集到的音频片段列表。"""
        self._enrollment_mode = False
        return self._enrollment_audio
```

`enroll_speaker` Tool 调用这两个方法，控制音频采集的起止。

## 8. 现有代码改动点

| 文件 | 改动 |
|------|------|
| `capabilities/tools/` | 新增 `enroll_speaker.py` |
| `voice/adapter/voice_adapter.py` | 新增 `start_voice_enrollment` / `stop_voice_enrollment` |
| `voice/speaker/ecapa.py` 或 `resemblyzer.py` | 确保可正常提取 embedding |
| `events/routing/speaker_registry_embedding.py` | 确认 `EmbeddingSpeakerRegistry` 可用 |
| `orchestrator.py` | `ainvoke(speakers=[...])` 透传到 LLM（已就绪） |

## 9. 实施顺序

1. **Phase 1**：修复 FunASR `distutils` 依赖（已完成 ✅）
2. **Phase 2**：VoiceAdapter 新增采集模式 API
3. **Phase 3**：`EnrollSpeakerTool` 实现（不含采集逻辑）
4. **Phase 4**：集成 VoiceAdapter 采集模式到 Tool
5. **Phase 5**：测试手动触发 + 陌生人自动触发流程

---

## 10. 待确认问题

请确认以下几点，我好调整方案：

1. **user_id 生成策略**：自动生成时用 hash 前缀 `speaker_` 可以吗？还是希望用户提供更友好的 ID？
2. **采集句数**：3-5 句是否合适？有些人声音变化大可能需要更多样本。
3. **持久化**：当前用内存 dict，应用重启丢失。能接受吗？还是需要 JSON 文件持久化？
4. **陌生人自动触发**：registry 找不到时弹窗/询问，交互体验上这样设计是否OK？
