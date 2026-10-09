"""情感层(emotion/) — 区分 SmartButler 与米家的核心。

三个子模块:
- personality/: 性格系统(特征 / 状态 / 演化 / 注入)
- memory/:      记忆系统(短期 / 长期 / 情景 / 语义 / 检索)
- learn/:       学习系统(从交互中提取偏好,驱动 personality 演化)

设计原则:
1. **learn 先行** — 是 personality 演化的数据源,是 memory 检索的权重依据。
   没 learn,personality 是静态的(跟米家没区别),memory 检索无个性化。
2. **personality 在中** — 5-6 个 trait + 状态机 + 演化公式,需要 learn 喂数据。
3. **memory 在后** — 涉及隐私边界 / 嵌入检索 / scope 分级,设计期 1-2 周。
   且必须接 personality/learn 后再设计,否则"用户隐私对话"无防护。

详见 TECHNICAL_DESIGN.md §3.2.4 + §5.9.6(Phase 6 段)。
"""
