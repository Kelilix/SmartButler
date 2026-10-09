"""Skill 工具集合 — 包装 SmartButlerFilesystemBackend 为 LLM 可调的 BaseTool。

设计:
- 每个工具是一个 BaseTool 子类,scope=GLOBAL(管家直接可见)
- readonly=True(只读) 或 False(可写)
- arun() 内部调 filesystem_backend 的对应方法
- 错误以 ToolResult(success=False) 形式返回,不抛异常给 LLM
"""
