"""5W1H 提炼 + LLM 摘要(Phase 6.3 P1)。

设计要点:
1. **5W1H 适配版**(2026-10-10 决策,不全照搬):
   - what / when / how(what_changed) 必填
   - where / who 可省(无信息就不写,不要写"unknown")
   - why **必须用户原话引述** —— LLM 不准编"为什么"
2. **LLM 摘要降级** —— 调 LLM 失败时,用启发式摘要(取前 200 字符 + 标签拼接)。
3. **why 严格校验** —— LLM 输出含"用户可能..."/"他大概..."等推断语 → 丢弃 why 字段。
4. **本地启发式** —— 不调 LLM,直接 ``"用户提到: " + content[:200]``。

不变量:
- 缺必填字段时,该 fact 不进 consolidation,降级为 raw 短期记忆
- why 永远是字符串(可能为空),不返回 None
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from smartbutler.utils.logging import get_logger

logger = get_logger(__name__)


# LLM 编"为什么" 的典型推断语(用于降级校验)
_INFERENCE_PATTERNS = re.compile(
    r"(用户可能|他大概|可能是因为|也许是因为|看来是|应该是|似乎是|估计是|或许是)"
)

# markdown 代码块围栏(```json ... ``` / ``` ... ```)
_CODE_FENCE_RE = re.compile(r"^```[a-zA-Z0-9_-]*\s*|\s*```$")


def _strip_code_fence(text: str) -> str:
    """剥掉 markdown 代码块围栏,只留正文。

    LLM 经常无视"不要代码块围栏"的指令直接包 ```json,注入 prompt 时
    这些围栏是纯噪音。
    """
    return _CODE_FENCE_RE.sub("", text).strip()


def _parse_json_object(text: str) -> dict[str, Any] | None:
    """从 LLM 输出里抠出第一个 JSON 对象。

    容错:围栏包裹、前后有解释文字、尾部逗号都尝试修。
    抠不出返回 None(调用方降级)。
    """
    text = _strip_code_fence(text)
    if not text:
        return None

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
                seg = text[start:i + 1]
                # 修尾逗号 {"a":1,}
                seg = re.sub(r",\s*([}\]])", r"\1", seg)
                try:
                    obj = json.loads(seg)
                    return obj if isinstance(obj, dict) else None
                except (json.JSONDecodeError, ValueError):
                    return None
    return None


@dataclass
class FiveW1H:
    """5W1H 提炼结果(2026-10-10 决策适配版)。

    - what: 必填
    - when: 必填(可空字符串表示"未指明时间")
    - how / what_changed: 必填
    - where: 可省
    - who: 可省
    - why: 用户原话引述,仅在用户明确说了时填
    """

    what: str
    when: str
    what_changed: str
    where: str = ""
    who: str = ""
    why: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "what": self.what,
            "when": self.when,
            "where": self.where,
            "who": self.who,
            "what_changed": self.what_changed,
            "why": self.why,
        }

    def is_valid(self) -> bool:
        """必填字段非空 = 有效。"""
        return bool(self.what and self.when and self.what_changed)

    def render(self) -> str:
        """渲染成 markdown 摘要(供 recall 输出)。"""
        lines = [
            f"**事件**:{self.what}",
            f"**时间**:{self.when}",
        ]
        if self.where:
            lines.append(f"**地点**:{self.where}")
        if self.who:
            lines.append(f"**人物**:{self.who}")
        if self.what_changed:
            lines.append(f"**变化**:{self.what_changed}")
        if self.why:
            lines.append(f"**原话**:`{self.why}`")
        return "\n".join(lines)


@dataclass
class SummarizedFact:
    """提炼 + 摘要的最终结果,供 consolidation 写入长期存储。"""

    content: str                # 原始 content(原文)
    summary: str                # 提炼后的 markdown 摘要
    importance: float           # 重要性 0-1
    tags: list[str] = field(default_factory=list)
    five_w1h: FiveW1H | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "content": self.content,
            "summary": self.summary,
            "importance": self.importance,
            "tags": list(self.tags),
            "five_w1h": self.five_w1h.to_dict() if self.five_w1h else None,
        }


class FiveW1HExtractor:
    """5W1H 提炼器。

    设计:核心逻辑是启发式从原文抽 6 字段(正则 + 长度截断)。
    LLM 增强版留给 consolidation(LLMSummarizer)调用,本类不做 LLM 调用,纯本地。
    """

    # 时间模式:具体日期 / 时间 / 相对时间
    _RE_TIME = re.compile(
        r"(\d{1,2}月\d{1,2}日|\d{4}-\d{1,2}-\d{1,2}|\d{1,2}/\d{1,2}|"
        r"\d{1,2}:\d{2}|"
        r"(今天|明天|后天|明早|明晚|今晚|昨晚|今早|今晚上午|下午|晚上)|"
        r"(下周|下个月|下月|今年|明年))"
    )
    # 地点关键词
    _RE_WHERE = re.compile(r"在([\u4e00-\u9fa5A-Za-z]{2,10})(开会|住|上班|出差|旅行|旅游|吃饭)")
    # 人物关键词("Alice 说的"/"Bob 喜欢")
    _RE_WHO = re.compile(r"([A-Z][a-zA-Z]+|[\u4e00-\u9fa5]{2,3})(说|告诉|跟|喜欢|想|要)")
    # 用户原话引述
    _RE_QUOTE = re.compile(r"[\"「『'\"'](.+?)[\"」』'\"']")

    def extract(self, content: str) -> FiveW1H:
        """启发式从原文抽 5W1H。

        Args:
            content: 原文。

        Returns:
            :class:`FiveW1H`(必填字段可能为空,调用方用 ``is_valid()`` 判断)。
        """
        if not content or not content.strip():
            return FiveW1H(what="", when="", what_changed="")

        # what: 整句(L1 daily 场景下,整句比截断到 100 字更有用)
        what = content.strip()

        # when: 抽时间模式
        m = self._RE_TIME.search(content)
        when = m.group(0) if m else ""

        # where: 抽地点
        m = self._RE_WHERE.search(content)
        where = m.group(1) if m else ""

        # who: 抽人物
        m = self._RE_WHO.search(content)
        who = m.group(1) if m else ""

        # what_changed: 启发式 = 整句(L1 daily 阶段 what == what_changed)
        what_changed = what

        # why: 抽原话引述
        m = self._RE_QUOTE.search(content)
        why = m.group(1) if m else ""

        return FiveW1H(
            what=what,
            when=when,
            where=where,
            who=who,
            what_changed=what_changed,
            why=why,
        )


class LLMScorer:
    """LLM 评分器 —— 对 consolidation 候选 fact 批评 0-1 分。

    设计:
    - 评分维度:
      - 0.0-0.2: 日常琐事(开灯/调温度等无后续影响)
      - 0.2-0.5: 有时间/数字的事实(8 点烧洗澡水)
      - 0.5-0.8: 任务/约定(下周出差/周末聚餐)
      - 0.8-1.0: 持久偏好/关系/约定(用户长期习惯/家庭成员信息)
    - **降级**: LLM 失败 / 未注入 → 返回 None,调用方走启发式兜底

    🆕 2026-10-12:接入真实 LLM(此前是 ``return None`` 的桩,导致
    consolidation 全程走启发式 importance)。``llm`` 为 None 时行为不变。
    """

    #: 评分 prompt —— 只输出 JSON,禁止解释性文字
    _SCORE_PROMPT = (
        "你是记忆重要性评分器。给下面这条记忆打 0-1 的重要性分。\n"
        "评分标准:\n"
        "- 0.0-0.2 日常琐事(开灯/调温度/打招呼),无后续影响\n"
        "- 0.2-0.5 有时间或数字的事实\n"
        "- 0.5-0.8 任务、约定、计划\n"
        "- 0.8-1.0 持久偏好、关系、身份信息\n"
        "\n"
        "只输出 JSON,不要任何解释:\n"
        '{"importance": 0.X, "reason": "一句话理由"}'
    )

    def __init__(self, llm: "Any | None" = None, *, max_chars: int = 2000) -> None:
        """Args:
            llm: :class:`~smartbutler.capabilities.llm.base.BaseLLM` 实例;
                None = 不调 LLM(降级到调用方的启发式 importance)。
            max_chars: 送进 prompt 的 fact 内容上限,防止超长撑爆上下文。
        """
        self._llm = llm
        self._max_chars = max_chars

    @property
    def enabled(self) -> bool:
        return self._llm is not None

    async def score_batch(
        self, facts: list[dict[str, Any]]
    ) -> list[dict[str, Any]] | None:
        """对一批 fact 评分。

        Args:
            facts: ``[{"key": ..., "content": ..., "current_importance": ...}, ...]``

        Returns:
            ``[{"key": ..., "importance": 0.X, "reason": "..."}, ...]`` 顺序对齐输入。
            未注入 LLM / 调用失败 → 返回 None(调用方走启发式)。
        """
        if self._llm is None or not facts:
            return None

        from smartbutler.capabilities.llm.types import Message

        out: list[dict[str, Any]] = []
        for f in facts:
            content = str(f.get("content", ""))[: self._max_chars]
            user_msg = (
                f"当前启发式分数:{f.get('current_importance', f.get('importance', 0.5))}\n"
                f"记忆内容:\n{content}"
            )
            try:
                resp = await self._llm.chat(
                    [
                        Message(role="system", content=self._SCORE_PROMPT),
                        Message(role="user", content=user_msg),
                    ],
                    temperature=0.0,
                    max_tokens=200,
                )
                parsed = _parse_json_object(getattr(resp, "content", "") or "")
                if not parsed or "importance" not in parsed:
                    logger.warning("LLMScorer.bad_response content=%r",
                                   (getattr(resp, "content", "") or "")[:120])
                    out.append({"key": f.get("key"), "importance": None})
                    continue
                score = float(parsed["importance"])
                score = max(0.0, min(1.0, score))
                out.append({
                    "key": f.get("key"),
                    "importance": score,
                    "reason": str(parsed.get("reason", ""))[:200],
                })
            except Exception as e:  # noqa: BLE001
                logger.warning("LLMScorer.score_failed key=%s err=%s", f.get("key"), e)
                out.append({"key": f.get("key"), "importance": None})

        # 全军覆没(每条 importance 都是 None)→ 返回 None 让调用方走启发式
        if all(r["importance"] is None for r in out):
            return None
        return out


class LLMSummarizer:
    """LLM 摘要器 —— 把**一批** fact 总结成**一条**日报/周报/月报。

    🆕 2026-10-12 重写。此前是 ``summarize_5w1h``(逐条 5W1H)的桩,返回 None;
    consolidation 因此退化成"N 条 fact → N 条 daily",压缩率为 0 —— 与
    consolidation 的目的(降低 Qdrant 负担)完全相反。

    新语义:输入 N 条 fact,**输出 1 条**时段摘要。调用方按
    :class:`~smartbutler.emotion.memory.levels.MemoryLevel` 组织 prompt:
    daily 汇总"昨天所有 fact"、weekly 汇总"本周所有 daily"、
    monthly 汇总"本月所有 weekly"。

    设计约束:
    - **why 严禁编造** —— 检出 "用户可能/大概/也许" 等推断语则丢弃该字段
    - **降级** —— LLM 未注入 / 调用失败 / 解析失败 → 返回 None,
      调用方走 :class:`HeuristicSummarizer` 的拼接兜底
    - **可读性优先** —— 摘要要能直接注入 prompt 给 LLM 看,不要 JSON
    """

    def __init__(
        self,
        llm: "Any | None" = None,
        *,
        max_facts: int = 200,
        max_fact_chars: int = 1500,
        max_tokens: int = 2000,
        timeout_seconds: float = 60.0,
    ) -> None:
        """Args:
            llm: :class:`~smartbutler.capabilities.llm.base.BaseLLM` 实例;
                None = 不调 LLM(降级到 :class:`HeuristicSummarizer`)。
            max_facts: 单次总结最多纳入多少条 fact(防止 prompt 爆炸)。
            max_fact_chars: 单条 fact 送进 prompt 的字符上限。
            max_tokens: LLM 输出上限。
            timeout_seconds: 单次调用超时,防止 consolidation 卡死。
        """
        self._llm = llm
        self._max_facts = max_facts
        self._max_fact_chars = max_fact_chars
        self._max_tokens = max_tokens
        self._timeout_seconds = timeout_seconds

    @property
    def enabled(self) -> bool:
        return self._llm is not None

    def _build_prompt(self, level: str, period: str, facts: list[dict[str, Any]]) -> str:
        """按目标层级拼 prompt。"""
        from smartbutler.emotion.memory.levels import MemoryLevel

        label = {
            MemoryLevel.DAILY.value: "日报",
            MemoryLevel.WEEKLY.value: "周报",
            MemoryLevel.MONTHLY.value: "月报",
        }.get(level, "摘要")

        lines: list[str] = []
        for i, f in enumerate(facts[: self._max_facts], 1):
            content = str(f.get("content", "")).strip()
            if not content:
                continue
            if len(content) > self._max_fact_chars:
                content = content[: self._max_fact_chars] + "…(略)"
            lines.append(f"[记忆{i}] {content}")

        return (
            f"你是记忆整理助手。请把下面 {period} 的 {len(lines)} 条记忆"
            f"合并成**一条{label}**。\n"
            "\n"
            "要求:\n"
            "1. **只写记忆里真实存在的信息**,严禁推断、补充、编造。\n"
            "2. 不确定的时间/原因宁可省略,也不要猜。\n"
            "3. 同一件事的多条记忆合并成一句,不要重复罗列。\n"
            "4. 保留具体的数字、日期、人名、地点、偏好等细节。\n"
            "5. 用中文 markdown,分条列出,控制在 600 字以内。\n"
            f"\n"
            f"【{period}的记忆】\n" + "\n".join(lines)
        )

    async def summarize_period(
        self,
        facts: list[dict[str, Any]],
        *,
        level: str = "daily",
        period: str = "昨天",
    ) -> str | None:
        """把一批 fact 总结成一条时段摘要。

        Args:
            facts: ``[{"key":..., "content":..., "importance":...}, ...]``
            level: 目标层("daily"/"weekly"/"monthly"),决定 prompt 措辞。
            period: 时间段描述(如 "昨天 2026-10-11"/"本周 10-06~10-12")。

        Returns:
            markdown 摘要字符串;未注入 LLM / 调用失败 / 内容为空 → ``None``
            (调用方降级到 :class:`HeuristicSummarizer`)。
        """
        if self._llm is None or not facts:
            return None

        from smartbutler.capabilities.llm.types import Message

        prompt = self._build_prompt(level, period, facts)
        if not prompt.strip():
            return None

        try:
            import asyncio

            resp = await asyncio.wait_for(
                self._llm.chat(
                    [
                        Message(
                            role="system",
                            content=(
                                "你只做记忆归档,不闲聊。"
                                "输出纯 markdown,不要代码块围栏,不要开场白。"
                            ),
                        ),
                        Message(role="user", content=prompt),
                    ],
                    temperature=0.2,
                    max_tokens=self._max_tokens,
                ),
                timeout=self._timeout_seconds,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "LLMSummarizer.summarize_failed level=%s n_facts=%d err=%s",
                level, len(facts), e,
            )
            return None

        text = (getattr(resp, "content", "") or "").strip()
        if not text:
            logger.warning("LLMSummarizer.empty_summary level=%s", level)
            return None

        # 剥掉可能的 markdown 代码块围栏
        text = _strip_code_fence(text)
        logger.info(
            "LLMSummarizer.summarize_done level=%s n_facts=%d summary_len=%d",
            level, len(facts), len(text),
        )
        return text

    async def summarize_5w1h(
        self, facts: list[FiveW1H]
    ) -> list[FiveW1H] | None:
        """兼容旧 API(逐条 5W1H 提炼)。

        🆕 2026-10-12:新流程用 :meth:`summarize_period`(聚合成一条),
        本方法保留仅为不破坏既有调用方,仍返回 None(不调 LLM)。
        """
        logger.debug("LLMSummarizer.summarize_5w1h deprecated, returning None")
        return None

    @staticmethod
    def validate_why(why: str) -> str:
        """校验 why 字段:含 LLM 推断语 → 丢弃(返空字符串)。"""
        if not why:
            return ""
        if _INFERENCE_PATTERNS.search(why):
            logger.warning("LLM_inferred_why_dropped content=%s", why[:50])
            return ""
        return why


class HeuristicSummarizer:
    """本地启发式摘要 —— 不调 LLM,直接拼接(降级兜底)。"""

    def summarize(self, facts: list[FiveW1H]) -> list[FiveW1H]:
        """返回原样(5W1H 已经是结构化,不需要再摘要)。

        Args:
            facts: 5W1H 列表。

        Returns:
            同一份 facts(原样返回,L1 daily 写入时直接用)。
        """
        return list(facts)

    def summarize_text(self, contents: list[str]) -> str:
        """纯文本启发式摘要:多 fact 拼接 + 去重。"""
        seen: set[str] = set()
        lines: list[str] = []
        for c in contents:
            snippet = c.strip()
            if not snippet or snippet in seen:
                continue
            seen.add(snippet)
            if len(snippet) > 200:
                snippet = snippet[:197] + "..."
            lines.append(f"- {snippet}")
        return "\n".join(lines)


# 公开 API
__all__ = [
    "FiveW1H",
    "SummarizedFact",
    "FiveW1HExtractor",
    "LLMScorer",
    "LLMSummarizer",
    "HeuristicSummarizer",
]
