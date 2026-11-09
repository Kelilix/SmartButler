"""Memory 4 层分片规则(Phase 6.3 P1)。

设计要点:
1. **同一 Qdrant collection,不同 level tag** —— 不分物理 collection,简单可靠。
2. **level tag 走 Qdrant tags 数组,与其他 tag(``source:xxx``)共存**。
3. **RecallLevel 枚举** —— recall() 默认只查 daily + weekly + permanent(跳 raw,跳 monthly 太粗)。
4. **压缩阈值集中管理** —— consolidation / forgetting / vacuum 都从这读。

为什么不分 collection:
- 多 collection 意味着多套向量索引,recall 跨 collection 要 union,复杂度 ↑,性能 ↓
- 4 层 tag 走 ``payload.tags`` 数组,Qdrant 服务端 ``MatchValue`` 单次过滤,跟现有 tag 过滤同路径
- 跨层 recall 用 ``level:daily OR level:weekly``(走 Qdrant 1.19 的 should/must)即可
"""

from __future__ import annotations

from enum import StrEnum, unique


@unique
class MemoryLevel(StrEnum):
    """4 层分片枚举(写时打 tag,查时过滤)。"""

    RAW = "raw"            # L0 短期,SqliteSaver 存,不进 Qdrant
    DAILY = "daily"        # L1 24h 内的已收尾 fact
    WEEKLY = "weekly"      # L2 7d daily 摘要
    MONTHLY = "monthly"    # L3 30d weekly 摘要
    PERMANENT = "permanent"  # L4 永久


@unique
class RecallLevel(StrEnum):
    """recall() 时的层选择(枚举值 = 逗号分隔的 level tag 列表)。

    🆕 2026-10-12:permanent 层已停用,默认改为"全层级"。
    聚合后每层条目都很少(月报可能只有几条),全查的成本可接受,
    反而能避免"漏掉某层"导致答不出来的��况(实测踩过:
    monthly 被默认跳过,导致月报内容召不回来)。
    """

    # 默认:三层全查(daily + weekly + monthly)
    DEFAULT = "daily,weekly,monthly"
    # 全量(含 raw,运维/调试用)
    ALL = "raw,daily,weekly,monthly"
    # 🆕 已废弃(permanent 层停用),保留供向后兼容
    PERMANENT_ONLY = "permanent"
    # 日+周(最近的活跃记忆)
    RECENT = "daily,weekly"


# Level 等级(int,用于比较)
LEVEL_RANK: dict[MemoryLevel, int] = {
    MemoryLevel.RAW: 0,
    MemoryLevel.DAILY: 1,
    MemoryLevel.WEEKLY: 2,
    MemoryLevel.MONTHLY: 3,
    MemoryLevel.PERMANENT: 4,
}


# 重要性分级(2026-10-12 重写)
#
# 🆕 聚合模型下,importance **不再决定"进哪几层"** —— 每条 fact 都先进
# 当期日报,压缩交给 LLM 总结决定。importance 现在只做两件事:
#   1. 门控:低于 CONSOLIDATION_TTL_THRESHOLD 的 fact 不进汇总
#   2. 摘要条目的 importance = 源 fact 的最大值(见 Consolidator._aggregate_importance)
#
# 旧的三级分级(0.5 → L1+L2,0.8 → L1+L2+L3+permanent)已废弃:
# 它是为"逐条升格"设计的,在聚合模型下没有意义 —— 一批 fact 无论
# importance 高低都汇进同一条日报。
CONSOLIDATION_TTL_THRESHOLD = 0.2       # < 0.2 不进汇总

# 🆕 3 层压缩时间窗(天)
# daily 汇总"昨天"→ weekly 扫最近 7 天的 daily → monthly 扫最近 30 天的 weekly
DAILY_TO_WEEKLY_DAYS = 7                # L1 → L2 触发
WEEKLY_TO_MONTHLY_DAYS = 30            # L2 → L3 触发
# ⚠️ permanent 层已不做(用户决策 2026-10-12):月报已足够小且永久保留,
# 真正的"永久层"(习惯/画像)留给后续 habit 模块。
# 下面两个常量仅保留供 CLI / 向后兼容 import,新流程不使用。
CONSOLIDATION_DAILY_THRESHOLD = 0.5     # 废弃:聚合模型下不按 importance 分层
CONSOLIDATION_PERMANENT_THRESHOLD = 0.8  # 废弃:同上
MONTHLY_TO_PERMANENT_DAYS = 365         # 废弃:permanent 层未启用

# TTL 时间窗(双时间窗,见 §2.5.1.16)
DELETED_RECOVERABLE_DAYS = 7           # deleted_recoverable → archived
ARCHIVED_RETENTION_DAYS = 30           # archived → purged


def level_tag(level: MemoryLevel) -> str:
    """把 MemoryLevel 转成 Qdrant tag 字符串(``level:daily``)。"""
    return f"level:{level.value}"


# ---------------------------------------------------------------------------
# 归属 tag 生成(2026-11-02)
# ---------------------------------------------------------------------------
#
# 设计原则:**归属是结构化字段,不是文本**。
#
# 归属(谁说的)必须由代码保证,不能靠 LLM 在摘要正文里"写上名字"——
# 那样每次升格的结果会漂移、无法断言、跨层必然衰减。
# 归属以 tag 形式落库,每层升格时由代码**重新写一遍**,形成不变式:
#
#     fact  tags: user:X, task:Y
#     日报  tags: user:X, task:Y          ← 代码写
#     周报  tags: user:X, task:Y          ← 代码写
#
# 只有正文措辞交给 LLM,归属永远由代码守。
#
# tag 语义:
# - ``user:<user_id>``      —— **主归属**。谁的"这一天/这一周"= 说话人
#   (声纹识别出的 user_id),**不是**话语里提到的人。
#   例:"我妈今天也去上海"由高天禹说 → 归属高天禹,转述对象留在正文里。
# - ``task:<task_id>``      —— 工作单元锚点。一条日报可含多个 task tag。
# - ``participants:<ids>``  —— 本条目涉及的全部 user_id(逗号分隔,排序去重)。
# - ``task_started_at:<iso>`` —— task 的起始时间。跨天 task 用它归一化时间窗,
#   避免一个 task 被切成几段日报。

#: 无归属时使用的占位 user_id(老数据没有 user: tag 的情况)
UNKNOWN_USER = "unknown"

#: 🆕 2026-11-04: "这条话否定了某个既有事实" 的标记 tag。
#:
#: 承载"张女士不再是我朋友了"这类**修正语义**的入口。
#: Step 1 只标记,Step 3(实体消解 + 图)才做真正的自动消解。
INVALIDATES_TAG = "invalidates:1"


def user_tag(user_id: str | None) -> str:
    """生成主归属 tag(``user:speaker_abc``)。

    空值统一转成 ``user:unknown`` —— 老数据(升格前写的 fact)没有归属,
    归到 unknown 组,**不丢弃**也不污染具体某个人的组。
    """
    uid = (user_id or "").strip()
    return f"user:{uid or UNKNOWN_USER}"


def task_tag(task_id: str | None) -> str:
    """生成 task 锚点 tag(``task:task_abc``)。空值返回 ""(不写该 tag)。"""
    tid = (task_id or "").strip()
    return f"task:{tid}" if tid else ""


def participants_tag(user_ids: list[str] | tuple[str, ...] | None) -> str:
    """生成参与者 tag(``participants:a,b``)。

    去重 + 排序,保证同一组参与者每次生成的值一致(便于幂等比较)。
    空列表返回 ""。
    """
    if not user_ids:
        return ""
    cleaned = sorted({(u or "").strip() for u in user_ids if (u or "").strip()})
    if not cleaned:
        return ""
    return f"participants:{','.join(cleaned)}"


def task_started_at_tag(started_at: str | None) -> str:
    """生成 task 起始时间 tag(``task_started_at:2026-11-01T10:00:00``)。"""
    ts = (started_at or "").strip()
    return f"task_started_at:{ts}" if ts else ""


# ---------------------------------------------------------------------------
# 时间 tag(2026-11-04)
# ---------------------------------------------------------------------------
#
# 设计依据:Zep / Graphiti 的**双时态**模型。
#
# 我们只落其中两条(第三步"图相关的失效修正"暂不做):
#
# - ``spoken_at`` —— **系统时间**:这条事实是**什么时候被说出来的**。
#   客观、可由代码直接取(task_messages.spoken_at),不依赖 LLM。
#   用途:回答"这件事我是什么时候跟你说的"、"上次的对话是什么时候"。
#
# - ``valid_at``  —— **世界时间**:这件事**本身在什么时候成立/生效**。
#   需要 LLM 把"明天""下周一"绝对化,语义有四种:
#
#     ① 精确时间点  : "2026年1月1号是我领证了"
#        → valid_at:2026-01-01
#     ② 时间范围    : "下周我想吃龙虾"
#        → valid_at:2026-11-09/2026-11-15   (ISO 区间,闭区间)
#     ③ 周期性      : "1月29号是我生日"  (每年都成立)
#        → valid_at:recurring:01-29
#     ④ 永久/普适事实: "张女士是我朋友"  (没有时间语义)
#        → valid_at:permanent
#
# 为什么用 **tag** 而不是 payload 字段:
# - 跟现有的归属 tag(user:/task:/participants:)体系一致,升格时**代码重写一遍**
# - Qdrant 的 tags 数组支持服务端 MatchValue 过滤,能直接按时间查
# - 区间/周期/永久统一编码成字符串,不需要改 payload schema
#
# 为什么 valid_at 用**字符串**而不是 datetime:
# - ②区间不是一个时刻,③周期更不是 —— datetime 表达不了
# - 字符串编码简单、可读、可比较(ISO 前缀排序对①②有效)


def spoken_at_tag(spoken_at: str | None) -> str:
    """生成 spoken_at tag(``spoken_at:2026-11-03T10:00:00+00:00``)。

    事实的**说话时间**(客观,代码直接取)。空值返回 ""(不写该 tag)。
    """
    ts = (spoken_at or "").strip()
    return f"spoken_at:{ts}" if ts else ""


#: valid_at 的四种形态标记
VALID_AT_PERMANENT = "permanent"          # 永久/普适事实("张女士是我朋友")
VALID_AT_RECURRING_PREFIX = "recurring:"  # 周期事实("1月29号是我生日")


def valid_at_tag(valid_at: str | None) -> str:
    """生成 valid_at tag(``valid_at:2026-01-01``)。

    支持四种形态(见上方设计说明):

    - 精确点   ``2026-01-01`` / ``2026-01-01T10:00:00+00:00``
    - 区间     ``2026-11-09/2026-11-15``(ISO 区间,闭区间:两端都含)
    - 周期     ``recurring:01-29``
    - 永久     ``permanent``

    空值返回 ""(不写该 tag)—— 表示"没能抽出时间",而不是"永久"。
    这两个语义必须区分,所以**空值不写 tag**,由 LLM 明确输出 ``permanent``。
    """
    ts = (valid_at or "").strip()
    return f"valid_at:{ts}" if ts else ""


# ---------------------------------------------------------------------------
# 失效 / 修正 tag(2026-11-04,Step 1)
# ---------------------------------------------------------------------------
#
# 问题:用户说"张女士不再是我朋友了,我们掰了" —— 这条话**否定**了一条旧事实。
#
# 如果只往库里追加一条新 fact,库里的状态就是:
#
#     F1: 张女士是我朋友            (permanent)
#     F2: 张女士不再是我朋友了,我们掰了 (permanent)
#
# 两条**互相矛盾**且都"永久成立" —— LLM 召回时看到这两条,无法判断该信哪条。
#
# 解法(双时态的经典做法,对应 Zep 的 ``invalid_at``):
#
#     F1: valid_at=permanent, invalid_at=2026-11-04  ← 到 11-04 失效
#     F2: valid_at=permanent                          ← 从 11-04 起生效
#
# 这样"现在"问"张女士是不是我朋友" → F1 已失效,F2 生效 → 答案唯一。
# 而"以前呢"(2026 年 10 月问)→ F1 当时有效,F2 当时还不存在 → 答案也唯一。
#
# **历史没有被改写,只是被加了一个失效时间** —— 这是 bi-temporal 的核心。
#
# Step 1 只负责**承载**这些字段(写 tag / 解析 tag)。
# 「自动发现 F2 否定 F1」需要实体消解 + 图,属于 Step 3,现在不做。
# 但字段和 prompt 现在就位,Step 3 接入时不用改 schema。


def invalid_at_tag(invalid_at: str | None) -> str:
    """生成 invalid_at tag(``invalid_at:2026-11-04T10:00:00+00:00``)。

    事实**失效的时间**(世界时间)。空值返回 ""(还没失效)。
    """
    ts = (invalid_at or "").strip()
    return f"invalid_at:{ts}" if ts else ""


def supersedes_tag(supersedes: str | None) -> str:
    """生成 supersedes tag(``supersedes:fact_abc123``)。

    标记"这条 fact **修正/否定**了另一条 fact"。用来给 Step 3 的
    矛盾消解留线索 —— 现在只标记,不做自动消解。

    支持多个 key(逗号分隔),排序去重保证值稳定。
    """
    if not supersedes:
        return ""
    keys = sorted({k.strip() for k in supersedes.split(",") if k.strip()})
    return f"supersedes:{','.join(keys)}" if keys else ""


def parse_invalid_at(tags: list[str] | tuple[str, ...] | None) -> str:
    """从 tags 里解析 invalid_at(ISO 字符串),取不到返回 ""。"""
    for tag in tags or ():
        if tag.startswith("invalid_at:"):
            return tag[len("invalid_at:") :].strip()
    return ""


def parse_supersedes(tags: list[str] | tuple[str, ...] | None) -> list[str]:
    """从 tags 里解析被这条 fact 修正的 key 列表。"""
    for tag in tags or ():
        if tag.startswith("supersedes:"):
            raw = tag[len("supersedes:") :]
            return [k for k in (x.strip() for x in raw.split(",")) if k]
    return []


def parse_spoken_at(tags: list[str] | tuple[str, ...] | None) -> str:
    """从 tags 里解析 spoken_at(ISO 字符串),取不到返回 ""。"""
    for tag in tags or ():
        if tag.startswith("spoken_at:"):
            return tag[len("spoken_at:") :].strip()
    return ""


def parse_valid_at(tags: list[str] | tuple[str, ...] | None) -> str:
    """从 tags 里解析 valid_at(原始值,不含 ``valid_at:`` 前缀)。

    取不到返回 ""。返回值可能是四种形态之一,调用方按需解析。
    """
    for tag in tags or ():
        if tag.startswith("valid_at:"):
            return tag[len("valid_at:") :].strip()
    return ""


def parse_user_tag(tags: list[str] | tuple[str, ...] | None) -> str | None:
    """从 tags 里解析主归属 user_id。

    找不到返回 ``None``(**不是** ``"unknown"``)—— 调用方需要区分
    "没有归属信息"和"归属是 unknown"两种情况,前者才走兜底逻辑。
    """
    for tag in tags or ():
        if tag.startswith("user:"):
            uid = tag[len("user:") :].strip()
            if uid:
                return uid
    return None


def parse_task_tags(tags: list[str] | tuple[str, ...] | None) -> list[str]:
    """从 tags 里解析全部 task_id(按出现顺序去重)。"""
    out: list[str] = []
    for tag in tags or ():
        if tag.startswith("task:"):
            tid = tag[len("task:") :].strip()
            if tid and tid not in out:
                out.append(tid)
    return out


def parse_participants_tag(tags: list[str] | tuple[str, ...] | None) -> list[str]:
    """从 tags 里解析参与者 user_id 列表。"""
    for tag in tags or ():
        if tag.startswith("participants:"):
            raw = tag[len("participants:") :]
            return [u for u in (x.strip() for x in raw.split(",")) if u]
    return []


def parse_task_started_at(tags: list[str] | tuple[str, ...] | None) -> str:
    """从 tags 里解析 task 起始时间(ISO 字符串),取不到返回 ""。"""
    for tag in tags or ():
        if tag.startswith("task_started_at:"):
            return tag[len("task_started_at:") :].strip()
    return ""


def parse_recall_level(levels: RecallLevel | str | list[str] | None) -> list[str]:
    """把 RecallLevel / 字符串 / 列表统一解析成 ``level:xxx`` tag 列表。

    Args:
        levels: RecallLevel 枚举 / 逗号分隔字符串 / 单个字符串 / 列表 / None。
            None = RecallLevel.DEFAULT。

    Returns:
        ``["level:daily", "level:weekly", "level:permanent"]`` 形式的列表。
    """
    if levels is None:
        levels = RecallLevel.DEFAULT
    if isinstance(levels, RecallLevel):
        raw = levels.value
    elif isinstance(levels, str):
        raw = levels
    elif isinstance(levels, list):
        raw = ",".join(levels)
    else:
        raise TypeError(
            f"levels 必须是 RecallLevel/str/list,得到 {type(levels).__name__}"
        )
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    # 接受 "daily" 或 "level:daily",统一加前缀
    tags: list[str] = []
    for p in parts:
        if p.startswith("level:"):
            tags.append(p)
        else:
            tags.append(f"level:{p}")
    return tags


def importance_to_levels(importance: float) -> list[MemoryLevel]:
    """给定 importance 0-1,返回该 fact 应驻留的 level 列表(写入/升格用)。

    🆕 2026-10-12 重写为**单层门控**。旧实现按 importance 分三级
    (0.5 → L1+L2,0.8 → L1+L2+L3+permanent),那是为"逐条升格"设计的。
    聚合模型下每条 fact 都先进当期日报,后续 weekly/monthly 由
    **摘要条目**收敛,不再对单条 fact 分层。

    Examples:
        >>> importance_to_levels(0.1)
        []  # 低于门控阈值,不进汇总
        >>> importance_to_levels(0.3)
        [<MemoryLevel.DAILY: 'daily'>]
        >>> importance_to_levels(0.9)
        [<MemoryLevel.DAILY: 'daily'>]
    """
    if importance < CONSOLIDATION_TTL_THRESHOLD:
        return []
    return [MemoryLevel.DAILY]


__all__ = [
    "MemoryLevel",
    "RecallLevel",
    "LEVEL_RANK",
    "CONSOLIDATION_TTL_THRESHOLD",
    "CONSOLIDATION_DAILY_THRESHOLD",
    "CONSOLIDATION_PERMANENT_THRESHOLD",
    "DAILY_TO_WEEKLY_DAYS",
    "WEEKLY_TO_MONTHLY_DAYS",
    "MONTHLY_TO_PERMANENT_DAYS",
    "DELETED_RECOVERABLE_DAYS",
    "ARCHIVED_RETENTION_DAYS",
    "level_tag",
    "parse_recall_level",
    "importance_to_levels",
    # 归属 tag(2026-11-02)
    "UNKNOWN_USER",
    "INVALIDATES_TAG",
    "user_tag",
    "task_tag",
    "participants_tag",
    "task_started_at_tag",
    "parse_user_tag",
    "parse_task_tags",
    "parse_participants_tag",
    "parse_task_started_at",
    # 时间 tag(2026-11-04)
    "VALID_AT_PERMANENT",
    "VALID_AT_RECURRING_PREFIX",
    "spoken_at_tag",
    "valid_at_tag",
    "invalid_at_tag",
    "supersedes_tag",
    "parse_spoken_at",
    "parse_valid_at",
    "parse_invalid_at",
    "parse_supersedes",
]
