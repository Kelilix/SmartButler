# SmartButler E2E 测试套件

本目录包含 SmartButler 的端到端连通测试，直接调用外部 API（DeepSeek / OpenAI 等），用于验证能力层抽象在实际网络环境下的行为。

## 目录结构

```
e2e/
├── conftest.py           # 共享 fixtures（LLM 实例、配置加载）
└── test_streaming.py     # 流式调用连通测试
```

## 运行前提

```bash
# 1. 复制并配置环境变量
cp .env.example .env
# 编辑 .env，填入 LLM API Key 和 Base URL

# 2. 安装测试依赖（如需独立运行）
pip install pytest pytest-asyncio httpx

# 3. 运行全部 e2e 测试（默认跳过，按需启用）
pytest tests/e2e/ -m e2e -v

# 4. 只跑流式测试
pytest tests/e2e/test_streaming.py -m e2e -v
```

## 测试标记

| 标记 | 含义 |
|------|------|
| `e2e` | 端到端测试，默认 skip，需显式启用 |

## 注意事项

- e2e 测试会真实消耗 API 配额，请勿在 CI 的公共 runner 上无条件全量运行
- 建议按需单跑：`pytest -m e2e tests/e2e/test_streaming.py`
