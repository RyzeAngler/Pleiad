"""Runtime-facing observability port and safe scope helpers."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager, nullcontext
from typing import Any, Protocol

from pleiad.observability.context import current_observability_context
from pleiad.observability.models import SpanKind, TraceLink
from pleiad.observability.tracer import SpanScope, TraceScope


class ObservabilityPort(Protocol):
    """The only observability surface Runtime execution code needs."""

    def trace(
        self,
        execution_id: str,
        *,
        conversation_id: str = "default",
        name: str = "execution",
        attributes: Mapping[str, Any] | None = None,
    ) -> TraceScope:
        """Open a root execution Trace."""

    def span(
        self,
        name: str,
        *,
        kind: SpanKind = SpanKind.SYSTEM,
        attributes: Mapping[str, Any] | None = None,
    ) -> SpanScope:
        """Open a child operation Span."""

    def link(
        self,
        target_trace_id: str,
        *,
        target_span_id: str | None = None,
        source_span_id: str | None = None,
        relation: str = "follows",
        correlation_id: str | None = None,
    ) -> TraceLink:
        """Record an explicit non-parent relationship."""


@contextmanager
def observe_span(
    observability: ObservabilityPort | None,
    name: str,
    *,
    kind: SpanKind,
    attributes: Mapping[str, Any] | None = None,
) -> Iterator[SpanScope | None]:
    """Open a child Span only when a root Trace is active."""
    if observability is None or current_observability_context() is None:
        with nullcontext() as scope:
            yield scope
        return
    with observability.span(name, kind=kind, attributes=attributes) as scope:
        yield scope
