from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class LogContext:
    """Validated correlation fields bound to one execution context."""

    execution_id: str | None = None
    session_id: str | None = None
    turn_id: str | None = None
    trace_id: str | None = None
    span_id: str | None = None
    agent_name: str | None = None
    tool_code: str | None = None

    def __post_init__(self) -> None:
        """Reject correlation values that cannot be logged safely."""
        for field_name in LOG_CONTEXT_FIELDS:
            value = getattr(self, field_name)
            if value is None:
                continue
            if not isinstance(value, str):
                raise TypeError("log context values must be strings or None")
            if not value.strip():
                raise ValueError(f"log context field {field_name} must not be empty")
            if len(value) > 256:
                raise ValueError(f"log context field {field_name} must not exceed 256 characters")


LOG_CONTEXT_FIELDS = (
    "execution_id",
    "session_id",
    "turn_id",
    "trace_id",
    "span_id",
    "agent_name",
    "tool_code",
)
LOG_CONTEXT_FIELD_SET = frozenset(LOG_CONTEXT_FIELDS)
LOG_CONTEXT_VAR: ContextVar[LogContext] = ContextVar("pleiad_log_context")


def current_log_context() -> LogContext:
    """Return the immutable context bound to the current execution context.

    Returns:
        The current correlation context, with all fields unset by default.
    """
    return LOG_CONTEXT_VAR.get(LogContext())


@contextmanager
def bind_log_context(**fields: str | None) -> Iterator[LogContext]:
    """Temporarily bind validated correlation fields and restore them on exit.

    Args:
        **fields: Correlation fields to replace in the current context.

    Yields:
        The context active inside the binding.

    Raises:
        TypeError: If a value is not a string or ``None``.
        ValueError: If a field is unknown or a value is empty or too long.
    """
    unknown = fields.keys() - LOG_CONTEXT_FIELD_SET
    if unknown:
        raise ValueError(f"unknown log context fields: {', '.join(sorted(unknown))}")
    current = current_log_context()
    context = LogContext(
        execution_id=fields.get("execution_id", current.execution_id),
        session_id=fields.get("session_id", current.session_id),
        turn_id=fields.get("turn_id", current.turn_id),
        trace_id=fields.get("trace_id", current.trace_id),
        span_id=fields.get("span_id", current.span_id),
        agent_name=fields.get("agent_name", current.agent_name),
        tool_code=fields.get("tool_code", current.tool_code),
    )
    token = LOG_CONTEXT_VAR.set(context)
    try:
        yield context
    finally:
        LOG_CONTEXT_VAR.reset(token)
