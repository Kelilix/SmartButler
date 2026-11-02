"""声纹注册 Tool（Phase 7.6，2026-11-01 重构为单阶段非阻塞）。

## 🆕 2026-11-01 重构:从"两阶段"改为"单阶段 + 后台自动注册"

### 旧设计的问题

原设计是两阶段:

    enroll_speaker(display_name="妈妈")        → "请说 3-5 句话"  (启动采集)
    enroll_speaker(display_name="妈妈", confirm=True)  → 保存声纹

第二阶段**依赖 LLM 自己判断"用户说完了"**,再去调一次。这有两个硬伤:

1. **时序脆弱**:LLM 凭什么知道用户说完了?说3句还是5句?要不要再等等?
   全靠 LLM 自由发挥。
2. **可能永远走不到**:采集期间 VoiceAdapter **不发布 VoiceEvent**
   (采集是静默的),所以用户说完那几句话之后,LLM **根本收不到任何事件**,
   自然也不会想起去调 confirm=True → 声纹永远存不下来。

### 新设计

    enroll_speaker(display_name="妈妈")  → 启动采集,**立即返回**(不等采集完成)
                                          LLM 当场就能回复用户
    ↓ (后台)
    采集够 3 句 → VoiceAdapter 触发回调 → VoiceprintEnrollmentService
                  → 自动判断"更新 or 新建" → 落库

用户决策:**"按A,LLM不卡,后台进行"** + **"按A,自动注册"**。

所以本 tool 只负责**启动采集**,不再负责保存 —— 保存由
:class:`VoiceprintEnrollmentService` 在后台完成。

## 🆕 2026-11-01 (Phase 7.6.1) 再改造:加同意硬门槛

上面那版虽然解决了"LLM 不卡",但带来一个**更严重的问题**:
**用户完全不知情就被录音**。

陌生人(声纹库匹配不上)一开口,系统就静默开始采集 —— 没有询问、
没有同意、没有命名。相比"自动注册"这个技术收益,这是**隐私风险**,
而且采到的可能是电视声/旁人/噪声。

原始设计(`docs/PHASE_7_6_VOICE_ENROLLMENT_DESIGN.md` §3.2)本来画的是
"LLM 主动询问 → 用户回答好/不用了 → 进入采集",实现阶段走偏了。
本次改造**拉回原始设计**,并补齐原始设计缺的三块拼图:

| 拼图 | 落地位置 |
|------|---------|
| 同意机制 | 本 tool 的 `confirm` 硬门槛 + VoiceAdapter 的 CONFIRMING 窗口 |
| 回声抑制 | VoiceAdapter 的 `_playback_gate_until`(播放期不录音) |
| 命名 | `display_name` 由 Agent 在询问时一并问到,不再默认"陌生人" |

### 新流程

    陌生人说话 → VoiceEvent(suggest_enrollment=True)
      ↓ Agent 询问:"方便告诉我你的名字、让我记住你的声音吗?"
      ↓ TTS 播放(播放期回声抑制)
      ↓ CONFIRMING 窗口(5s)→ VoiceEvent(CONFIRM_ANSWER)
      ↓ Agent 判定:
         同意 → enroll_speaker(display_name="小明", confirm=True) → 采集
         拒绝 → 不调用 + mark_consent_rejected(当天不再问)
         沉默 → 超时,不调用 + 记冷却

    enroll_speaker(display_name="小明")              → consent_required(不采集)
    enroll_speaker(display_name="小明", confirm=True) → collecting(启动采集)

⚠️ **`confirm=False` 永不启动采集**。这是刻意的保守设计:
宁可漏采集,不可未经同意采集。详见函数 docstring。

Author: SmartButler Team
Date: 2026-10-16 / 2026-11-01
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from smartbutler.capabilities.tools.decorator import register_tool
from smartbutler.capabilities.tools.types import ToolScope

if TYPE_CHECKING:
    from smartbutler.events.routing.speaker_registry import SpeakerRegistry
    from smartbutler.voice.adapter.voice_adapter import VoiceAdapter

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 全局依赖注入（由 VoiceService 启动时设置）
# ---------------------------------------------------------------------------

# 全局 VoiceAdapter 实例（由 voice_service.py 初始化时注入）
_voice_adapter: "VoiceAdapter | None" = None

# 全局 SpeakerRegistry 实例（由 voice_service.py 初始化时注入）
_speaker_registry: "SpeakerRegistry | None" = None

# 🆕 2026-11-01: 本次采集对应的人名/已知 user_id。
# VoiceAdapter 的采集回调是通用的(不知道人名),所以把上下文存在这里,
# 由 voice_service 的回调读取并传给 VoiceprintEnrollmentService。
#
# 🆕 2026-11-01 (Phase 7.6.1): 改成**栈**。
#
# 为什么:原来是个单 dict,新采集会 clear() 掉旧的。如果上一个采集的
# 回调还没跑完(比如还在等 embedding 提取),上下文就被覆盖了 →
# 声纹注册到错的人名。
_pending_contexts: list[dict[str, Any]] = []


def set_voice_adapter(adapter: "VoiceAdapter") -> None:
    """由 VoiceService 初始化时调用，注入 VoiceAdapter 实例。"""
    global _voice_adapter
    _voice_adapter = adapter


def set_speaker_registry(registry: "SpeakerRegistry") -> None:
    """由 VoiceService 初始化时调用，注入 SpeakerRegistry 实例。"""
    global _speaker_registry
    _speaker_registry = registry


def set_pending_context(
    *, display_name: str | None = None, user_id: str | None = None
) -> None:
    """记录本次采集的人名 / 已知 user_id(2026-11-01)。

    采集回调在后台执行,拿不到 tool 的参数,所以通过这个全局上下文传递。

    🆕 2026-11-01 (Phase 7.6.1): 改成**压栈**而不是覆盖,
    避免连续两次采集时后一次把前一次的上下文冲掉。

    Args:
        display_name: 人名(如"妈妈")。
        user_id: 已知 user_id(重新采集时传入 → 保证保留原 id)。
    """
    ctx: dict[str, Any] = {}
    if display_name:
        ctx["display_name"] = display_name
    if user_id:
        ctx["user_id"] = user_id
    _pending_contexts.append(ctx)


def take_pending_context() -> dict[str, Any]:
    """取出并清空采集上下文(供回调使用)。

    🆕 2026-11-01 (Phase 7.6.1): 出栈(FIFO)。栈空时返回空 dict。
    """
    if not _pending_contexts:
        return {}
    return _pending_contexts.pop(0)


def peek_pending_context() -> dict[str, Any]:
    """查看当前待处理的采集上下文(不弹出)。"""
    if not _pending_contexts:
        return {}
    return dict(_pending_contexts[0])


# ---------------------------------------------------------------------------
# Tool 实现
# ---------------------------------------------------------------------------


@register_tool(
    name="enroll_speaker",
    description=(
        "建立声纹档案(记住某个人的声音)。\n"
        "【硬性前提:必须先取得用户明确同意】\n"
        "  - 用户说'要'/'好'/'可以'等表示同意后,才调用本工具,并传 confirm=true。\n"
        "  - 用户拒绝或没有明确同意时,**严禁**调用本工具。\n"
        "调用后系统会采集 3 句话,采集完成自动保存,之后就能通过声纹识别出这个人。\n"
        "请立刻告诉用户'请说 3 句话让我记住你的声音',不要等待采集完成。"
    ),
    scope=ToolScope.GLOBAL,
    timeout_seconds=30.0,
    idempotent=False,
)
async def enroll_speaker(
    display_name: str,
    user_id: str | None = None,
    confirm: bool = False,
) -> dict[str, Any]:
    """启动声纹采集(注册在后台自动完成)。

    🆕 2026-11-01 (Phase 7.6.1): 增加 ``confirm`` 硬门槛。

    ## 为什么要有这个参数

    改造前是**后台静默采集**:陌生人一开口,系统直接开始录音,
    用户完全不知情 —— 这是隐私问题(未经同意处理生物特征)。

    现在改成:Agent 主动询问 → 用户明确同意 → 才调本工具。

    但**光靠 prompt 约束 LLM 不可靠** —— LLM 可能忘记传 confirm,
    或者误判用户的回答。所以代码侧加一道硬门槛:

    ``confirm=False`` → **不启动采集**,直接返回 ``consent_required``。

    这条是刻意的保守设计:**宁可漏采集,不可未经同意采集**。

    Args:
        display_name: 用户自称的名字，如"妈妈"、"爸爸"、"奶奶"。
        user_id: 可选，已知 user_id。重新采集已注册的人时传入，
            可保证更新声纹时**保留原 user_id**（人物表绑定不断链）。
        confirm: 用户是否已**明确同意**录入声纹。必须为 True 才会启动采集。

    Returns:
        dict: 含 status / message / instruction 等字段。
        status="consent_required" → 没有明确同意,未启动采集。
        status="collecting"       → 已启动采集,等待用户说话。
        status="busy"             → 上一次采集还没结束。
    """
    if _speaker_registry is None:
        return {
            "status": "error",
            "message": "声纹注册功能未初始化，请联系管理员。",
        }

    if not display_name or not display_name.strip():
        return {
            "status": "error",
            "message": "display_name 不能为空，请提供你的名字（如'妈妈'、'爸爸'）。",
        }

    if _voice_adapter is None:
        return {
            "status": "error",
            "message": "语音采集功能未初始化，无法采集声纹。",
        }

    # 🆕 2026-11-01 (Phase 7.6.1): 同意硬门槛。
    #
    # 不信 LLM 的口头同意,只认这个显式参数。没传 confirm=True 就不采。
    if not confirm:
        logger.warning(
            "🚫 enroll_speaker 被调用但 confirm=False,拒绝启动采集"
            "(用户未明确同意,或 LLM 忘记传 confirm=true)",
        )
        return {
            "status": "consent_required",
            "message": (
                "尚未获得用户明确同意,不能采集声纹。"
                "请先询问用户是否愿意被记住声音,得到明确同意后再调用本工具"
                "(confirm=true)。"
            ),
            "instruction": "need_consent",
        }

    # 🆕 2026-11-01 (Phase 7.6.1): 采集是单通道,上一次没结束就不接受新的。
    if getattr(_voice_adapter, "_enrollment_mode", False):
        logger.warning("🚫 上一次声纹采集尚未结束,拒绝启动新的采集")
        return {
            "status": "busy",
            "message": "正在采集声纹,请等这次采集完成后再试。",
            "instruction": "busy",
        }

    # 记录上下文(后台回调要用)
    set_pending_context(display_name=display_name.strip(), user_id=user_id)

    # 启动 VoiceAdapter 采集模式。
    # ⚠️ 2026-11-01:start_voice_enrollment 现在是**非阻塞**的 ——
    # 它只切 _enrollment_mode,主循环下一轮自然走进采集分支。
    # 之前它会置 _running=False,直接把主循环干掉,采集永远执行不到。
    await _voice_adapter.start_voice_enrollment()

    logger.info(
        "🎙️ 声纹采集已启动(display_name=%s user_id=%s),采集完成后自动注册",
        display_name,
        user_id,
    )

    return {
        "status": "collecting",
        "message": (
            f"好的 {display_name}，请说 3 句话让我记住你的声音。\n"
            "每句说 5-10 秒，句与句之间停顿一下。\n"
            "说完我会自动处理好。"
        ),
        "display_name": display_name.strip(),
        "instruction": "collecting",
    }


@register_tool(
    name="decline_speaker_enrollment",
    description=(
        "用户**拒绝**登记声纹时调用。\n"
        "调用后系统会记住:今天不再询问这个人是否愿意登记声纹。\n"
        "调用后请礼貌地回应(如'好的,那就不打扰你了'),不要追问原因。"
    ),
    scope=ToolScope.GLOBAL,
    timeout_seconds=10.0,
    idempotent=True,
)
async def decline_speaker_enrollment(
    user_id: str | None = None,
    reason: str = "",
) -> dict[str, Any]:
    """记录用户拒绝录入声纹 → 当天不再询问。

    🆕 2026-11-01 (Phase 7.6.1): 同意机制的**另一半**。

    ## 为什么需要这个工具

    只靠 prompt 告诉 LLM"用户拒绝时不要调用 enroll_speaker"是不够的 ——
    **拒绝这个状态没人记录**。结果是:用户说了"不用",LLM 确实没采集,
    但下一轮陌生人又出现时,系统又去问一遍 —— 用户被反复打扰。

    用户决策:"拒绝后当天不询问" → 必须有人把这个"拒绝"记下来。

    这个工具就是那个"记下来"的动作。

    Args:
        user_id: 被询问的说话人 id(通常来自 VoiceEvent.user_id)。
            陌生人 id 是临时哈希,所以**留空也可以** —— 系统会退化成
            "按设备冷却"(这台设备今天不再问任何人)。
        reason: 拒绝原因(可选,仅用于日志/观测)。

    Returns:
        dict: 含 status / message。
    """
    if _voice_adapter is None:
        return {
            "status": "error",
            "message": "语音模块未初始化。",
        }

    target = (user_id or "").strip()
    # user_id 留空 → mark_consent_rejected("") 内部按设备冷却
    _voice_adapter.mark_consent_rejected(target)

    logger.info(
        "🚫 记录声纹登记被拒: user_id=%s reason=%s",
        target or "(按设备冷却)",
        reason or "(未说明)",
    )

    return {
        "status": "declined",
        "message": "已记录,今天不会再询问。",
        "instruction": "declined",
    }


__all__ = [
    "enroll_speaker",
    "decline_speaker_enrollment",
    "set_voice_adapter",
    "set_speaker_registry",
    "set_pending_context",
    "take_pending_context",
    "peek_pending_context",
]
