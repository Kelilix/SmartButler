"""SmartButler — 有性格、有记忆、懂上下文的通用智能体。

本包结构（按文档 §3.1）：
- interface/      接口层（HTTP / WebSocket / CLI / MCP）
- capabilities/   能力层（LLM / ASR / TTS / Vision / Tools）
- thinking/       思考层（LangGraph Loop + Reasoning / Decision / Prompt / Context）
- emotion/        情感层（Personality / Memory）
- agents/         Agent 层（Base / Manager / Home / Schedule / Search）
- config/         配置管理
- storage/        存储层
- utils/          通用工具

基础设施层（当前阶段）仅落地 config / storage / utils，其他模块陆续实现。
"""

from __future__ import annotations

__version__ = "0.1.0"
