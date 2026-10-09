# SmartButler 技能(Skill)目录

本目录存放内置 Skill。

## Skill 格式

每个 Skill 一个子目录,子目录下必须有 `SKILL.md`:

```
smartbutler/skills/builtin/
└── my-skill/
    ├── SKILL.md          # 必填:YAML frontmatter + markdown 指令
    ├── data/             # 可选:Skill 自有数据(cookie / cache)
    └── helper.py         # 可选:辅助脚本
```

`SKILL.md` 格式(Anthropic Agent Skills 规范):

```markdown
---
name: my-skill
description: 简短描述,会被注入到管家的 system prompt 用于路由
---

# My Skill

## When to Use
- 适用场景 A
- 适用场景 B

## Steps
1. 第一步
2. 第二步
```

## 内置 Skill 清单

| 名称 | 状态 | 说明 |
|---|---|---|
| (占位) | 待添加 | 用户自填 agent-browser |

## Phase 5 当前状态

**无内置 Skill**。等用户提供 `agent-browser` Skill 后,放在 `smartbutler/skills/builtin/agent-browser/SKILL.md`。
