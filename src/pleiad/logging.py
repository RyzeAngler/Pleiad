"""Application logging configuration and execution-context propagation."""

from __future__ import annotations

import inspect
import logging as stdlib_logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from loguru import logger

_DEFAULT_EXTRA = {
    "execution_id": "-",
    "trace_id": "-",
    "span_id": "-",
}
_FILE_FORMAT = (
    "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | "
    "{name}:{function}:{line} | execution={extra[execution_id]} "
    "trace={extra[trace_id]} span={extra[span_id]} | {message}\n{exception}"
)
_CONSOLE_FORMAT = (
    "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
    "<level>{level: <8}</level> | "
    "<cyan>{name}:{function}:{line}</cyan> | "
    "execution={extra[execution_id]} trace={extra[trace_id]} "
    "span={extra[span_id]} | <level>{message}</level>\n{exception}"
)


def _is_project_logger(name: str) -> bool:
    """Return whether a standard-library logger belongs to Pleiad."""
    return name == "pleiad" or name.startswith("pleiad.")


class InterceptHandler(stdlib_logging.Handler):
    """Forward standard-library records into the configured Loguru sinks."""

    def emit(self, record: stdlib_logging.LogRecord) -> None:
        """Forward one record while preserving its original exception."""
        if not _is_project_logger(record.name):
            return

        try:
            level: str | int = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno

        frame = inspect.currentframe()
        depth = 6
        while frame is not None and frame.f_code.co_filename == stdlib_logging.__file__:
            frame = frame.f_back
            depth += 1
        logger.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())


def configure_logging(
    *,
    log_level: str = "INFO",
    log_file: Path | None = None,
    console: bool = True,
) -> None:
    """Configure process-wide Loguru sinks and standard-library interception."""
    level = _normalize_level(log_level)
    logger.remove()
    logger.configure(extra=dict(_DEFAULT_EXTRA))

    if console:
        logger.add(
            sys.stderr,
            level=level,
            format=_CONSOLE_FORMAT,
            colorize=sys.stderr.isatty(),
            enqueue=True,
            backtrace=True,
            diagnose=False,
        )
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        logger.add(
            log_file,
            level=level,
            format=_FILE_FORMAT,
            encoding="utf-8",
            rotation="10 MB",
            retention="7 days",
            enqueue=True,
            backtrace=True,
            diagnose=False,
        )

    _intercept_standard_logging(level)


@contextmanager
def logging_context(
    *,
    execution_id: str | None = None,
    trace_id: str | None = None,
    span_id: str | None = None,
) -> Iterator[None]:
    """Bind execution identifiers to all Loguru records in the current context."""
    values: dict[str, Any] = {
        key: value
        for key, value in {
            "execution_id": execution_id,
            "trace_id": trace_id,
            "span_id": span_id,
        }.items()
        if value is not None
    }
    with logger.contextualize(**values):
        yield


def _normalize_level(value: str) -> str:
    """Normalize and validate a Loguru-compatible level name."""
    normalized = value.upper().strip()
    if normalized not in {"TRACE", "DEBUG", "INFO", "SUCCESS", "WARNING", "ERROR", "CRITICAL"}:
        raise ValueError(f"unsupported log level: {value}")
    return normalized


def _intercept_standard_logging(level: str) -> None:
    """Route dependency loggers through the same configured sinks."""
    handler = InterceptHandler()
    root = stdlib_logging.getLogger()
    root.handlers = [handler]
    root.setLevel({"TRACE": stdlib_logging.DEBUG, "SUCCESS": stdlib_logging.INFO}.get(level, level))
    for value in stdlib_logging.root.manager.loggerDict.values():
        if isinstance(value, stdlib_logging.Logger):
            value.handlers = []
            value.propagate = True
            value.setLevel(stdlib_logging.NOTSET)
