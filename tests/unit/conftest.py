"""tests/conftest.py — 测试级全局 fixture。

问题：项目根的 .env 文件会被 pydantic-settings 自动读取,污染"测默认值"的测试。
解法：在测试全局禁用 .env 加载（env_file=None）,只保留 os.environ 来源。
需要真实 .env 的集成测试请显式调用 settings(_env_file=...) 或重新启用。
"""

from __future__ import annotations

import pytest
from pydantic_settings import BaseSettings


@pytest.fixture(autouse=True)
def _disable_dotenv_loading(monkeypatch: pytest.MonkeyPatch) -> None:
    """全局禁用所有 BaseSettings 子类的 .env 文件加载。"""
    original_init = BaseSettings.__init__

    def _patched_init(self: BaseSettings, **kwargs: object) -> None:
        kwargs.setdefault("_env_file", None)
        original_init(self, **kwargs)

    monkeypatch.setattr(BaseSettings, "__init__", _patched_init)
