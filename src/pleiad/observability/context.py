"""Context-local identifiers shared by Runtime Event, Logger, and Trace."""

from contextvars import ContextVar, Token
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ObservabilityContext:
    """Current execution and trace identifiers for one async context."""

    execution_id: str
    trace_id: str
    span_id: str | None = None


_CURRENT_CONTEXT: ContextVar[ObservabilityContext | None] = ContextVar(
    "pleiad_observability_context",
    default=None,
)


def current_observability_context() -> ObservabilityContext | None:
    """Return the trace context attached to the current async task."""
    return _CURRENT_CONTEXT.get()


def set_observability_context(
    context: ObservabilityContext | None,
) -> Token[ObservabilityContext | None]:
    """Set the current context and return its reset token."""
    return _CURRENT_CONTEXT.set(context)


def reset_observability_context(token: Token[ObservabilityContext | None]) -> None:
    """Restore a context using a token returned by the setter."""
    _CURRENT_CONTEXT.reset(token)
