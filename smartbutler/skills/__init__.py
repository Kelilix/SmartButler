"""smartbutler.skills 根包 — 内置 Skill 存放地。

子目录约定(参考 TECHNICAL_DESIGN.md §3.2.7 + 2026-10-09 决议):
- builtin/   内置 Skill,随代码仓库分发
- user/      用户自定义 Skill(预留,Phase 7+)
- (运行时)   ~/.smartbutler/skills/   跨机器用户级 Skill(预留)

Phase 5 只扫描 builtin/。
"""
