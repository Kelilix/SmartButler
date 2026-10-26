"""日志统一管线单元测试。

回归重点:
- TTSOutputAdapter 的 structlog kwargs 写法不再抛
  ``TypeError: Logger._log() got an unexpected keyword argument 'error'``
- structlog 与 stdlib 两条管线输出格式一致(同一 ProcessorFormatter)
- stdlib 的 extra={...} 字段能进结构化输出
- exc_info 正常渲染
"""

from __future__ import annotations

import json
import logging
import tempfile
from pathlib import Path
from typing import Any

import pytest

from smartbutler.config import LoggingSettings
from smartbutler.utils import configure_logging, get_logger


def _close_root_handlers() -> None:
    """关闭并移除根 logger 上的所有 handler(释放文件句柄)。"""
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
        h.close()


@pytest.fixture(autouse=True)
def _reset_logging() -> None:
    """每个测试后恢复根 logger 状态,避免污染其他测试。"""
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    yield
    for h in list(root.handlers):
        root.removeHandler(h)
        # 必须 close():FileHandler 持有文件句柄,Windows 上不关会导致
        # 临时目录清理时 PermissionError(WinError 32)。
        h.close()
    for h in saved_handlers:
        root.addHandler(h)
    root.setLevel(saved_level)


def _capture_json(capsys: pytest.CaptureFixture[str]) -> list[dict[str, Any]]:
    """捕获 stdout 并解析出 JSON 行。"""
    out = capsys.readouterr().out
    parsed: list[dict[str, Any]] = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return parsed


# ---------------------------------------------------------------------------
# 回归: tts_output_adapter 的日志调用
# ---------------------------------------------------------------------------


async def test_tts_output_adapter_logger_kwargs_does_not_raise() -> None:
    """``logger.error(..., error=..., content=...)`` 不应抛 TypeError。

    这是本次修复的核心回归:该文件原先用 stdlib logger 却按 structlog 的
    kwargs 写法传参,异常处理路径自身先炸,把真正的原始异常掩盖掉。
    """
    from smartbutler.server.adapters import ButlerReply
    from smartbutler.voice.adapter.tts_output_adapter import TTSOutputAdapter
    from smartbutler.voice.player.pygame_player import FakeAudioPlayer
    from smartbutler.voice.tts.edge_tts import FakeTTS

    class _BoomTTS(FakeTTS):
        async def speak(self, text: str, voice: Any = None) -> Any:
            raise RuntimeError("tts exploded")
            yield b""  # pragma: no cover - generator marker

    adapter = TTSOutputAdapter(tts=_BoomTTS(), player=FakeAudioPlayer())
    reply = ButlerReply(content="测试文本", user_id="u1")

    with pytest.raises(Exception) as exc_info:
        await adapter.push(reply)

    # 关键:抛出的应该是 TTSOutputError,而不是 Logger._log() 的 TypeError
    assert "unexpected keyword argument" not in str(exc_info.value)
    assert "tts exploded" in str(exc_info.value)


# ---------------------------------------------------------------------------
# 统一管线
# ---------------------------------------------------------------------------


def test_structlog_and_stdlib_share_same_format(capsys: pytest.CaptureFixture[str]) -> None:
    """structlog 与 stdlib 的日志应输出成同一种格式。"""
    configure_logging(LoggingSettings(level="INFO", json_output=True, log_file=None))

    get_logger("mod.structlog").info("structlog_line", foo="bar")
    logging.getLogger("mod.stdlib").info("stdlib_line")

    records = _capture_json(capsys)
    events = {r.get("event") for r in records}

    assert "structlog_line" in events
    assert "stdlib_line" in events
    # 两条记录都应带有 structlog 管线注入的统一字段
    for r in records:
        if r.get("event") in {"structlog_line", "stdlib_line"}:
            assert "timestamp" in r
            assert "level" in r
            assert "logger" in r


def test_stdlib_extra_is_included(capsys: pytest.CaptureFixture[str]) -> None:
    """stdlib 的 extra={...} 字段应进入结构化输出。"""
    configure_logging(LoggingSettings(level="INFO", json_output=True, log_file=None))

    logging.getLogger("mod.extra").info("extra_line", extra={"device_id": "speaker_01"})

    records = _capture_json(capsys)
    target = next(r for r in records if r.get("event") == "extra_line")
    assert target["device_id"] == "speaker_01"


def test_structlog_kwargs_are_included(capsys: pytest.CaptureFixture[str]) -> None:
    """structlog 的 kwargs 应作为顶层字段输出。"""
    configure_logging(LoggingSettings(level="INFO", json_output=True, log_file=None))

    get_logger("mod.kwargs").info("kwargs_line", error="boom", content="hi")

    records = _capture_json(capsys)
    target = next(r for r in records if r.get("event") == "kwargs_line")
    assert target["error"] == "boom"
    assert target["content"] == "hi"


def test_exc_info_is_rendered(capsys: pytest.CaptureFixture[str]) -> None:
    """exc_info=True 应渲染出 traceback。"""
    configure_logging(LoggingSettings(level="INFO", json_output=True, log_file=None))

    try:
        raise ValueError("boom-original")
    except ValueError:
        get_logger("mod.exc").error("exc_line", exc_info=True)

    records = _capture_json(capsys)
    target = next(r for r in records if r.get("event") == "exc_line")
    assert "exception" in target
    assert "boom-original" in target["exception"]


def test_console_mode_is_readable(capsys: pytest.CaptureFixture[str]) -> None:
    """json_output=False 时输出人类可读格式(不是 JSON)。"""
    configure_logging(LoggingSettings(level="INFO", json_output=False, log_file=None))

    get_logger("mod.console").info("console_line", foo="bar")

    out = capsys.readouterr().out
    assert "console_line" in out
    assert "foo" in out


def test_log_file_written() -> None:
    """log_file 指定时,日志应同时写入文件。"""
    with tempfile.TemporaryDirectory() as tmp:
        log_file = Path(tmp) / "nested" / "app.log"
        configure_logging(LoggingSettings(level="INFO", json_output=True, log_file=log_file))

        get_logger("mod.file").info("file_line", marker="x")

        assert log_file.exists()
        content = log_file.read_text(encoding="utf-8")
        assert "file_line" in content
        assert "marker" in content

        # 关闭 handler 才能释放文件句柄 —— Windows 上不关的话,
        # TemporaryDirectory 清理时会报 PermissionError(WinError 32)。
        _close_root_handlers()
