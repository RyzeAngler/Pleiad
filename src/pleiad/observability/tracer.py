"""Context-aware Trace and Span lifecycle management."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from contextvars import Token
from datetime import UTC, datetime
from types import TracebackType
from typing import Any, Literal
from uuid import uuid4

from loguru import logger

from pleiad.errors import ErrorCode, ErrorInfo, PleiadError, make_error
from pleiad.observability.context import (
    ObservabilityContext,
    current_observability_context,
    reset_observability_context,
    set_observability_context,
)
from pleiad.observability.models import (
    SpanKind,
    SpanStatus,
    TraceLink,
    TraceRecord,
    TraceRecordType,
    TraceStatus,
)
from pleiad.observability.processor import TraceProcessor


class Tracer:
    """Create execution scopes and publish lifecycle facts to a processing pipeline."""

    def __init__(self, processor: TraceProcessor) -> None:
        self.processor = processor

    def trace(
        self,
        execution_id: str,
        *,
        conversation_id: str = "default",
        name: str = "execution",
        attributes: Mapping[str, Any] | None = None,
    ) -> TraceScope:
        """Create a root Trace scope for one Execution."""
        values = dict(attributes or {})
        values.setdefault("conversation_id", conversation_id)
        return TraceScope(
            self,
            execution_id=execution_id,
            trace_id=uuid4().hex,
            name=name,
            attributes=values,
        )

    def span(
        self,
        name: str,
        *,
        kind: SpanKind = SpanKind.SYSTEM,
        attributes: Mapping[str, Any] | None = None,
    ) -> SpanScope:
        """Create a child Span inside the current Trace context."""
        context = current_observability_context()
        if context is None:
            raise RuntimeError("cannot create a span without an active trace")
        return SpanScope(
            self,
            context=context,
            span_id=uuid4().hex,
            name=name,
            kind=kind,
            attributes=dict(attributes or {}),
        )

    def _record(
        self,
        *,
        record_type: TraceRecordType,
        context: ObservabilityContext,
        span_id: str | None,
        parent_span_id: str | None,
        kind: SpanKind,
        name: str,
        status: TraceStatus,
        attributes: Mapping[str, Any] | None = None,
        error: ErrorInfo | None = None,
        source_trace_id: str | None = None,
        source_span_id: str | None = None,
        target_trace_id: str | None = None,
        target_span_id: str | None = None,
        relation: str | None = None,
        correlation_id: str | None = None,
    ) -> None:
        self.processor.record(
            TraceRecord(
                sequence=0,
                record_type=record_type,
                timestamp=datetime.now(UTC),
                execution_id=context.execution_id,
                trace_id=context.trace_id,
                span_id=span_id,
                parent_span_id=parent_span_id,
                kind=kind,
                name=name,
                status=status,
                attributes=dict(attributes or {}),
                error=error,
                source_trace_id=source_trace_id,
                source_span_id=source_span_id,
                target_trace_id=target_trace_id,
                target_span_id=target_span_id,
                relation=relation,
                correlation_id=correlation_id,
            )
        )

    def link(
        self,
        target_trace_id: str,
        *,
        target_span_id: str | None = None,
        source_span_id: str | None = None,
        relation: str = "follows",
        correlation_id: str | None = None,
    ) -> TraceLink:
        """Record a non-parent relationship from the current Trace context."""
        context = current_observability_context()
        if context is None:
            raise RuntimeError("cannot create a Trace link without an active trace")
        link = TraceLink(
            source_trace_id=context.trace_id,
            source_span_id=source_span_id or context.span_id,
            target_trace_id=target_trace_id,
            target_span_id=target_span_id,
            relation=relation,
            correlation_id=correlation_id,
        )
        self._record(
            record_type=TraceRecordType.LINK_CREATED,
            context=context,
            span_id=context.span_id,
            parent_span_id=None,
            kind=SpanKind.SYSTEM,
            name=relation,
            status=TraceStatus.RUNNING,
            attributes={},
            source_trace_id=link.source_trace_id,
            source_span_id=link.source_span_id,
            target_trace_id=link.target_trace_id,
            target_span_id=link.target_span_id,
            relation=link.relation,
            correlation_id=link.correlation_id,
        )
        return link


class TraceScope:
    """Synchronous context manager for one root Trace."""

    def __init__(
        self,
        tracer: Tracer,
        *,
        execution_id: str,
        trace_id: str,
        name: str,
        attributes: dict[str, Any],
    ) -> None:
        self.tracer = tracer
        self.execution_id = execution_id
        self.trace_id = trace_id
        self.name = name
        self.attributes = attributes
        self.status = TraceStatus.RUNNING
        self.error: ErrorInfo | None = None
        self._context_token: Token[ObservabilityContext | None] | None = None
        self._logging_scope: Any = None

    def __enter__(self) -> TraceScope:
        context = ObservabilityContext(self.execution_id, self.trace_id)
        self._context_token = set_observability_context(context)
        self._logging_scope = logger.contextualize(
            execution_id=self.execution_id,
            trace_id=self.trace_id,
            span_id="-",
        )
        self._logging_scope.__enter__()
        self.tracer._record(
            record_type=TraceRecordType.TRACE_STARTED,
            context=context,
            span_id=None,
            parent_span_id=None,
            kind=SpanKind.SYSTEM,
            name=self.name,
            status=self.status,
            attributes=self.attributes,
        )
        return self

    def set_status(self, status: TraceStatus, error: ErrorInfo | None = None) -> None:
        """Set the terminal status written when the scope closes."""
        self.status = status
        self.error = error

    def span_event(self, name: str, attributes: Mapping[str, Any] | None = None) -> None:
        """Record an event attached to the root Trace."""
        context = current_observability_context()
        if context is None:
            return
        self.tracer._record(
            record_type=TraceRecordType.SPAN_EVENT,
            context=context,
            span_id=context.span_id,
            parent_span_id=None,
            kind=SpanKind.SYSTEM,
            name=name,
            status=self.status,
            attributes=attributes,
        )

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        """Close the root Trace and flush its local records."""
        if exc_type is not None and self.status is TraceStatus.RUNNING:
            self.status = _status_for_exception(exc_type)
            self.error = _error_for_exception(exc_value, exc_type)
        elif self.status is TraceStatus.RUNNING:
            self.status = TraceStatus.SUCCEEDED
        context = ObservabilityContext(self.execution_id, self.trace_id)
        self.tracer._record(
            record_type=TraceRecordType.TRACE_ENDED,
            context=context,
            span_id=None,
            parent_span_id=None,
            kind=SpanKind.SYSTEM,
            name=self.name,
            status=self.status,
            attributes=self.attributes,
            error=self.error,
        )
        if self._context_token is not None:
            reset_observability_context(self._context_token)
        if self._logging_scope is not None:
            self._logging_scope.__exit__(exc_type, exc_value, traceback)
        return False


class SpanScope:
    """Synchronous context manager for one nested runtime Span."""

    def __init__(
        self,
        tracer: Tracer,
        *,
        context: ObservabilityContext,
        span_id: str,
        name: str,
        kind: SpanKind,
        attributes: dict[str, Any],
    ) -> None:
        self.tracer = tracer
        self.context = context
        self.span_id = span_id
        self.name = name
        self.kind = kind
        self.attributes = attributes
        self.status = SpanStatus.RUNNING
        self.error: ErrorInfo | None = None
        self._context_token: Token[ObservabilityContext | None] | None = None
        self._logging_scope: Any = None

    def __enter__(self) -> SpanScope:
        self.tracer._record(
            record_type=TraceRecordType.SPAN_STARTED,
            context=self.context,
            span_id=self.span_id,
            parent_span_id=self.context.span_id,
            kind=self.kind,
            name=self.name,
            status=TraceStatus.RUNNING,
            attributes=self.attributes,
        )
        child_context = ObservabilityContext(
            self.context.execution_id,
            self.context.trace_id,
            self.span_id,
        )
        self._context_token = set_observability_context(child_context)
        self._logging_scope = logger.contextualize(span_id=self.span_id)
        self._logging_scope.__enter__()
        return self

    def set_status(self, status: SpanStatus, error: ErrorInfo | None = None) -> None:
        """Set the status written when the span closes."""
        self.status = status
        self.error = error

    def set_attribute(self, name: str, value: object) -> None:
        """Add one low-cardinality diagnostic attribute."""
        self.attributes[name] = value

    def set_attributes(self, attributes: Mapping[str, Any]) -> None:
        """Add a bounded set of diagnostic attributes before the span closes."""
        self.attributes.update(attributes)

    def event(self, name: str, attributes: Mapping[str, Any] | None = None) -> None:
        """Attach a timestamped event to this Span."""
        context = current_observability_context() or self.context
        self.tracer._record(
            record_type=TraceRecordType.SPAN_EVENT,
            context=context,
            span_id=self.span_id,
            parent_span_id=self.context.span_id,
            kind=self.kind,
            name=name,
            status=TraceStatus(self.status.value),
            attributes=attributes,
        )

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        """Close the span and restore its parent context."""
        if exc_type is not None and self.status is SpanStatus.RUNNING:
            if _is_cancelled(exc_type):
                self.status = SpanStatus.CANCELLED
            elif issubclass(exc_type, TimeoutError):
                self.status = SpanStatus.TIMEOUT
            else:
                self.status = SpanStatus.FAILED
            self.error = _error_for_exception(exc_value, exc_type)
        elif self.status is SpanStatus.RUNNING:
            self.status = SpanStatus.SUCCEEDED
        self.tracer._record(
            record_type=TraceRecordType.SPAN_ENDED,
            context=self.context,
            span_id=self.span_id,
            parent_span_id=self.context.span_id,
            kind=self.kind,
            name=self.name,
            status=TraceStatus(self.status.value),
            attributes=self.attributes,
            error=self.error,
        )
        if self._context_token is not None:
            reset_observability_context(self._context_token)
        if self._logging_scope is not None:
            self._logging_scope.__exit__(exc_type, exc_value, traceback)
        return False


def _status_for_exception(exc_type: type[BaseException]) -> TraceStatus:
    """Map cancellation and ordinary exceptions to Trace status."""
    if issubclass(exc_type, TimeoutError):
        return TraceStatus.TIMEOUT
    return TraceStatus.CANCELLED if _is_cancelled(exc_type) else TraceStatus.FAILED


def _is_cancelled(exc_type: type[BaseException]) -> bool:
    """Return whether an exception type represents cooperative cancellation."""
    return issubclass(exc_type, asyncio.CancelledError)


def _error_for_exception(
    exc_value: BaseException | None,
    exc_type: type[BaseException],
) -> ErrorInfo:
    """Project an exception into the existing unified error vocabulary."""
    if isinstance(exc_value, PleiadError):
        return exc_value.info
    if _is_cancelled(exc_type):
        return make_error(ErrorCode.AGENT_CANCELLED)
    return make_error(
        ErrorCode.SYSTEM_UNEXPECTED,
        details={"exception": exc_type.__name__},
    )
