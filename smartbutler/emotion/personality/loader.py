"""性格文件加载器(Phase 6.6)。

输入文件 = ``personality.md``(YAML frontmatter,跟 SKILL.md 格式一致)。
文件缺失 / 解析失败 → 回退到默认 :class:`Personality`(管家不崩)。

格式示例::

    ---
    name: 小管家
    version: 1
    traits:
      formality: 0.3
      humor: 0.6
      verbosity: 0.4
      warmth: 0.7
      proactiveness: 0.5
    guidelines:
      - 回答简洁,默认不超过 3 句话
      - 不知道就说不知道,不要瞎猜
    updated_at: 2026-10-12T10:00:00+00:00
    updated_by: user_explicit
    ---

    # 管家性格画像

    这里是自由注释,启动时会被忽略。
    用户可以手写说明为什么这么调。

设计:
- 严格只读 frontmatter,body 部分原样忽略
- 用 ``yaml.safe_load`` 解析 frontmatter,不执行任何指令
- 任何异常都被吞掉并打 warning,返回默认 Personality
  (符合 README §3 "降级链必须闭合")
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import structlog

from smartbutler.emotion.personality.traits import Personality

_logger = structlog.get_logger(__name__)

# 默认文件名(项目根 / 用户家目录,任选其一)
DEFAULT_PERSONALITY_FILENAME: str = "personality.md"


def _split_frontmatter(raw: str) -> tuple[dict[str, Any], str]:
    """分离 YAML frontmatter 和 markdown body。

    Args:
        raw: 文件原始内容。

    Returns:
        (frontmatter_dict, body_text)。frontmatter 空 = ``{}``。
    """
    # 空文件 / 没有 frontmatter 都返回空 dict + 原内容
    raw = raw.strip()
    if not raw or not raw.startswith("---"):
        return {}, raw

    # 找第二个 ``---``(可能不在行首,允许有缩进)
    lines = raw.splitlines()
    if lines[0].strip() != "---":
        return {}, raw

    end_idx: int | None = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end_idx = i
            break
    if end_idx is None:
        return {}, raw

    fm_text = "\n".join(lines[1:end_idx])
    body = "\n".join(lines[end_idx + 1:])

    # 解析 YAML —— 失败返空 dict
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError:
        _logger.warning("personality.loader yaml_not_installed")
        return {}, body

    try:
        parsed = yaml.safe_load(fm_text) or {}
    except yaml.YAMLError as e:  # type: ignore[attr-defined]
        _logger.warning("personality.loader frontmatter_parse_failed", error=str(e))
        return {}, body

    if not isinstance(parsed, dict):
        _logger.warning("personality.loader frontmatter_not_dict", type_=type(parsed).__name__)
        return {}, body

    return parsed, body


def load_personality(path: str | Path | None = None) -> Personality:
    """加载性格文件,失败回退默认值。

    Args:
        path: 配置文件路径。``None`` → 走默认候选路径列表。
            候选顺序(找到即停):
            1. ``<cwd>/personality.md``
            2. ``~/.smartbutler/personality.md``
            都找不到 → 返回默认 :class:`Personality`。

    Returns:
        合法 :class:`Personality` 实例(永远不为 None,内部失败也有默认值兜底)。
    """
    if path is not None:
        candidates: list[Path] = [Path(path)]
    else:
        candidates = [
            Path.cwd() / DEFAULT_PERSONALITY_FILENAME,
            Path.home() / ".smartbutler" / DEFAULT_PERSONALITY_FILENAME,
        ]

    for cand in candidates:
        if cand.is_file():
            return _load_one(cand)
        else:
            _logger.debug("personality.loader candidate_missing", path=str(cand))

    _logger.info("personality.loader using_default", candidates=[str(c) for c in candidates])
    return Personality()


def _load_one(path: Path) -> Personality:
    """读单个文件并解析 frontmatter。"""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as e:
        _logger.warning("personality.loader read_failed", path=str(path), error=str(e))
        return Personality()

    data, _body = _split_frontmatter(raw)
    if not data:
        _logger.info("personality.loader empty_frontmatter_using_default", path=str(path))
        return Personality()

    try:
        personality = Personality.from_dict(data)
    except Exception as e:  # noqa: BLE001
        # Pydantic 校验失败也不让管家崩 —— 记录后回退默认
        _logger.warning(
            "personality.loader validation_failed",
            path=str(path),
            error=str(e),
        )
        return Personality()

    _logger.info(
        "personality.loader loaded",
        path=str(path),
        name=personality.name,
        traits=personality.list_trait_keys(),
        updated_by=personality.updated_by,
    )
    return personality


def save_personality(
    personality: Personality,
    path: str | Path,
) -> Path:
    """把 Personality 序列化成 ``personality.md``。

    写盘后路径 = ``path``(已存在则覆盖)。
    frontmatter 之外写一段简短注释,告诉用户可以自由编辑。

    Args:
        personality: 要落盘的 Personality。
        path: 目标路径(父目录不存在会创建)。

    Returns:
        实际写入的 :class:`Path`(绝对路径)。
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)

    data = personality.to_dict()

    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError:
        _logger.warning("personality.save yaml_not_installed_skipping")
        return target

    fm_text = yaml.safe_dump(
        data,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    )

    body = (
        "\n# 管家性格画像\n\n"
        "这里是自由注释,管家启动时只读上面的 YAML frontmatter。\n"
        "你可以手写说明为什么这么调,例如:\n\n"
        "> 我爸耳背,把 verbosity 调到 0.7,humor 调到 0.3,\n"
        "> 让他听到的是正经话。\n"
    )

    content = f"---\n{fm_text}---\n{body}"
    target.write_text(content, encoding="utf-8")
    _logger.info("personality.save written", path=str(target))
    return target


__all__ = [
    "load_personality",
    "save_personality",
    "DEFAULT_PERSONALITY_FILENAME",
]
