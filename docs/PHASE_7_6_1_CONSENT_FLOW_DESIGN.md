# Phase 7.6.1 技术方案:声纹采集交互改造(主动询问 → 确认 → 采集)

> **状态**:草稿,待用户确认后实施
> **日期**:2026-11-01
> **范围**:陌生人声纹的**采集触发方式**改造 —— 从「后台静默自动采集」改为「Agent 主动询问 → 用户明确确认 → 采集 → 命名」
> **不包含**:声纹识别算法本身(已是 Resemblyzer)、SQLite 持久化(已实现)、情绪治理(Phase 8,另案)
> **关联文档**:`docs/PHASE_7_6_VOICE_ENROLLMENT_DESIGN.md`(声纹注册原始设计,**本文档是其 §3.2 的落地修正**)/ `docs/PHASE_7_TECHNICAL_DESIGN.md`(事件驱动 + 路由)/ `smartbutler/thinking/ADR-009-proactive-reactive-dual-loop.md`
> **关联 ADR**:无新增 ADR(本文档是设计文档,实施时如引入新状态机再补)

> **🆕 定位变更(2026-11-01,本轮敲定)**:
> - 原始设计 §3.2 明确写过「LLM 主动询问:我好像不认识你,要建立声纹档案吗?」,但实现阶段被改成**后台静默自动采集**(见 `enroll_speaker.py` 顶部注释「用户决策:按A,LLM不卡,后台进行」)。
> - 本方案**回归原始设计的交互意图**,但补齐原始设计缺失的三块拼图:**同意机制 / 状态机 / 回声抑制**。
> - 用户本轮决策:**严格确认**(必须识别到明确同意才采集)+ **询问时一并问名字** + **先出设计文档**。

---

## 0. 当前状态盘点(2026-11-01)

### 0.1 现状:后台静默采集

```
陌生人说话 → _flush_buffer
  ↓ publish(VoiceEvent, user_id=stranger_xxx)   ← 正常对话照常,LLM 立刻回应
  ↓ should_enroll=True → start_voice_enrollment()
  ↓
[采集模式] 静默收集 3 句 → 回调 → VoiceprintEnrollmentService → 落库
     ↑ 全程不发 VoiceEvent、不播 TTS、不问用户
```

关键代码位置:

| 位置 | 职责 |
|------|------|
| `voice/adapter/voice_adapter.py:499` | `_flush_buffer` 末尾触发 `start_voice_enrollment()` |
| `voice/adapter/voice_adapter.py:730` | `start_voice_enrollment()` —— 切 `_enrollment_mode` |
| `voice/adapter/voice_adapter.py:531` | `_enrollment_listen()` —— 静默采集循环 |
| `events/routing/speaker_registry_embedding.py:243` | `should_trigger_enrollment()` —— 双阈值判定 |
| `events/routing/voiceprint_enrollment.py:140` | `enroll_from_segments()` —— 提取 + 注册 |
| `capabilities/tools/voice/enroll_speaker.py:122` | Tool —— 单阶段非阻塞 |

### 0.2 真实缺口

| 缺口 | 现状 | 风险 |
|------|------|------|
| **无用户知情** | 采集全程静默,用户不知道在被录音 | ❌ 隐私风险;体验像间谍软件 |
| **无用户同意** | 陌生人一开口就自动建档 | ❌ 未经 opt-in 就处理生物特征数据 |
| **名字永远是「陌生人」** | `VoiceprintEnrollmentService._default_display_name = "陌生人"` | ❌ 注册后无法区分是谁,人物表失去意义 |
| **采集内容不可控** | 后台采集窗口 = 用户说完话后的 60s | ❌ 可能录到电视声/旁人/噪声 |
| **TTS 回声污染** | `mic_capture` 无 AEC / 播放期抑制 | ❌ 管家自己的声音会被录进声纹 |
| **失败不可感知** | 采集失败只写日志 | ❌ 用户以为被记住了,其实没有 |

### 0.3 与原始设计 §3.2 的差距

原始设计 §3.2 画的是「LLM 主动询问 → 用户回答好/不用了 → 进入采集」,但实现时走了「后台静默」。本次改造等于**把实现拉回原始设计**,并补齐原始设计没写的实现细节。

---

## 1. 方案总览

### 1.1 目标交互流(严格确认版)

```
陌生人说话("今天天气怎么样")
  ↓
VoiceAdapter: 声纹未命中 → should_enroll=True
  ↓ publish VoiceEvent(user_id=stranger_xxx, is_known=False)
  ↓
🧠 Agent: "你好，我还不太确定你是谁。方便告诉我你的名字，让我记住你的声音吗？"
  ↓ TTS 播放(⚠️ 播放期做回声抑制)
  ↓ 进入【确认窗口】(等 1 个短语音,最长 5s)
  ├─ ASR 识别到同意(要/好/可以/行/yes) → ✅ 采集
  ├─ ASR 识别到拒绝(不用/不要/算了/no)  → ❌ 放弃,记 cooldown,不再打扰
  └─ 静默超时 / 听不清               → ❌ 放弃,记 cooldown,不再打扰
  ↓
🧠 Agent: "好的，{name}。请说 3 句话，每句 5-10 秒。"
  ↓ 进入【采集模式】(3 句,最多 60s)
  ↓
🧠 Agent: "记住了，{name}。以后我能听出是你了。"
```

### 1.2 与现状的对照

| 维度 | 现状(后台静默) | 本方案(主动询问) |
|------|----------------|------------------|
| 用户知情 | ❌ | ✅ TTS 明确告知 |
| 用户同意 | ❌ 静默采集 | ✅ **必须识别到明确同意** |
| 称呼 | ❌ 永远「陌生人」 | ✅ 询问时一并问,登记真实名字 |
| 采集内容 | ❌ 60s 窗口,可能录到噪声 | ✅ 用户知情配合,质量高 |
| 失败反馈 | ❌ 仅日志 | ✅ TTS 告知失败原因 |
| 拒绝后 | ❌ 30s 去抖后还会再问 | ✅ 冷却期内**不再打扰** |
| LLM 是否阻塞 | ✅ 不阻塞 | ⚠️ 多一轮交互(但用户主动参与,可接受) |

### 1.3 为什么用「严格确认」而不是「问完直接采」

用户本轮明确选择**严格确认**。技术原因:

> 采集开始的那一刻,用户必须能**说出一个独立的、不属于采集内容的词**("要")。
> 如果"问完直接采",用户的"要"这声会被**录进声纹的第一句** ——
> 声纹里混入一个单音节词,虽然影响不大,但更严重的是:
> **TTS 正在播报问题时,麦克风可能同时录进管家自己的声音**(见 §4 回声抑制)。

严格确认把「管家说话」和「用户同意」在时间上分开,且给了回声抑制一个明确的静默窗口。

---

## 2. 架构设计

### 2.1 新增状态机

`VoiceAdapter` 目前是 `IDLE / LISTENING / SPEAKING` 三态。本次新增**两个采集相关状态**:

```
IDLE ──> LISTENING ──> SPEAKING
            │
            └──> CONFIRMING ──> ENROLLING ──> IDLE
                    (等同意)      (采集中)
```

| 状态 | 含义 | 谁驱动 |
|------|------|--------|
| `CONFIRMING` | 等用户回答「要不要录声纹」 | VoiceAdapter 内部循环 |
| `ENROLLING` | 正在采集 N 句 | VoiceAdapter 内部循环 |

**为什么放在 VoiceAdapter 而不是 Agent/Orchestrator 层?**

采集是**音频流层面的操作**,只有 VoiceAdapter 持有 mic stream。放在上层会导致音频流跨层传递。VoiceAdapter 只负责「采到够/没采够」,**业务语义(同意与否、叫什么名字)交给上层**。

### 2.2 分层职责

```
┌─────────────────────────────────────────────────────────┐
│ Orchestrator / Agent (LLM)                              │
│   · 看到 is_known=False → 决定「要不要问」               │
│   · 生成询问话术(带名字)                                │
│   · 解析用户回答 → 同意/拒绝                              │
│   · 调用 enroll_speaker(confirm=...) 启动采集              │
└─────────────────────────────────────────────────────────┘
                    ↓ VoiceEvent / Tool
┌─────────────────────────────────────────────────────────┐
│ VoiceAdapter (音频层)                                    │
│   · CONFIRMING: 采一段短音频 → ASR → 交给上层判定         │
│   · ENROLLING: 采 N 句 → 回调                             │
│   · 回声抑制: TTS 播放期不录音                            │
└─────────────────────────────────────────────────────────┘
                    ↓ 回调
┌─────────────────────────────────────────────────────────┐
│ VoiceprintEnrollmentService (注册层)                      │
│   · 过滤无效片段 → 提取 embedding → 平均 → 落库            │
└─────────────────────────────────────────────────────────┘
```

---

## 3. 同意机制(核心)

### 3.1 Tool 改造:`enroll_speaker` 两阶段

现状是**单阶段非阻塞**:

```python
# 现状:调用即启动采集,LLM 立刻返回
async def enroll_speaker(display_name: str, user_id: str | None = None) -> dict:
    await _voice_adapter.start_voice_enrollment()
    return {"status": "collecting", "message": "..."}
```

改为**两阶段**:

```python
@register_tool(
    name="enroll_speaker",
    description=(
        "建立声纹档案(记住某个人的声音)。\n"
        "【必须先取得用户明确同意】:\n"
        "  1. 第一次调用(confirm=False):启动采集,返回提示语,让用户说 3-5 句。\n"
        "  2. 采集完成后会自动注册,无需再次调用。\n"
        "严禁在用户未明确同意前调用本工具。"
    ),
    ...
)
async def enroll_speaker(
    display_name: str,
    user_id: str | None = None,
    confirm: bool = False,        # 🆕 用户已明确同意
) -> dict[str, Any]:
    """启动声纹采集(注册在后台自动完成)。

    Args:
        display_name: 用户自称的名字,如"妈妈"。
        user_id: 已知 user_id(重新采集时保留原 id)。
        confirm: 🆕 用户是否已明确同意录入声纹。

    Returns:
        dict: 含 status / message / instruction。
        status="consent_required" → 调用方还没取得同意,不该走到这。
        status="collecting"       → 已启动采集。
    """
```

### 3.2 同意词表(判定放在哪一层?)

**放在 Agent(LLM)层,不放在代码里。** 理由:

| 方案 | 问题 |
|------|------|
| 代码里硬编码词表("要"/"好"/"可以") | 漏词("嗯""行吧""随便"),且方言/口音/噪声会误判;LLM 本来就擅长这个 |
| LLM 判定 | ✅ 天然处理同义、口语、语气;可结合上下文 |

但 LLM 判定需要**把用户这句话喂回去**。这引出下面的确认窗口设计。

### 3.3 确认窗口(CONFIRMING 状态)

```
[Agent 播报] "你好，方便让我记住你的声音吗？"
      ↓
[VoiceAdapter] 进入 CONFIRMING,监听一个短窗口
      ↓
   收到一段语音 → ASR → 发一个 VoiceEvent(action=CONFIRM_ANSWER)
      ↓
[Agent] 读 ASR 文本 → 判断同意/拒绝
      ↓
   同意 → enroll_speaker(display_name=..., confirm=True) → ENROLLING
   拒绝 → 不调用,记 cooldown
```

**关键设计点**:

| 点 | 设计 | 理由 |
|----|------|------|
| 窗口时长 | 5s(可配) | 够用户说"要",不至于等太久 |
| 是否需要唤醒词 | ❌ 不需要 | 已经在对话中,再要求唤醒词很烦 |
| 静默超时 | 视为拒绝 | 沉默 = 不愿意,尊重用户 |
| 采集内容 | 这一段**不计入**声纹 | 它是"要"这个单音节词,没价值 |
| 音频去哪 | 发 `VoiceEvent(action=CONFIRM_ANSWER)` | 复用现有事件管道,LLM 能直接看到 |

### 3.4 新增 VoiceAction

```python
class VoiceAction(StrEnum):
    """语音动作枚举。"""

    WAKE_WORD = "wake_word"      # 唤醒词
    COMMAND = "command"          # 唤醒后用户说话
    INTERRUPT = "interrupt"      # 用户打断
    SILENT = "silent"            # 唤醒后无语音(放弃)
    CONFIRM_ANSWER = "confirm_answer"  # 🆕 声纹采集同意窗口内的回答
```

`CONFIRM_ANSWER` 事件携带:
- `asr_text` —— 用户说的原文("要"/"不用")
- `device_id`
- `user_id` —— 陌生人临时 id(便于 Agent 关联上下文)
- `audio_meta` —— 可含该段音频(若 Agent 想自己判断)

### 3.5 冷却机制(拒绝后不再打扰)

现状的去抖是 `_enrollment_debounce_seconds = 30.0`,只防「30 秒内重复触发」。

本方案需要**冷却**,防止用户明确拒绝后还被反复问:

```python
# VoiceAdapter
self._consent_cooldown: dict[str, float] = {}   # 冷却键 → 下次可询问的时间戳
self._consent_cooldown_seconds: float = 24 * 3600.0  # 拒绝后当天不再问
```

> 📌 **用户决策(2026-11-01)**:**确认窗口 5 秒**、**拒绝后当天不询问**。
> 所以冷却时长取 24 小时(而不是设计稿初版的 1 小时)。

#### 冷却键的选择(实现时修正)

设计稿初版写的是「按 user_id 冷却」,**实现时发现这是错的**:

陌生人临时 id(`stranger_xxx`)是**声纹前 8 维的哈希**,同一个人说不同的话
→ 声纹不同 → 哈希不同 → id 不同。如果直接拿它当冷却键:

```
第 1 轮:用户拒绝,记下 stranger_a1b2c3d
第 2 轮:同一个用户说话,id 变成 stranger_e4f5g6h
        → 查不到冷却 → 又去问     ← 「当天不再问」完全失效
```

而且这个失效**很隐蔽**:日志里能看到冷却记录,但询问照旧。

所以冷却键改成:

| 说话人类型 | 冷却键 | 理由 |
|-----------|--------|------|
| 陌生人(`stranger_xxx`) | `device:{device_id}` | id 跨轮不稳定,只能退到设备级。宁可少问一次,不可多问 —— 隐私优先 |
| 已注册(`speaker_xxx`) | `user:{user_id}` | 重采集场景,精确到人 |
| 未指定 | `device:{device_id}` | 同设备兜底 |

> ⚠️ 代价:陌生人拒绝后,**同设备的其他陌生人**当天也不会被问。
> 考虑到这是「智能音箱」这种单户设备,实际影响很小,收益是绝不打扰。

#### 谁来写冷却?

只有 prompt 约束 LLM「拒绝时不要调用工具」是**不够**的 —— 拒绝这个状态没人记录,
结果就是用户被反复问。所以新增一个配套工具:

```python
decline_speaker_enrollment(user_id: str | None = None, reason: str = "")
```

Agent 判定为「拒绝」时调用它 → 写入 cooldown。沉默超时也按拒绝处理(沉默 = 不愿意)。

---

## 4. 回声抑制(必须做,否则声纹会被管家自己的声音污染)

### 4.1 问题

当前 `mic_capture` 是**裸采集**,无 AEC、无播放期抑制:

```
TTS 正在播 "请说 3 句话让我记住你的声音"
    ↓ 麦克风同时录到
VAD 判定为语音 → 管家自己的声音进了 buffer
```

后果:
1. 声纹 embedding 被管家音色污染 → 识别率下降
2. `MIN_AUDIO_RMS_FOR_ENROLLMENT` / `MIN_SAMPLES_FOR_VALID_SEGMENT` 过滤可能把管家的话当有效段
3. 用户回答「要」的那一声,可能和 TTS 尾巴重叠

### 4.2 方案:播放期门控(不引入重型 AEC)

不引入 WebRTC AEC(依赖重、Windows 声卡回环路径复杂),采用**播放期门控 + 播放后静默窗**。

#### ⚠️ 门控必须放在**播放器层**(实现时修正)

设计稿初版把门控放在 `VoiceAdapter.speak()` 里。实现时发现**漏掉了主路径**:

```
管家回复有两条播放路径:
  1. VoiceAdapter.speak()          → player.play()     ← 设计稿只考虑了这一条
  2. TTSOutputAdapter.push()       → player.play()     ← Agent 的正常回复走这条!
     ↑ AnswerRouter → TTSOutputAdapter → player
```

Agent 平时说的话**全部**走路径 2,路径 1 只在少数直接调用 `speak()` 时用。
门控放在路径 1 的话,等于**日常对话完全不设防**。

好在两条路径**共用同一个 `player` 实例**(`voice_service.py` 里同一个 `player`
同时传给 `VoiceAdapterConfig` 和 `TTSOutputAdapter`),所以把门控下沉到
`BaseAudioPlayer` 就一次覆盖两条路径:

```python
class BaseAudioPlayer(ABC):
    def __init__(self, playback_tail_seconds: float = 0.3) -> None:
        self._playback_tail_seconds = playback_tail_seconds
        self._playback_gate_until: float = 0.0

    async def play_gated(self, audio_stream: AsyncIterator[bytes]) -> None:
        """带回声抑制门控的播放(所有播报路径都应走这个)。"""
        self._playback_gate_until = time.monotonic() + self._playback_tail_seconds
        try:
            await self.play(audio_stream)
        finally:
            self._playback_gate_until = time.monotonic() + self._playback_tail_seconds
```

采集侧和确认窗口统一读 `player.playback_gate_until`:

```python
async for chunk in self._capture.stream():
    if self._is_playback_gated():
        continue    # 🔇 播放期内,丢弃所有音频
```

| 参数 | 建议值 | 说明 |
|------|--------|------|
| `PLAYBACK_TAIL_SECONDS` | 0.3s | 扬声器尾音 + 房间反射消散 |
| 播放本身期间 | 门控生效 | `play_gated` 在播放**前**就开门控 |
| 确认窗口起始 | TTS 播完后 **+0.5s** | 避免用户抢答被截断 |

> 📌 **注意**:确认窗口是**先等门控结束再开始听**,不是「边等边听」。
> 否则用户抢答的那句会被门控吃掉,或者把管家自己的话当成用户回答。

### 4.3 更彻底的方案(可选,后续)

若门控后声纹质量仍不达标,再考虑:
- Windows 端启用 `WASAPI` 独占模式 + 系统级 AEC(需要 `sounddevice` 换 stream)
- 或在 TTS 播放期间**暂停 mic stream**(而非丢弃数据),省 CPU

**本期只做门控**。

---

## 5. 采集流程细化

### 5.1 采集参数

| 参数 | 当前值 | 本方案 | 说明 |
|------|--------|--------|------|
| 目标句数 | 3 | 3 | 不变 |
| 每句最短 | 10 chunk (1s) | 1s | 不变 |
| 每句最长 | — | 10s | 显式限制,防止用户长篇大论 |
| 片段间静默 | 3s (`silence_timeout_ms`) | 3s | 不变 |
| 总超时 | 60s(本次新增) | 60s | 不变 |
| **注册门槛** | 1 段 (`MIN_SAMPLES_FOR_ENROLLMENT`) | 1 段(建议加 low_confidence 标记) | ⚠️ 与「目标句数 3」是两个数,见 §5.3 |

### 5.2 每句结束时的用户反馈

当前采集是**完全静默**的,用户不知道说够没、说到第几句。

改造为**每句给一次简短反馈**(TTS):

```
用户: "今天天气真不错啊"
  ↓ 3s 静默 → 保存片段 1
🔊 "好的,第一句。"
用户: "我今天心情不太好"
  ↓
🔊 "第二句,还有一句。"
用户: "谢谢你啊"
  ↓
🔊 "好了,记住了。"
```

**注意**:这三次 TTS 播放**都在采集窗口内**,所以:
- 管家的话**必须**被门控丢弃(§4)
- 或者更简单:**采集期间不播 TTS**,改为**用灯光/音效**提示(若有)

> ⚠️ **用户决策(2026-11-01)**:**不做逐句播报**。
> 改为:**采集开始前播一次**「请说 3 句话让我记住你的声音」,
> **采集结束后播一次**「记住了,{name}」。
> 逐句播报列为可选增强(§6)。
>
> 理由:逐句 TTS 会显著增加回声风险和采集耗时(每次播完要等静默散音),
> 而收益(让用户知道进度)对 3 句的短采集来说不大。
>
> 📌 **实现要点**:采集**开始前**的播报由 Agent 的正常回复路径承担
> (`enroll_speaker` 返回 `status="collecting"`,Agent 据此回复用户);
> 采集**结束后**的播报在 `voice_service.py` 的
> `_on_enrollment_complete` 回调里做 —— 那里能拿到注册结果
> (成功/失败/样本数),播报更准确。

### 5.3 采集失败处理

> ⚠️ 注意常量差异:`VoiceprintEnrollmentService.MIN_SAMPLES_FOR_ENROLLMENT = 1`
> (2026-11-01 调的,因为用户可能只说一句就停,不该把有效样本扔掉),
> 而 `VoiceAdapter` 的采集目标是 **3 句**。
> 所以「采集目标」和「注册门槛」是**两个不同的数**,失败判定要按下面的逻辑走。

```
采集结果
  ├─ 有效段 >= 3 → 注册成功 → TTS "记住了,你是 {name}"
  ├─ 有效段 1-2 → **仍然注册**(门槛是 1)→ TTS "记住了,你是 {name}"
  │                  ⚠️ 但样本少,识别率会偏低;建议追加一句
  │                  "记下了,不过你说得有点少,以后多说说会更准"
  ├─ 有效段 0   → 不注册 → TTS "没听清呢,方便再试一次吗?"
  └─ 超时       → 用已有片段尝试(同 1-2 分支)
```

失败后是否重试:**重试一次**,再失败就放弃并记 cooldown(不要无限纠缠)。

> 📌 **设计决策**:现状门槛=1 意味着**用户只说一句"你好"也会被注册**,
> 这会污染声纹库(单句样本噪声大)。本方案建议:
> - 采集目标仍是 3 句(给足样本)
> - 但**注册门槛改成 2**(至少两句才建档),避免"嗯""啊"这种噪声入库
> - 或保持门槛=1,但在 profile 上标记 `sample_count=1` + `low_confidence=True`,
>   后续有人来匹配时降权。
>
> **建议后者**(不改现有阈值,只加标记),风险更小。见 §12 待确认问题 5。

---

## 6. 状态与数据流

### 6.1 完整时序

```
User        Agent(LLM)      VoiceAdapter      Registry      TTS
 │             │                │                │          │
 │ "今天天气"  │                │                │          │
 │────────────►│ via VoiceEvent │                │          │
 │             │ (is_known=F)  │                │          │
 │             │──问?─────────►│                │          │
 │             │◄─"记得你声音吗"────────────────────────────│
 │             │                │                │          │
 │ "要"        │                │                │          │
 │────────────►│ via CONFIRM_ANSWER             │          │
 │             │  (LLM 判定:同意)                │          │
 │             │──enroll(confirm=T, name=XX)───►│          │
 │             │                │──ENROLLING────►│          │
 │             │                │  采 3 句        │          │
 │             │                │──segments──────►│          │
 │             │                │                │─注册────►│
 │             │◄──"记住了,XX"─────────────────────────────│
 │             │                │                │          │
```

### 6.2 状态转移表

| 当前 | 事件 | 下一 | 动作 |
|------|------|------|------|
| `IDLE` | 唤醒 | `LISTENING` | |
| `LISTENING` | VAD 命中 → 静默超时 | `IDLE` | `_flush_buffer` → publish + 可能触发询问 |
| `LISTENING` | `should_enroll` 且非冷却期 | `LISTENING`(同轮) | publish 一个 `ENROLL_SUGGEST` 元信息 |
| `LISTENING` | Agent 调 `enroll_speaker(confirm=T)` | `ENROLLING` | |
| `ENROLLING` | 3 段收齐 | `IDLE` | 回调注册 + TTS 反馈 |
| `ENROLLING` | 超时 | `IDLE` | 同上(样本不足则失败) |
| `ENROLLING` | `stop_voice_enrollment()` | `IDLE` | 丢弃未完成 buffer |
| `LISTENING` | 询问窗口超时 | `LISTENING` | 记 cooldown |

### 6.3 谁决定「要不要问」

**双层判定,缺一不可**:

| 层 | 判定 | 理由 |
|----|------|------|
| VoiceAdapter | `should_trigger_enrollment()` 双阈值 + RMS | 已有。挡住「熟人但这次含糊」和「噪声」 |
| Agent | 是否值得打断用户 | 已有 `should_enroll` 语义,但**新增**:LLM 可以选择不问(比如用户只是随口一句,上下文很忙) |

> 现状是 VoiceAdapter **直接**触发采集,没有给 Agent「选择不问」的机会。
> 改造后:VoiceAdapter 只**标记**「这个人可以采集」,Agent 决定**要不要开口问**。

实现上,给 `VoiceEvent` 加一个字段:

```python
class VoiceEvent(BaseEvent):
    ...
    # 🆕 是否建议询问登记声纹(陌生人 + 质量达标)
    suggest_enrollment: bool = False
    # 🆕 触发原因(可观测)
    enrollment_reason: str = ""
```

---

## 7. 代码改动清单

| 文件 | 改动 | 风险 |
|------|------|------|
| `voice/adapter/voice_adapter.py` | 新增 `CONFIRMING`/`ENROLLING` 状态;`_confirm_listen()`;`speak()` 加播放门控;`_flush_buffer` 改为「标记」而非直接采集 | 🔴 高(核心状态机) |
| `events/protocol/voice_event.py` | 新增 `VoiceAction.CONFIRM_ANSWER`;`VoiceEvent` 加 `suggest_enrollment`/`enrollment_reason` | 🟢 低 |
| `capabilities/tools/voice/enroll_speaker.py` | 两阶段改造(加 `confirm` 参数);`set_pending_context` 支持并发采集的上下文栈 | 🟡 中 |
| `events/routing/voiceprint_enrollment.py` | `display_name` 从「陌生人」改为**必填**(或由 tool 传入);失败结果要能被 TTS 播报 | 🟡 中 |
| `thinking/loop/orchestrator.py` | 透传 `suggest_enrollment` 到 Agent;`_resolve_user_name` 支持陌生人 | 🟡 中 |
| `thinking/loop/nodes/decide.py` | system prompt 增加「何时该问 / 怎么问 / 怎么判同意」的指引 | 🟡 中(改 prompt 要回归测) |
| `voice/registry`(`sqlite`) | 支持 `display_name` 更新 | 🟢 低 |

### 7.1 prompt 指引(需回归测试)

Agent 需要知道:

```markdown
## 声纹登记(仅在用户明确同意后才可采集)

当事件里 `suggest_enrollment=true` 时,表示"当前说话人的声音是陌生的,
且音频质量可用,可以考虑记住他"。

你可以选择:
- **忽略**:对方只是随口一问/当前对话更重要 → 不打断,照常回答。
- **询问**:用一句自然的话询问,例如
  "你好,方便告诉我你的名字、让我记住你的声音吗?"

如果用户回答同意(如"要"/"好"/"可以"),调用 `enroll_speaker(display_name=..., confirm=true)`。
如果用户拒绝(如"不用"/"算了"),不要调用,继续正常对话。
**严禁在用户明确同意前调用 enroll_speaker。**
```

### 7.2 并发采集的上下文栈

现状 `_pending_context` 是**单个 dict**,采集完成时 `take_pending_context()` 取走。

如果两个人同时要采集(或同一设备反复触发),会串。改为**栈**:

```python
_pending_contexts: list[dict[str, Any]] = []

def push_pending_context(*, display_name, user_id): ...
def pop_pending_context() -> dict: ...
```

> ⚠️ 但 `VoiceAdapter` 的采集是**单通道**(`_enrollment_mode` 一个 bool),
> 两个人不能同时采集。所以栈主要解决「上一次采集还没回调,新的又来了」。
> **本期用栈 + 保守策略**:采集进行中不允许新的采集请求(直接返回 busy)。

---

## 8. 测试方案

### 8.1 单元测试

| 测试 | 断言 |
|------|------|
| `test_confirm_window_accepts_consent` | 说出"要" → 发出 `CONFIRM_ANSWER` 事件 |
| `test_confirm_window_timeout` | 5s 静默 → 放弃,不进采集 |
| `test_confirm_answer_not_recorded` | 确认窗口的音频**不进** `_enrollment_audio` |
| `test_playback_gate_blocks_capture` | TTS 播放期内麦克风音频被丢弃 |
| `test_enroll_requires_confirm` | 未确认时调 tool → 不启动采集 |
| `test_reject_sets_cooldown` | 拒绝后 1h 内不再触发询问 |
| `test_enrollment_timeout_no_deadlock` | 超时后主循环恢复(回归 2026-11-01 修复) |
| `test_display_name_not_stranger` | 注册后 `display_name` 是真实名字 |
| `test_single_sample_marked_low_confidence` | 只采到 1 段时,profile 标记 `low_confidence=True` |

### 8.2 集成测试

- 陌生人 → Agent 询问 → 同意 → 采 3 句 → 注册成功 → `display_name` 正确
- 陌生人 → Agent 询问 → 拒绝 → 不采集,对话继续
- 熟人 → 不询问,正常对话

### 8.3 真机验证(必须)

```powershell
python -m smartbutler.scripts.voice_service --no-fake --vad-threshold 0.02
```

期望日志序列:

```
🎙️ 声纹可采集: reason=stranger user_id=stranger_xxx
🔊 Agent: 你好,方便告诉我你的名字、让我记住你的声音吗?
🎤 CONFIRMING 窗口开启(5s)
🎤 CONFIRM_ANSWER: asr_text=要
🔊 Agent: 好的,小明。请说 3 句话,每句 5-10 秒。
🎙️ 声纹采集: 保存第 1 个片段, 时长=6.20s, RMS=0.1234
🎙️ 声纹采集: 保存第 2 个片段, 时长=5.80s, RMS=0.1122
🎙️ 声纹采集: 保存第 3 个片段, 时长=7.10s, RMS=0.1301
🎙️ 声纹采集完成: 共采集 3 个有效片段
🔢 声纹提取 #1: 时长=6.20s 维度=256 范数=...
📊 平均声纹: 3 段, 维度=256, 范数=...
🆔 声纹自动注册完成: status=created user_id=speaker_xxx display_name=小明
🔊 Agent: 记住了,小明。
```

---

## 9. 风险与缓解

| 风险 | 影响 | 缓解 |
|------|------|------|
| **TTS 回声**污染声纹 | 识别率下降 | §4 播放期门控 + 尾部静默;门控后仍不达标则暂停 mic stream |
| **用户抢答**被门控截断 | 确认失败 | 门控结束后 +0.5s 才开确认窗口;必要时用户可以说「等一下」再重来 |
| **LLM 误判同意**("不是不行") | 未同意就采集 | prompt 里给出明确判定指引;**代码侧再加一道 `confirm=True` 门槛**(tool 不信 LLM,只信参数) |
| **Agent 每次都问**,太啰嗦 | 体验差 | 双阈值(已有)+ 按人 cooldown + Agent 自主选择不问 |
| **确认窗口超时**导致用户被"无视" | 用户困惑 | 超时后 TTS 播一句「没听清,先不说这个了」 |
| **采集期间用户忘了在干嘛** | 困惑 | 采集前 TTS 明确说明要说什么、说几句 |
| **`_pending_context` 串味** | 注册到错的人 | §7.2 上下文栈 + 采集中拒绝新请求 |

### 9.1 「代码侧再加一道门槛」的实现

即使 LLM 判定同意,`enroll_speaker` 也必须收到 `confirm=True` 才启动采集:

```python
async def enroll_speaker(display_name, user_id=None, confirm=False):
    ...
    if not confirm:
        # 没拿到明确同意 → 不启动采集,返回"需要同意"
        return {
            "status": "consent_required",
            "message": "尚未获得用户明确同意,不能采集声纹。",
        }
    await _voice_adapter.start_voice_enrollment()
    return {"status": "collecting", ...}
```

> 这条是**硬约束**:LLM 忘记传 `confirm=True` 就不会采集。
> 宁可漏采集,不可未经同意采集。

---

## 10. 实施顺序(建议)

| 阶段 | 内容 | 产出 | 可独立验证 |
|------|------|------|----------|
| **S1** | `VoiceAction.CONFIRM_ANSWER` + `VoiceEvent.suggest_enrollment` 字段 | 协议层 | ✅ 纯协议改动 |
| **S2** | VoiceAdapter `CONFIRMING` 状态 + `_confirm_listen()` | 采集层 | ✅ 假麦克风可测 |
| **S3** | TTS 播放门控(回声抑制) | 音频层 | ✅ 单元测试可测 |
| **S4** | `enroll_speaker` 两阶段 + `confirm` 硬门槛 | Tool 层 | ✅ 单测可测 |
| **S5** | Agent prompt 指引 + `should_enroll` 改为「标记」 | 决策层 | ⚠️ 需真机 |
| **S6** | 冷却机制 + 失败重试 | 策略层 | ✅ 单测可测 |
| **S7** | `display_name` 必填 + 并发上下文栈 | 注册层 | ✅ 单测可测 |

> **建议 S1→S4 先做**(协议 + 采集 + 门控 + tool),这部分可以完全靠单元测试验证,
> 风险低。S5(prompt)留到最后,等前面的地基稳了再调 prompt,避免 prompt 变了
> 不知道是 prompt 的问题还是代码的问题。

---

## 11. 与「后台静默」方案的取舍留档

如果将来需要回退到后台静默(比如用户明确表示「不要问我,直接录」),
只需把 §6.3 的「Agent 决定要不要问」改成「VoiceAdapter 直接采集」,
其余(两阶段 tool / 门控 / 冷却)都能复用。

---

## 12. 待确认问题 → 已决策

| # | 问题 | 决策 | 落地位置 |
|---|------|------|---------|
| 1 | 确认窗口 5s 够不够? | ✅ **5 秒**(用户拍板 2026-11-01) | `VoiceAdapter._confirm_window_seconds = 5.0` |
| 2 | 逐句播报要不要做? | ✅ **不做**(用户拍板)。采集前播一次 + 采集后播一次 | §5.2 / `voice_service._on_enrollment_complete` |
| 3 | 拒绝后冷却多久? | ✅ **当天**(用户拍板 2026-11-01),即 24 小时 | `VoiceAdapter._consent_cooldown_seconds = 24*3600` |
| 4 | `display_name` 必填是否合理? | ✅ **必填**(Agent 询问时一并问到名字) | `enroll_speaker` 参数校验 |
| 5 | 单句样本要不要入库? | ✅ **入库但打标**(不改阈值,风险更小) | `metadata["low_confidence"]`,阈值 `< 3` 段 |

### 实现时新增的决策(设计稿未覆盖)

| # | 问题 | 决策 | 理由 |
|---|------|------|------|
| 6 | 冷却键用什么? | 陌生人 → **设备级**;已注册 → **用户级** | 陌生人 id 是声纹哈希,跨轮不稳定,用 id 当键会让「当天不再问」静默失效(§3.5) |
| 7 | 谁来记录「用户拒绝」? | 新增 `decline_speaker_enrollment` 工具 | 只靠 prompt 约束 LLM 不够 —— 拒绝状态没人记录,用户会被反复问(§3.5) |
| 8 | 门控放哪一层? | **播放器层** `BaseAudioPlayer.play_gated` | Agent 正常回复走 `TTSOutputAdapter`,不经过 `VoiceAdapter.speak()` —— 放错层等于日常对话完全不设防(§4.2) |
| 9 | 确认窗口先等门控还是边等边听? | **先等门控结束再听** | 边等边听会把管家自己的话当成用户回答,或吃掉用户抢答(§4.2) |
| 10 | 采集上下文怎么存? | **栈**(FIFO),不是单个 dict | 连续两次采集时,后一次会冲掉前一次 → 声纹注册到错的人名 |

---

## 附录 A: 当前代码关键片段索引

| 关注点 | 文件:行 |
|--------|---------|
| 陌生人判定 | `voice/adapter/voice_adapter.py:446-457` |
| 触发采集 | `voice/adapter/voice_adapter.py:499-508` |
| 采集模式入口 | `voice/adapter/voice_adapter.py:730-771` |
| 采集循环 | `voice/adapter/voice_adapter.py:534-630` |
| 双阈值判定 | `events/routing/speaker_registry_embedding.py:243-305` |
| 注册服务 | `events/routing/voiceprint_enrollment.py:140-250` |
| Tool | `capabilities/tools/voice/enroll_speaker.py:122-178` |
| TTS 播放 | `voice/adapter/voice_adapter.py:655-711` |
| 事件协议 | `events/protocol/voice_event.py:49-77` |

---

## 附录 B: 实现记录(2026-11-02)

### 落地的文件

| 文件 | 改动 |
|------|------|
| `events/protocol/voice_event.py` | `VoiceAction.CONFIRM_ANSWER`;`VoiceEvent.suggest_enrollment` / `enrollment_reason`;`to_user_input()` 三条分支(COMMAND 带询问提示 / CONFIRM_ANSWER 带判定指引 / 其他走标准格式) |
| `voice/adapter/voice_adapter.py` | `State.CONFIRMING` / `State.ENROLLING`;`start_confirmation_window()`;冷却三件套(`_consent_cooldown_key` / `_is_in_consent_cooldown` / `mark_consent_rejected`);`_flush_buffer` 改为**只标记不采集**;采集与确认窗口都读 player 门控 |
| `voice/player/base.py` | `BaseAudioPlayer.play_gated()` + `playback_gate_until` + `is_playback_gated()`(门控实现在这一层) |
| `voice/player/winsound_player.py` / `pygame_player.py` | 调 `super().__init__()`;`FakeAudioPlayer` 同 |
| `voice/adapter/tts_output_adapter.py` | `player.play()` → `player.play_gated()`(**Agent 正常回复的回声抑制**) |
| `capabilities/tools/voice/enroll_speaker.py` | `enroll_speaker(confirm=...)` 硬门槛;新增 `decline_speaker_enrollment`;上下文改栈;新增 `peek_pending_context` |
| `capabilities/tools/voice/__init__.py` | 导出新工具 |
| `thinking/prompt/builder.py` | `_VOICEPRINT_ENROLLMENT_GUIDE`(仅在 `enroll_speaker` 可用时注入) |
| `events/routing/voiceprint_enrollment.py` | `LOW_CONFIDENCE_SAMPLE_THRESHOLD` + `metadata["low_confidence"]` |
| `scripts/voice_service.py` | 采集完成后播报「记住了,{name}」/ 样本不足提示 / 失败提示 |

### 测试

| 文件 | 用例数 | 覆盖 |
|------|--------|------|
| `tests/unit/voice/test_consent_flow.py` | 30 | confirm 硬门槛、确认窗口、回声抑制、冷却、只标记不采集、协议、low_confidence、prompt 指引 |
| `tests/integration/voice/test_consent_flow_e2e.py` | 5 | 完整链路(陌生人→建议→确认→同意→采集)、拒绝冷却、工具门槛、门控保护、确认音频不泄漏 |

### 全量回归

```
tests=1007  failures=1  errors=0  skipped=2
```

唯一的 failure 是 `tests/e2e/test_memory_thinking.py::test_thinking_source_isolation_real`
(Bob/Alice 记忆隔离),**改动前就存在**(已在 `v0.3---adapter` 上 stash 验证过),
与本次改动无关。

