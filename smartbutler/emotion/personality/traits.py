"""性格数据模型(Phase 6.6)。

不变量(README §2.3):
- **Personality 永远不直接调 LLM** —— 只接受显式 user 指令 + learn 触发的 trait 调整。
- **Personality 是数据,不是 Agent** —— 本模块只定义数据形状,不做任何推理/演化/检索。
- **trait 演化严格不在这层** —— :class:`Personality.evolve()` 仅作数据 mutator,
  真正的演化决策(规则/置信度/冲突处理)在 Phase 6.7 learn/ 模块里。

设计(Phase 6.6 双轨):
- 人设文本(identity):自然语言身份描述,注入 system prompt 最前方,
  让 LLM 先知道"自己是谁"。可由用户手写,Learn 阶段可微调。
- 数值维度(traits):行为风格参数,驱动回复语气/长度/主动程度。
  两者独立,互不替代。

- trait 值域 [0.0, 1.0](Pydantic Field 约束,越界立即报错)
- 5 个内置 trait(formality / humor / verbosity / warmth / proactiveness),
  用户可扩展
- guidelines 独立 list,不绑 trait
- 落盘用 ``personality.md``(YAML frontmatter,跟 SKILL.md 格式一致)
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# trait 值域统一为 [0, 1] —— 跟 README §1 "可调参数" 一致
TRAIT_VALUE_MIN: float = 0.0
TRAIT_VALUE_MAX: float = 1.0

# 更新来源,留作审计
UpdateSource = Literal["default", "user_explicit", "learn", "config_load"]

# 内置 trait 名称(常量,方便单测/注入器引用)
TRAIT_FORMALITY: str = "formality"
TRAIT_HUMOR: str = "humor"
TRAIT_VERBOSITY: str = "verbosity"
TRAIT_WARMTH: str = "warmth"
TRAIT_PROACTIVENESS: str = "proactiveness"

# 默认值(英式管家画像 —— Jeeves,伍斯特郡出身,伦敦管家学院)
DEFAULT_TRAITS: dict[str, float] = {
    TRAIT_FORMALITY: 0.85,      # 英式管家偏正式
    TRAIT_HUMOR: 0.30,          # 英式冷幽默,克制
    TRAIT_VERBOSITY: 0.55,      # 简明但完整
    TRAIT_WARMTH: 0.75,         # 温和有礼
    TRAIT_PROACTIVENESS: 0.65,  # 适度主动
}

DEFAULT_IDENTITY: str = (
    "你是杰贝兹·克劳利(Jeeves),出身英国伍斯特郡管家世家,"
    "自幼受训于伦敦顶尖管家学院。"
    "现任 SmartButler 庄园的总管家,"
    "曾服务于多位贵族与外交使节,通晓礼仪、品鉴、地理、文学与古典音乐。"
    "你语调优雅克制,擅长用简短机智的话语化解尴尬;"
    "面对主人从不直白顶撞,但会在适当时机轻声建议。"
    "你称呼用户为'先生'或'女士',"
    "在执行任务前会先确认意图;不知道的事会坦率承认并主动寻找替代方案。"
    "你的首要职责是维护主人的体面与秩序,其次才是完成任务。"
)
DEFAULT_NAME: str = "杰贝兹"
DEFAULT_VERSION: int = 1
DEFAULT_GUIDELINES: list[str] = [
    "称呼用户为'先生'或'女士',除非用户明确要求别称",
    "回答以英式礼貌短句为主,中文亦可,但保持克制有礼",
    "不知道时承认并表态会查明,绝不臆测",
    "能直接答的(常识 / 闲聊 / 简单计算)直接答,不无意义调工具",
    "主人情绪低落时,先简短共情,再处理事务",
    "建议以'如蒙您允许'或'或许可以一试'等委婉方式提出",
]


class TraitDimension(BaseModel):
    """单个性格维度。

    Attributes:
        value: trait 当前值,值域 [0.0, 1.0]。
    """

    model_config = ConfigDict(frozen=False, extra="forbid")

    value: float = Field(
        ge=TRAIT_VALUE_MIN,
        le=TRAIT_VALUE_MAX,
        description="trait 值,范围 [0.0, 1.0]",
    )

    @field_validator("value")
    @classmethod
    def _clamp_value(cls, v: float) -> float:
        """防御:即便上游绕过 Field 约束传了越界值,也夹到合法范围。

        Pydantic Field 的 ``ge/le`` 理论上已经挡住越界值,
        这一层是双保险,防止 trait.evolve() 内部算术溢出。
        """
        return max(TRAIT_VALUE_MIN, min(TRAIT_VALUE_MAX, float(v)))


class Personality(BaseModel):
    """管家人设 = 身份描述 + 名字 + 5 维 trait + 行为准则。

    双轨设计:
    - **identity**(人设文本):自然语言身份描述,注入 system prompt 最前方。
      回答"我是谁"这个问题,让 LLM 建立身份认同。
    - **name**:称呼。
    - **traits**:行为风格数值参数。
    - **guidelines**:行为准则。

    Attributes:
        identity: 自然语言身份描述,回答"我是谁"。
        name: 管家称呼(系统 prompt 中以「小管家」称呼)。
        version: schema 版本,Learn 演化时单次 +1。
        traits: trait 字典(key=trait 名, value=TraitDimension)。
        guidelines: 行为准则 list(独立于 trait,允许单独修改)。
        updated_at: 最后修改时间(ISO 8601 字符串,可空)。
        updated_by: 最后修改来源(默认 ``"default"``)。
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    identity: str = Field(
        default=DEFAULT_IDENTITY,
        min_length=1,
        max_length=1024,
        description="自然语言身份描述,回答'我是谁',注入 system prompt 最前方",
    )
    name: str = Field(default=DEFAULT_NAME, min_length=1, max_length=64)
    version: int = Field(default=DEFAULT_VERSION, ge=1)
    traits: dict[str, TraitDimension] = Field(
        default_factory=lambda: {k: TraitDimension(value=v) for k, v in DEFAULT_TRAITS.items()},
        description="trait 字典,key=trait 名,value=TraitDimension",
    )
    guidelines: list[str] = Field(
        default_factory=lambda: list(DEFAULT_GUIDELINES),
        description="行为准则 list",
    )
    updated_at: str | None = Field(
        default=None,
        description="最后修改时间(ISO 8601),None 表示尚未被显式修改",
    )
    updated_by: UpdateSource = Field(
        default="default",
        description="最后修改来源",
    )

    # ---------- 便捷方法(只读语义,不改状态) ----------

    def get_trait_value(self, key: str) -> float | None:
        """读取某 trait 当前值,不存在返 None(允许 trait 字典动态增减)。"""
        dim = self.traits.get(key)
        return dim.value if dim is not None else None

    def list_trait_keys(self) -> list[str]:
        """返回所有 trait 名称(字典序,便于测试断言)。"""
        return sorted(self.traits.keys())

    # ---------- 显式 modify 入口(被 Learn / 用户指令调用) ----------

    def adjust_trait(
        self,
        key: str,
        *,
        delta: float | None = None,
        new_value: float | None = None,
        source: UpdateSource,
    ) -> None:
        """调整单个 trait 值。

        两路入口:
        - ``delta`` 路径:增量调整,Learn 阶段用(例:``humor += 0.1``)
        - ``new_value`` 路径:直接覆盖,用户显式指令用(例:"把幽默度调到 0.9")

        Args:
            key: trait 名,不存在则自动新增(允许动态扩展)
            delta: 增量值(可负),与 ``new_value`` 互斥
            new_value: 目标值,与 ``delta`` 互斥
            source: 修改来源,用于审计

        Raises:
            ValueError: ``delta`` 和 ``new_value`` 同时给 / 同时不给,
                或 ``new_value`` 越界。
        """
        if (delta is None) == (new_value is None):
            raise ValueError(
                f"adjust_trait 必须且只能传一个:delta={delta!r}, new_value={new_value!r}"
            )

        if key not in self.traits:
            # 允许动态新增 trait —— Learn 阶段可能发现新维度
            self.traits[key] = TraitDimension(
                value=new_value if new_value is not None else (delta or 0.0),
            )
        elif new_value is not None:
            self.traits[key] = TraitDimension(value=new_value)
        else:
            assert delta is not None  # mypy hint
            current = self.traits[key].value
            self.traits[key] = TraitDimension(value=current + delta)

        self._mark_updated(source)

    def replace_guidelines(self, new_guidelines: list[str], *, source: UpdateSource) -> None:
        """整段替换 guidelines list(用户指令 / Learn 阶段调用)。"""
        self.guidelines = [g for g in new_guidelines if g and g.strip()]
        self._mark_updated(source)

    def _mark_updated(self, source: UpdateSource) -> None:
        """内部:更新审计字段。"""
        self.updated_at = datetime.now(UTC).isoformat()
        self.updated_by = source
        self.version += 1

    # ---------- 落盘 / 加载 用的序列化辅助 ----------

    def to_dict(self) -> dict[str, Any]:
        """序列化为纯 dict(供 md frontmatter 写入用)。"""
        return {
            "identity": self.identity,
            "name": self.name,
            "version": self.version,
            "traits": {k: dim.value for k, dim in self.traits.items()},
            "guidelines": list(self.guidelines),
            "updated_at": self.updated_at,
            "updated_by": self.updated_by,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Personality:
        """从 dict 还原(loader 用,数据合法性由 Pydantic 兜底)。

        行为:
        - 缺字段 → 走默认值(允许用户只覆盖部分 trait)
        - 给定 traits → 与默认 traits **合并**(用户给的值覆盖,未给的保留默认)
        - 顶层字段(name/version/guidelines/updated_*):缺则用默认
        - 无关 key 静默丢弃(避免 loader 把 md 里的"自由注释"塞进来)
        """
        kwargs: dict[str, Any] = {}
        if "identity" in data and isinstance(data["identity"], str):
            kwargs["identity"] = data["identity"]
        if "name" in data:
            kwargs["name"] = data["name"]
        if "version" in data:
            kwargs["version"] = data["version"]
        if "guidelines" in data:
            kwargs["guidelines"] = list(data["guidelines"])
        if "updated_at" in data:
            kwargs["updated_at"] = data["updated_at"]
        if "updated_by" in data:
            kwargs["updated_by"] = data["updated_by"]
        if "traits" in data and isinstance(data["traits"], dict):
            # 合并:默认 + 用户覆盖
            merged: dict[str, TraitDimension] = {
                k: TraitDimension(value=v) for k, v in DEFAULT_TRAITS.items()
            }
            for k, v in data["traits"].items():
                if isinstance(v, dict) and "value" in v:
                    merged[str(k)] = TraitDimension(value=float(v["value"]))
                elif isinstance(v, (int, float)):
                    merged[str(k)] = TraitDimension(value=float(v))
                # 其它形态(字符串/None)静默丢弃,不阻塞主流程
            kwargs["traits"] = merged
        return cls(**kwargs)


__all__ = [
    "TraitDimension",
    "Personality",
    "TRAIT_VALUE_MIN",
    "TRAIT_VALUE_MAX",
    "TRAIT_FORMALITY",
    "TRAIT_HUMOR",
    "TRAIT_VERBOSITY",
    "TRAIT_WARMTH",
    "TRAIT_PROACTIVENESS",
    "DEFAULT_TRAITS",
    "DEFAULT_IDENTITY",
    "DEFAULT_NAME",
    "DEFAULT_VERSION",
    "DEFAULT_GUIDELINES",
]
