"""utils/logging 单元测试。

验证：
- configure_logging 接受 LoggingSettings 或单独参数。
- get_logger 返回可用的 logger，输出符合预期格式。
- 不污染全局 stdlib logging 状态。
"""

from __future__ import annotations

import json
import logging as stdlib_logging
from pathlib import Path

import pytest
import structlog

from smartbutler.config import LoggingSettings
from smartbutler.utils import configure_logging, get_logger


@pytest.fixture(autouse=True)
def _reset_structlog() -> None:
    """每个测试前后重置 structlog 配置，避免测试间污染。"""
    structlog.reset_defaults()
    yield
    structlog.reset_defaults()
    # 同时把 stdlib logging 还原到 WARNING，避免遗留 handler
    stdlib_logging.basicConfig(level=stdlib_logging.WARNING, force=True)


def test_configure_logging_accepts_settings(capsys: pytest.CaptureFixture[str]) -> None:
    """configure_logging(LoggingSettings(...)) 应能工作。"""
    settings = LoggingSettings(level="INFO", json_output=False, log_file=None)
    configure_logging(settings)

    logger = get_logger("test.module")
    logger.info("hello", foo="bar")

    captured = capsys.readouterr()
    assert "hello" in captured.out
    assert "foo=bar" in captured.out or "bar" in captured.out


def test_configure_logging_json_output(capsys: pytest.CaptureFixture[str]) -> None:
    """json_output=True 时必须输出合法 JSON。"""
    settings = LoggingSettings(level="INFO", json_output=True, log_file=None)
    configure_logging(settings)

    logger = get_logger("test.json")
    logger.info("json_event", count=42)

    captured = capsys.readouterr()
    # 最后一行是 JSON（可能有颜色码/前缀，取最后一行解析）
    lines = [ln for ln in captured.out.strip().split("\n") if ln.strip()]
    assert lines, "no log line captured"
    # 至少有一行能解析为 JSON
    parsed = False
    for ln in lines:
        try:
            obj = json.loads(ln)
        except json.JSONDecodeError:
            continue
        assert obj["event"] == "json_event"
        assert obj["count"] == 42
        parsed = True
    assert parsed, f"no JSON line found in: {captured.out!r}"


def test_configure_logging_individual_params(capsys: pytest.CaptureFixture[str]) -> None:
    """不传 settings、传单独参数也能工作。"""
    configure_logging(level="DEBUG", json_output=False, log_file=None)
    logger = get_logger("test.individual")
    logger.debug("debug_msg")
    captured = capsys.readouterr()
    assert "debug_msg" in captured.out


def test_get_logger_returns_structlog_logger() -> None:
    """get_logger 返回的是 structlog logger，能绑定初始字段。"""
    configure_logging(level="INFO", json_output=False, log_file=None)
    logger = get_logger("agent.home", agent_name="home")
    # 验证 bind 后的 logger 仍是合法 structlog 类型
    assert hasattr(logger, "info")
    assert hasattr(logger, "bind")


def test_configure_logging_writes_to_file(tmp_path: Path) -> None:
    """log_file 不为 None 时必须把日志写到文件。"""
    log_file = tmp_path / "test.log"
    configure_logging(level="INFO", json_output=True, log_file=log_file)

    logger = get_logger("test.file")
    logger.info("written_to_file")

    # structlog 默认会缓存首次配置，这里强制 flush 不容易；
    # 但配置后立即 log，应已落盘（_open_file 在配置时打开）
    content = log_file.read_text(encoding="utf-8")
    assert "written_to_file" in content


def test_configure_logging_log_file_creates_parent_dir(tmp_path: Path) -> None:
    """log_file 父目录不存在时必须自动创建。"""
    log_file = tmp_path / "nested" / "deep" / "test.log"
    assert not log_file.parent.exists()
    configure_logging(level="INFO", json_output=False, log_file=log_file)
    assert log_file.parent.exists()
