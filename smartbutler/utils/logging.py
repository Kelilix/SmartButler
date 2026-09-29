"""结构化日志基础设施（按文档 §4.5）。

选型：structlog（与 LangChain / FastAPI 生态一致）。
- 控制台开发模式：彩色人类可读
- 生产模式：JSON 格式，便于采集到 ELK
- 单一入口 configure_logging()：所有模块启动时调用一次
- 工厂函数 get_logger(name)：业务代码获取 logger 实例

模式：stdlib mode
- structlog 把渲染后的消息交给 stdlib logging
- stdlib logging 统一输出到 stdout 或文件
- 这样第三方库的日志（如 langchain、fastapi）能汇聚到同一管道

本模块不直接依赖 config（避免循环依赖），调用方需显式传入 LoggingSettings 或参数。
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

import structlog

from smartbutler.config.logging import LoggingSettings


def configure_logging(
    settings: LoggingSettings | None = None,
    *,
    level: str | None = None,
    json_output: bool | None = None,
    log_file: Path | None = None,
) -> None:
    """配置全局 structlog（stdlib 模式）。

    Args:
        settings: 完整的 LoggingSettings。传入时优先使用其中的字段。
        level/json_output/log_file: 单独覆盖任一字段（用于单元测试快速配置）。
    """
    # 解析最终配置
    if settings is not None:
        level = level if level is not None else settings.level
        json_output = json_output if json_output is not None else settings.json_output
        log_file = log_file if log_file is not None else settings.log_file
    level = (level or "INFO").upper()
    json_output = bool(json_output) if json_output is not None else False

    numeric_level = getattr(logging, level, logging.INFO)

    # ---- 1. 配置 stdlib logging（接管根 logger）----
    root_logger = logging.getLogger()
    # 清除已有的 handler，避免重复输出
    for h in list(root_logger.handlers):
        root_logger.removeHandler(h)
    root_logger.setLevel(numeric_level)

    formatter = logging.Formatter("%(message)s")

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setLevel(numeric_level)
    stream_handler.setFormatter(formatter)
    root_logger.addHandler(stream_handler)

    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setLevel(numeric_level)
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)

    # ---- 2. 配置 structlog（stdlib 模式）----
    timestamper = structlog.processors.TimeStamper(fmt="iso", utc=True)
    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        timestamper,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
    ]

    if json_output:
        renderer: structlog.types.Processor = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=True)

    final_processors: list[structlog.types.Processor] = [
        *shared_processors,
        structlog.processors.format_exc_info,
        renderer,
    ]

    structlog.configure(
        processors=final_processors,
        wrapper_class=structlog.make_filtering_bound_logger(numeric_level),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None, **initial_values: Any) -> structlog.stdlib.BoundLogger:
    """获取一个带名字和初始字段的 logger。

    用法::

        logger = get_logger(__name__)
        logger.info("agent.invoked", agent_name="home", request_id=req.id)
    """
    bound: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    if initial_values:
        bound = bound.bind(**initial_values)
    return bound


__all__ = ["configure_logging", "get_logger"]
