"""学习系统(Phase 6,Phase 11 并入) — 从用户交互中提取偏好。

第一版范围(本目录当前包含的):
- `interaction_log`: 一条交互事件的"打分"(positive/negative/neutral + 上下文)
- `preference_vector`: 用户偏好的向量表示(trait → score)
- `signal_extractor`: 把"管家被骂"等信号转成打分

设计原则:
- **先求有,再求好** — 第一版不引入 embedding,纯 KV + 简单聚合
- **写比读多** — 大部分路径是 record(写),读只在 personality 演化时
- **可降级** — learn 不可用时,personality 走静态默认值,管家不崩

后续扩展(不做):
- 嵌入检索(等 memory 模块上 embedding 后再合流)
- 多用户偏好隔离(等 storage 支持 user_id 分桶)
- LLM 总结(等 Phase 9 多模态稳定后)
"""
