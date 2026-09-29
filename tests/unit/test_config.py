"""config 层单元测试。

验证：
- 各子模块 Settings 能从环境变量读取。
- 默认值符合文档约定的技术选型（OpenAI 默认、SQLite 默认等）。
- 环境变量隔离：使用 monkeypatch 不污染其他测试。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from smartbutler.config import (
    PROJECT_ROOT,
    AgentSettings,
    LLMSettings,
    LoggingSettings,
    StorageSettings,
    load_agent_settings,
    load_llm_settings,
    load_logging_settings,
    load_storage_settings,
)


def test_project_root_resolves_to_repo_root() -> None:
    """PROJECT_ROOT 必须是项目根（包含 pyproject.toml 的目录）。"""
    assert (PROJECT_ROOT / "pyproject.toml").exists()


def test_llm_defaults_match_document(monkeypatch: pytest.MonkeyPatch) -> None:
    """LLM 默认值符合文档 §4.2：默认 OpenAI、gpt-4o-mini。"""
    monkeypatch.delenv("SMARTBUTLER_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("SMARTBUTLER_LLM_MODEL", raising=False)
    settings = load_llm_settings()
    assert settings.provider == "openai"
    assert settings.model == "gpt-4o-mini"
    assert 0.0 <= settings.temperature <= 2.0
    assert settings.max_tokens > 0


def test_llm_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """环境变量能覆盖默认配置。"""
    monkeypatch.setenv("SMARTBUTLER_LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("SMARTBUTLER_LLM_MODEL", "claude-3-5-sonnet")
    settings = load_llm_settings()
    assert settings.provider == "anthropic"
    assert settings.model == "claude-3-5-sonnet"


def test_llm_temperature_bounds(monkeypatch: pytest.MonkeyPatch) -> None:
    """temperature 超出 [0, 2] 必须报错。"""
    monkeypatch.setenv("SMARTBUTLER_LLM_TEMPERATURE", "3.0")
    with pytest.raises(ValueError):
        load_llm_settings()


def test_storage_default_sqlite_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """默认 SQLite 路径在项目内 data/ 目录。"""
    monkeypatch.delenv("SMARTBUTLER_STORAGE_SQLITE_PATH", raising=False)
    settings = load_storage_settings()
    assert settings.sqlite_path == PROJECT_ROOT / "data" / "smartbutler.db"
    assert settings.qdrant_collection == "smartbutler_memory"
    assert settings.agent_config_dir == PROJECT_ROOT / "config" / "agents"


def test_storage_sqlite_path_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """能覆盖 SQLite 路径。"""
    custom = tmp_path / "custom.db"
    monkeypatch.setenv("SMARTBUTLER_STORAGE_SQLITE_PATH", str(custom))
    settings = load_storage_settings()
    assert settings.sqlite_path == custom


def test_logging_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """日志默认配置：INFO 级别、非 JSON、写到 logs/ 目录。"""
    monkeypatch.delenv("SMARTBUTLER_LOGGING_LEVEL", raising=False)
    settings = load_logging_settings()
    assert settings.level == "INFO"
    assert settings.json_output is False
    assert settings.log_file == PROJECT_ROOT / "logs" / "smartbutler.log"


def test_logging_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """日志级别、JSON 输出可被环境变量覆盖。"""
    monkeypatch.setenv("SMARTBUTLER_LOGGING_LEVEL", "DEBUG")
    monkeypatch.setenv("SMARTBUTLER_LOGGING_JSON_OUTPUT", "true")
    settings = load_logging_settings()
    assert settings.level == "DEBUG"
    assert settings.json_output is True


def test_agent_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """Agent 默认值合理。"""
    monkeypatch.delenv("SMARTBUTLER_AGENT_DEFAULT_TIMEOUT", raising=False)
    settings = load_agent_settings()
    assert settings.default_timeout > 0
    assert settings.default_max_retries >= 0
    assert settings.auto_discover is True


def test_settings_are_independently_loadable(monkeypatch: pytest.MonkeyPatch) -> None:
    """各子模块配置相互独立，加载一个不影响另一个。"""
    monkeypatch.setenv("SMARTBUTLER_LLM_PROVIDER", "ollama")
    llm = load_llm_settings()
    storage = load_storage_settings()
    # storage 不应被 LLM env 影响
    assert llm.provider == "ollama"
    assert storage.sqlite_path.suffix == ".db"


def test_explicit_class_instantiation_matches_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """直接 new 与 load_*_settings() 结果一致（契约稳定）。"""
    monkeypatch.delenv("SMARTBUTLER_LLM_PROVIDER", raising=False)
    assert LLMSettings().provider == load_llm_settings().provider
    assert StorageSettings().sqlite_path == load_storage_settings().sqlite_path
    assert LoggingSettings().level == load_logging_settings().level
    assert AgentSettings().auto_discover == load_agent_settings().auto_discover
