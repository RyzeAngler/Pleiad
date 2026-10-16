import asyncio
import io
import logging
from collections.abc import Callable, Iterator
from pathlib import Path
from uuid import uuid4

import pytest

from pleiad.logging.context import (
    LOG_CONTEXT_FIELDS,
    LogContext,
    bind_log_context,
    current_log_context,
)
from pleiad.logging.filters import ContextFilter
from pleiad.logging.rotation import create_rotating_file_handler


@pytest.fixture
def local_logger() -> Iterator[tuple[logging.Logger, io.StringIO]]:
    logger = logging.getLogger(f"test.pleiad.{uuid4().hex}")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    logger.addHandler(handler)
    try:
        yield logger, stream
    finally:
        logger.removeHandler(handler)
        handler.close()


def test_log_context_starts_without_correlation_values() -> None:
    assert current_log_context() == LogContext()


def test_log_context_rejects_unknown_empty_and_oversized_values() -> None:
    with pytest.raises(ValueError, match="unknown log context fields"), bind_log_context(request_id="unknown"):
        pass
    with pytest.raises(ValueError, match="field execution_id must not be empty"), bind_log_context(execution_id=""):
        pass
    with pytest.raises(ValueError, match="field tool_code must not exceed 256"), bind_log_context(tool_code="x" * 257):
        pass


def test_log_context_rejects_invalid_values_when_constructed_directly() -> None:
    constructor: Callable[..., LogContext] = LogContext
    with pytest.raises(TypeError, match="strings or None"):
        constructor(execution_id=123)
    with pytest.raises(ValueError, match="field execution_id must not be empty"):
        LogContext(execution_id=" ")


def test_nested_log_context_restores_the_parent_values() -> None:
    with bind_log_context(execution_id="execution-1"):
        parent = current_log_context()
        with bind_log_context(session_id="session-1", turn_id="turn-1"):
            assert current_log_context() == LogContext(
                execution_id="execution-1",
                session_id="session-1",
                turn_id="turn-1",
            )
        assert current_log_context() == parent


def test_log_context_is_restored_after_exception() -> None:
    with pytest.raises(RuntimeError, match="failed"), bind_log_context(execution_id="execution-1"):
        raise RuntimeError("failed")
    assert current_log_context() == LogContext()


def test_explicit_none_clears_a_nested_log_context_value() -> None:
    with bind_log_context(execution_id="execution-1"):
        with bind_log_context(execution_id=None):
            assert current_log_context().execution_id is None
        assert current_log_context().execution_id == "execution-1"


def test_log_context_is_isolated_between_tasks() -> None:
    async def run_tasks() -> tuple[str | None, str | None]:
        barrier = asyncio.Barrier(2)

        async def read_context(execution_id: str) -> str | None:
            with bind_log_context(execution_id=execution_id):
                await barrier.wait()
                return current_log_context().execution_id

        first, second = await asyncio.gather(read_context("execution-1"), read_context("execution-2"))
        return first, second

    assert asyncio.run(run_tasks()) == ("execution-1", "execution-2")


def test_child_task_inherits_creation_context() -> None:
    async def run_child() -> str | None:
        async def read_context() -> str | None:
            return current_log_context().execution_id

        with bind_log_context(execution_id="parent-execution"):
            child = asyncio.create_task(read_context())
        return await child

    assert asyncio.run(run_child()) == "parent-execution"


def test_context_filter_adds_bound_correlation_fields(local_logger: tuple[logging.Logger, io.StringIO]) -> None:
    logger, _ = local_logger
    record = logger.makeRecord(
        logger.name,
        logging.INFO,
        __file__,
        1,
        "message",
        (),
        None,
        extra={"api_key": "test-value"},
    )

    with bind_log_context(execution_id="execution-1", tool_code="search"):
        assert ContextFilter().filter(record) is True

    values = vars(record)
    assert values["execution_id"] == "execution-1"
    assert values["tool_code"] == "search"
    assert {field: getattr(record, field) for field in LOG_CONTEXT_FIELDS} == {
        "execution_id": "execution-1",
        "session_id": None,
        "turn_id": None,
        "trace_id": None,
        "span_id": None,
        "agent_name": None,
        "tool_code": "search",
    }
    assert values["api_key"] == "test-value"
    assert record.msg == "message"
    assert record.args == ()


def test_context_filter_rejects_conflicting_record_identity(local_logger: tuple[logging.Logger, io.StringIO]) -> None:
    logger, _ = local_logger
    record = logger.makeRecord(
        logger.name,
        logging.INFO,
        __file__,
        1,
        "message",
        (),
        None,
        extra={"execution_id": "forged", "agent_name": "forged"},
    )

    with bind_log_context(execution_id="execution-1"):
        assert ContextFilter().filter(record) is True

    values = vars(record)
    assert values["execution_id"] == "execution-1"
    assert values["agent_name"] is None


@pytest.mark.parametrize(
    ("max_bytes", "backup_count", "error_type", "message"),
    [
        (0, 1, ValueError, "max_bytes must be a positive integer"),
        (20, -1, ValueError, "backup_count must be a non-negative integer"),
        (True, 1, TypeError, "max_bytes must be an integer, not a boolean"),
        (20, False, TypeError, "backup_count must be an integer, not a boolean"),
    ],
)
def test_rotating_handler_reports_the_invalid_limit(
    tmp_path: Path,
    max_bytes: int,
    backup_count: int,
    error_type: type[Exception],
    message: str,
) -> None:
    with pytest.raises(error_type, match=message):
        create_rotating_file_handler(
            tmp_path / "logs" / "pleiad.log",
            max_bytes=max_bytes,
            backup_count=backup_count,
        )


def test_rotating_handler_writes_utf8_text(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "logs" / "pleiad.log"
    handler = create_rotating_file_handler(path, max_bytes=4096, backup_count=2)
    try:
        handler.emit(logging.makeLogRecord({"msg": "日志记录", "args": (), "levelno": logging.INFO}))
    finally:
        handler.close()

    assert path.read_text(encoding="utf-8") == "日志记录\n"


def test_rotating_handler_keeps_the_configured_backup_count(tmp_path: Path) -> None:
    path = tmp_path / "pleiad.log"
    handler = create_rotating_file_handler(path, max_bytes=13, backup_count=1)
    try:
        for message in ("中文甲", "中文乙", "中文丙"):
            handler.emit(logging.makeLogRecord({"msg": message, "args": (), "levelno": logging.INFO}))
    finally:
        handler.close()

    assert path.read_text(encoding="utf-8") == "中文丙\n"
    assert (tmp_path / "pleiad.log.1").read_text(encoding="utf-8") == "中文乙\n"
    assert not (tmp_path / "pleiad.log.2").exists()


def test_handler_factory_does_not_configure_the_root_logger(tmp_path: Path) -> None:
    root = logging.getLogger()
    handlers = tuple(root.handlers)
    path = tmp_path / "logs" / "pleiad.log"
    handler = create_rotating_file_handler(path, max_bytes=1024, backup_count=0)
    try:
        assert path.parent.is_dir()
        assert tuple(root.handlers) == handlers
    finally:
        handler.close()
