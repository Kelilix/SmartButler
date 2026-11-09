"""🆕 2026-11-04: 时间语义抽取(LLM 绝对化)。

设计依据:Zep / Graphiti 论文的 **Temporal Extraction** 步骤
(附录 6.1.5)—— 把"下周四""两周后"这类相对时间,基于 **reference
timestamp** 换算成绝对日期。

我们做的是同一件事,但**不引入 Zep 的四个时间戳**,只落两个:

    spoken_at —— 系统时间:这条事实**什么时候被说出来**(客观,代码取)
    valid_at  —— 世界时间:这件事**本身什么时候成立**(LLM 抽取)

为什么只落两个:
- Zep 有四个:``valid_at`` / ``invalid_at``(世界) + ``created_at`` /
  ``expired_at``(系统)。``invalid_at`` / ``expired_at`` 属于**失效修正**,
  那是图谱阶段的事(第三步),现在不做。
- 我们已有的 ``created_at``(Qdrant payload)承担了 Zep 的 ``created_at``
  角色 —— 事实入库时间。

为什么 valid_at 是**字符串**而不是 datetime:
valid_at 有四种形态,只有第一种是"一个时刻":

    ① 精确点   "2026年1月1号是我领证了"      → ``2026-01-01``
    ② 区间     "下周我想吃龙虾"             → ``2026-11-09/2026-11-15``
    ③ 周期     "1月29号是我生日"            → ``recurring:01-29``
    ④ 永久     "张女士是我朋友"              → ``permanent``

datetime 表达不了 ②③,所以统一编码成字符串。

**LLM 与代码的分工**(延续"代码承担事实,LLM 承担表达"的原则):
- 代码负责:reference timestamp(谁在什么时候说的)、绝对化所需的基准、
  最终写库(代码重写一遍,保证 tag 格式统一)
- LLM 负责:判断"这句话有没有时间语义"、"是哪种形态"、"具体值是什么"

不变量:
- LLM 失败 → 降级为"不写 valid_at"(**不是** permanent)
- 空值 ≠ permanent。"没抽到时间"和"这件事永久成立"是两回事
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from smartbutler.utils.logging import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# LLM prompt
# ---------------------------------------------------------------------------

#: 时间抽取 prompt —— 参考 Zep 附录 6.1.5 的 9 条细则改写。
_TIME_EXTRACT_PROMPT = """你是记忆时间抽取器。给定一句话,判断它描述的事情**在什么时候成立**。

## 参考时间戳
<reference_timestamp>{reference_timestamp}</reference_timestamp>

这句话是「{speaker}」在参考时间戳那一刻说的。

## 你的任务
从这句话里抽取时间语义,输出 JSON。

## 严格规则

1. **只抽取句中明确提及的时间信息**。没有提及时间 → `valid_at` 为 null。
2. **相对时间必须换算成绝对日期**,基准是参考时间戳。
   - "今天" → 参考时间戳的日期
   - "明天"/"后天" → 参考时间戳 + 1天 / + 2天
   - "下周一" → 参考时间戳所在周的**下周**的周一(周一是一周之始)
   - "下个月" → 参考时间戳的下一个自然月
   - "两周后" → 参考时间戳 + 14 天
3. **绝对时间直接用原值**。"2026年1月1号" → `2026-01-01`。
4. **区间用 `起始日/结束日`** 表示,斜杠分隔,**两端都含**(闭区间)。
   - "下周" → 该周的周一到周日,写成 `周一/周日`
   - "这周末" → 周六到周日
   - "明年" → 该年的 1月1日 到 12月31日
5. **周期性事实**用 `recurring:MM-DD` 表示。
   - "1月29号是我生日" → `recurring:01-29`
   - "每年3月体检" → `recurring:03-01`
   - 只有月日(没有年份)→ 说明它**每年都成立**。
6. **没有时间语义的普适事实**用 `permanent`。
   - "张女士是我朋友" → `permanent`
   - "我讨厌香菜" → `permanent`
   判据:这句话描述的是一个**持续成立的状态/关系/偏好**,而不是某时刻发生的事件。
7. **区分"事件"和"状态"**:
   - "我今天去上海" → 事件 → `2026-11-04`(今天的日期)
   - "我住上海" → 状态 → `permanent`
8. **不要从相关事件推断日期**。只抽句中实际提及的时间。
9. **不要编造**。宁可输出 null,也不要猜。
10. **识别"否定/修正"**。如果这句话**终止了某个既有状态或关系**,
    `invalidates` 设为 true。
    - "张女士不再是我朋友了,我们掰了" → `invalidates: true`
    - "我已经不在这家公司工作了" → `invalidates: true`
    - "我搬到上海了" → `invalidates: true`(搬走 = 旧状态终止)
    - "我今天去上海" → `invalidates: false`
    判据:这句话是否让某个**之前成立的状态/关系**不再成立。
    注意:这类话**仍然要正常给 valid_at**(它本身是一条从此刻起生效的新事实)。

## 输出格式(严格 JSON,不要任何解释文字)

精确时间点:
```json
{"valid_at": "2026-01-01", "kind": "point", "invalidates": false}
```

时间范围:
```json
{"valid_at": "2026-11-09/2026-11-15", "kind": "range", "invalidates": false}
```

周期性:
```json
{"valid_at": "recurring:01-29", "kind": "recurring", "invalidates": false}
```

永久事实:
```json
{"valid_at": "permanent", "kind": "permanent", "invalidates": false}
```

否定/修正既有事实:
```json
{"valid_at": "permanent", "kind": "permanent", "invalidates": true}
```

没有时间语义:
```json
{"valid_at": null, "kind": "none", "invalidates": false}
```

## 更多例子

| 句子 | valid_at | kind |
|------|----------|------|
| 2026年1月1号是我领证了 | 2026-01-01 | point |
| 我明天要去上海 | 2026-11-05 | point |
| 下周我想吃龙虾 | 2026-11-09/2026-11-15 | range |
| 1月29号是我生日 | recurring:01-29 | recurring |
| 张女士是我朋友 | permanent | permanent |
| 我喜欢喝拿铁 | permanent | permanent |
| 下周一开会 | 2026-11-09 | point |
| 这个周末去爬山 | 2026-11-07/2026-11-08 | range |
| 我妈下周也去上海 | 2026-11-09/2026-11-15 | range |
"""


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TemporalInfo:
    """一条事实的时间语义抽取结果。

    Attributes:
        valid_at: 世界时间(事件生效时间)的**编码值**。四种形态之一:
            精确点 ``2026-01-01`` / 区间 ``2026-11-09/2026-11-15`` /
            周期 ``recurring:01-29`` / 永久 ``permanent``。
            None = 没有时间语义(不代表永久)。
        kind: ``point`` / ``range`` / ``recurring`` / ``permanent`` / ``none``。
        invalidates: 这条话是否**否定/修正**了某个既有事实。

            🆕 2026-11-04,Step 1 的"历史关系修正"承载字段。

            "张女士不再是我朋友了,我们掰了" → invalidates=True。
            这种话的 ``valid_at`` 通常是"**从现在起**"(permanent 或今天),
            它本身是一条新事实,但语义上**终止**了旧事实。

            **Step 1 只标记,不自动消解** —— 要消解得知道"被否定的是哪条"
            (需要实体消解 + 图,属于 Step 3)。Step 3 接入时用
            ``supersedes`` tag 把新旧对上。
    """

    valid_at: str | None
    kind: str
    invalidates: bool = False

    @property
    def is_permanent(self) -> bool:
        """是否是"永久成立"的事实。"""
        return self.kind == "permanent"

    @property
    def has_time(self) -> bool:
        """是否抽到了可用的时间语义。"""
        return self.valid_at is not None and self.kind != "none"

    @property
    def is_invalidation(self) -> bool:
        """是否是一条"否定/修正"既有事实的话。"""
        return self.invalidates


#: 合法的 kind 值
_VALID_KINDS = frozenset({"point", "range", "recurring", "permanent", "none"})

#: ISO 日期 / 日期时间
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ISO_DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?")
#: 区间:``起始/结束``
_ISO_RANGE_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}(T\d{2}:\d{2}(:\d{2})?)?"
    r"/"
    r"\d{4}-\d{2}-\d{2}(T\d{2}:\d{2}(:\d{2})?)?$"
)
#: 周期:``recurring:MM-DD``
_RECURRING_RE = re.compile(r"^recurring:(\d{2})-(\d{2})$")


# ---------------------------------------------------------------------------
# 抽取器
# ---------------------------------------------------------------------------


class TemporalExtractor:
    """LLM 时间抽取器 —— 把相对时间绝对化。

    用法::

        ex = TemporalExtractor(llm)
        info = await ex.extract(text="下周一开会", speaker="高天禹")
        # info.valid_at == "2026-11-09", info.kind == "point"

    降级:LLM 未注入 / 调用失败 / 输出不可解析 → 返回 ``TemporalInfo(None, "none")``,
    即**不写 valid_at tag**(而不是写 permanent —— 那会把"抽不到"伪装成"永久")。
    """

    def __init__(
        self,
        llm: Any | None = None,
        *,
        max_chars: int = 2000,
        timeout_seconds: float = 30.0,
        max_tokens: int = 200,
    ) -> None:
        """Args:
            llm: LLM 实例;None = 不调 LLM(永远返回 none)。
            max_chars: 送进 prompt 的文本上限。
            timeout_seconds: 单次调用超时。
            max_tokens: LLM 输出上限(只要一小段 JSON)。
        """
        self._llm = llm
        self._max_chars = max_chars
        self._timeout_seconds = timeout_seconds
        self._max_tokens = max_tokens

    @property
    def enabled(self) -> bool:
        """是否启用了 LLM。"""
        return self._llm is not None

    async def extract(
        self,
        text: str,
        *,
        speaker: str = "",
        reference_time: datetime | None = None,
    ) -> TemporalInfo:
        """抽取一段文本的时间语义。

        Args:
            text: 原文(通常是 fact 正文)。
            speaker: 说话人(仅用于 prompt 上下文,不影响抽取)。
            reference_time: **reference timestamp** —— 这段话被说出来的时刻。
                相对时间("明天")以此为基准换算。None = 用当前 UTC 时间。

        Returns:
            :class:`TemporalInfo`。失败时 ``valid_at=None, kind="none"``。
        """
        if self._llm is None:
            return TemporalInfo(None, "none")
        content = (text or "").strip()
        if not content:
            return TemporalInfo(None, "none")

        ref = reference_time or datetime.now(UTC)
        if ref.tzinfo is None:
            ref = ref.replace(tzinfo=UTC)

        # ⚠️ 不能用 ``.format()`` —— prompt 里有大量 JSON 示例(含 ``{}``),
        # format 会把它们当成占位符,直接抛 KeyError。
        # 所以用显式的字符串替换。
        prompt = _TIME_EXTRACT_PROMPT.replace(
            "{reference_timestamp}", ref.isoformat()
        ).replace("{speaker}", speaker or "用户")
        # 正文放在最后,避免被前面的模板稀释
        user_msg = f"{prompt}\n\n## 待抽取的句子\n{content[: self._max_chars]}"

        try:
            import asyncio

            from smartbutler.capabilities.llm.types import Message

            resp = await asyncio.wait_for(
                self._llm.chat(
                    [
                        Message(
                            role="system",
                            content=(
                                "你只做时间抽取,输出严格 JSON,不要任何解释文字。"
                            ),
                        ),
                        Message(role="user", content=user_msg),
                    ],
                    temperature=0.0,
                    max_tokens=self._max_tokens,
                ),
                timeout=self._timeout_seconds,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "temporal.extract_failed err=%s text=%s", e, content[:80],
            )
            return TemporalInfo(None, "none")

        raw = (getattr(resp, "content", "") or "").strip()
        parsed = _parse_temporal_json(raw)
        if parsed is None:
            logger.warning(
                "temporal.bad_response raw=%s text=%s", raw[:150], content[:80],
            )
            return TemporalInfo(None, "none")

        kind = parsed.get("kind", "none")
        valid_at = parsed.get("valid_at")
        if kind not in _VALID_KINDS:
            kind = "none"
        if valid_at is not None:
            valid_at = str(valid_at).strip()
            if not valid_at:
                valid_at = None

        # 校验编码格式(防止 LLM 编出乱七八糟的值)
        if valid_at is not None and not _is_valid_valid_at(valid_at, kind):
            logger.warning(
                "temporal.invalid_valid_at value=%r kind=%s → 丢弃", valid_at, kind,
            )
            valid_at = None
            kind = "none"

        if kind == "none":
            valid_at = None
        # permanent 必须显式带 permanent 标记
        if kind == "permanent":
            valid_at = "permanent"

        # 🆕 invalidates —— 这条话是否否定/修正了某个既有事实。
        #
        # 容错:LLM 可能返回 "true"/"yes"/1 等多种真值表示。
        invalidates = _coerce_bool(parsed.get("invalidates"))

        logger.info(
            "temporal.extracted kind=%s valid_at=%s invalidates=%s text=%s",
            kind, valid_at, invalidates, content[:60],
        )
        return TemporalInfo(
            valid_at=valid_at, kind=kind, invalidates=invalidates,
        )


# ---------------------------------------------------------------------------
# 解析 / 校验
# ---------------------------------------------------------------------------


def _parse_temporal_json(raw: str) -> dict[str, Any] | None:
    """从 LLM 输出里抠出 JSON 对象(容错围栏 / 前后解释文字)。"""
    if not raw:
        return None
    text = raw.strip()
    # 剥 markdown 围栏
    text = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", text)
    text = re.sub(r"\s*```$", "", text).strip()

    # 1) 直接试
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except (json.JSONDecodeError, ValueError):
        pass

    # 2) 抠第一个 {...} 平衡括号段
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                seg = text[start : i + 1]
                seg = re.sub(r",\s*([}\]])", r"\1", seg)
                try:
                    obj = json.loads(seg)
                    return obj if isinstance(obj, dict) else None
                except (json.JSONDecodeError, ValueError):
                    return None
    return None


def _coerce_bool(value: Any) -> bool:
    """把 LLM 返回的"真值表示"统一成 bool。

    LLM 可能返回 ``true`` / ``"true"`` / ``"yes"`` / ``1`` / ``"是"``。
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"true", "yes", "y", "1", "是", "对的"}
    return False


def _is_valid_valid_at(value: str, kind: str) -> bool:
    """校验 valid_at 编码值与 kind 是否匹配。

    防止 LLM 编出非法值(比如 ``"待定"`` / ``"null"`` / ``"下周一"``)。
    """
    if kind == "permanent":
        return value == "permanent"
    if kind == "recurring":
        return bool(_RECURRING_RE.match(value))
    if kind == "point":
        return bool(_ISO_DATE_RE.match(value) or _ISO_DATETIME_RE.match(value))
    if kind == "range":
        return bool(_ISO_RANGE_RE.match(value))
    return False


def parse_recurring_md(value: str) -> tuple[int, int] | None:
    """解析 ``recurring:MM-DD`` → ``(month, day)``。不合法返回 None。"""
    m = _RECURRING_RE.match((value or "").strip())
    if not m:
        return None
    month, day = int(m.group(1)), int(m.group(2))
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return None
    return month, day


def parse_range(value: str) -> tuple[str, str] | None:
    """解析 ``起始/结束`` 区间 → ``(start_iso, end_iso)``。不合法返回 None。"""
    v = (value or "").strip()
    if "/" not in v:
        return None
    start, _, end = v.partition("/")
    start, end = start.strip(), end.strip()
    if not start or not end:
        return None
    if not (_ISO_DATE_RE.match(start) or _ISO_DATETIME_RE.match(start)):
        return None
    if not (_ISO_DATE_RE.match(end) or _ISO_DATETIME_RE.match(end)):
        return None
    return start, end


def is_valid_at_active(
    valid_at: str,
    *,
    at: datetime | None = None,
    now: datetime | None = None,
) -> bool:
    """判断某条事实在 ``at`` 时刻是否**当前成立**。

    用于"当前状态"类查询:管家回答"我现在在哪上班""我现在还跟张三是朋友吗"
    时,按 valid_at 过滤出**当前有效**的事实。

    规则:
    - ``permanent`` → 永远成立
    - 精确点 → 该点**已经到达**才算成立(事件已发生)
    - 区间   → 落在区间内
    - ``recurring:MM-DD`` → **该月日每年都成立**(生日不是"只在生日那天成立")
    - 空/不合法 → True(不因为时间信息缺失就误杀)

    ⚠️ **"还没发生" 不等于 "已过期"**:
    - 未来精确点("我明天去上海"→ ``2026-11-05``)→ 现在还没到 → **不成立**
    - 过去的区间(已过期的出差)→ 已经过去了 → **不成立**
    两者都是 False,但**语义完全不同** —— 渲染时必须区分
    ("尚未生效" vs "当前已不适用"),否则 LLM 会把未来计划当成已作废的事。

    Args:
        valid_at: valid_at 编码值。
        at: 判断基准时刻(默认 now)。
        now: 当前时刻(用于 recurring 的"今年"判断)。

    Returns:
        True = 当前成立。
    """
    v = (valid_at or "").strip()
    if not v:
        return True

    ref = at or now or datetime.now(UTC)
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=UTC)

    if v == "permanent":
        return True

    rec = parse_recurring_md(v)
    if rec is not None:
        # 🆕 修正:周期事实是"每年这一天成立",**不是"只有这一天成立"**。
        #
        # "1月29号是我生日" → recurring:01-29
        #   它表达的是"每年 1 月 29 日都是我的生日" —— 12 月问的时候
        #   这个事实依然为真(只是下一个生日还没到)。
        #   旧实现要求 ref.month/day 精确匹配,导致：
        #   · 12 月问"我什么时候生日" → 判为"不成立" → LLM 看到
        #     "每年 01-29 生效，当前已不适用" → 荒谬。
        #   · 生日当天的"今天是我生日"反而只在 1/29 那天成立。
        #
        # 正确语义:**月日匹配 = 成立**(当天正是这个周年日);
        # **月日不匹配 = 依然成立**(它是每年重复的规律,只是下次还没到)。
        #
        # 所以 recurring **永远成立**(active)。真正的"当前不适用"
        # 要靠 invalid_at / supersedes 来表达,不是靠 recurring 本身。
        return True

    rng = parse_range(v)
    if rng is not None:
        start, end = rng
        return _iso_in_closed_range(ref, start, end)

    # 精确点:事件已发生即成立
    if _ISO_DATE_RE.match(v):
        return ref.date() >= _parse_date(v)
    if _ISO_DATETIME_RE.match(v):
        return ref >= _parse_datetime(v)

    # 未知格式 → 不误杀
    return True


def is_valid_at_pending(
    valid_at: str,
    *,
    at: datetime | None = None,
    now: datetime | None = None,
) -> bool:
    """判断某条事实是否**尚未生效**(在 ``at`` 时刻还没到)。

    🆕 2026-11-04。与 :func:`is_valid_at_active` 互补。

    为什么需要区分:
    - "我明天去上海" → valid_at=2026-11-05,今天 11-04 → **尚未生效**
    - "我那阵子在杭州出差" → valid_at=2026-01-01/2026-01-31 → **已过期**
    两者 ``is_valid_at_active`` 都是 False,但对 LLM 的意义相反:
    前者是"计划中的事",后者是"过去的事"。渲染时给错标签会让 LLM
    把用户明天的安排当成"已经作废"。

    判定:
    - 精确点在 ``at`` 之后 → 尚未生效
    - 区间的开始日在 ``at`` 之后 → 尚未生效
    - 其余(permanent / recurring / 已过期的区间 / 空)→ False

    Args:
        valid_at: valid_at 编码值。
        at: 判断基准时刻(默认 now)。
        now: 当前时刻。

    Returns:
        True = 尚未生效。
    """
    v = (valid_at or "").strip()
    if not v:
        return False

    ref = at or now or datetime.now(UTC)
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=UTC)

    if v == "permanent":
        return False
    if parse_recurring_md(v) is not None:
        # 周期事实没有"开始"的概念 —— 它每年都成立,谈不上"尚未生效"。
        return False

    rng = parse_range(v)
    if rng is not None:
        start, _end = rng
        # 区间还没开始 → 尚未生效
        return not _iso_le(start, ref)

    if _ISO_DATE_RE.match(v):
        return ref.date() < _parse_date(v)
    if _ISO_DATETIME_RE.match(v):
        return ref < _parse_datetime(v)

    return False


def _parse_date(value: str) -> Any:
    """解析 ``YYYY-MM-DD`` → date。失败返回 date.min。"""
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        from datetime import date as _date

        return _date.min


def _parse_datetime(value: str) -> datetime:
    """解析 ISO datetime(兼容 ``Z`` 后缀)。失败返回 UTC 纪元。"""
    s = value.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return datetime(1970, 1, 1, tzinfo=UTC)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _iso_le(iso: str, ref: datetime) -> bool:
    """``iso`` 表示的时刻 ≤ ``ref``。"""
    if _ISO_DATE_RE.match(iso):
        return _parse_date(iso) <= ref.date()
    return _parse_datetime(iso) <= ref


def _iso_ge(iso: str, ref: datetime) -> bool:
    """``iso`` 表示的时刻 ≥ ``ref``。"""
    if _ISO_DATE_RE.match(iso):
        return _parse_date(iso) >= ref.date()
    return _parse_datetime(iso) >= ref


def _iso_in_closed_range(ref: datetime, start: str, end: str) -> bool:
    """判断 ``ref`` 是否落在 **闭区间** ``[start, end]`` 内。

    🆕 2026-11-04:区间语义统一为**闭区间**(两端都含)。

    为什么要改:
    - prompt 告诉 LLM "下周 → 周一/周日(含结束日)",LLM 会照着写
    - 旧实现却是右开(``ref < end``)→ 周日当天被判为"不在区间内"
    - 结果:"这个周末去爬山"(11-07/11-08)在周日当天会显示"当前已不适用"
      —— 荒谬,且与 LLM 的理解不一致。

    两端都含还有个好处:区间是**日期粒度**时,"结束日当天整天都算在内",
    语义更自然(出差到 1 月 31 日,1 月 31 日当天还在出差)。

    Args:
        ref: 判断基准时刻。
        start: 起始(ISO 日期/时间)。
        end: 结束(ISO 日期/时间)。

    Returns:
        True = 落在闭区间内(即 ``start <= ref <= end``)。
    """
    # ⚠️ 方向别搞反:
    #   _iso_le(iso, ref)  == iso <= ref
    #   _iso_ge(iso, ref)  == iso >= ref
    # 要判定的"start <= ref <= end" 是:
    #   start <= ref  → _iso_le(start, ref)
    #   ref <= end   → _iso_ge(end, ref)
    return _iso_le(start, ref) and _iso_ge(end, ref)


__all__ = [
    "TemporalExtractor",
    "TemporalInfo",
    "parse_recurring_md",
    "parse_range",
    "is_valid_at_active",
    "is_valid_at_pending",
]
