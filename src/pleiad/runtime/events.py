"""Small synchronous event bus shared by API and runtime components."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from threading import RLock
from uuid import uuid4

from loguru import logger

from pleiad.logging import logging_context
from pleiad.observability.context import current_observability_context


@dataclass(frozen=True, slots=True)
class RuntimeEvent:
    """One immutable observation emitted by an Execution."""

    event_id: str
    execution_id: str
    event_type: str
    timestamp: datetime
    data: Mapping[str, object] = field(default_factory=dict)
    sequence: int = 0
    trace_id: str | None = None
    span_id: str | None = None

    @classmethod
    def create(
        cls,
        execution_id: str,
        event_type: str,
        data: Mapping[str, object] | None = None,
        *,
        sequence: int = 0,
        trace_id: str | None = None,
        span_id: str | None = None,
    ) -> "RuntimeEvent":
        """Create an event with a sortable timestamp and unique identifier."""
        return cls(
            event_id=uuid4().hex,
            execution_id=execution_id,
            event_type=event_type,
            timestamp=datetime.now(UTC),
            sequence=sequence,
            trace_id=trace_id,
            span_id=span_id,
            data=dict(data or {}),
        )


EventListener = Callable[[RuntimeEvent], None]


class EventBus:
    """Thread-safe in-process event publisher with replayable history."""

    def __init__(self) -> None:
        self._events: dict[str, list[RuntimeEvent]] = {}
        self._sequences: dict[str, int] = {}
        self._listeners: list[EventListener] = []
        self._lock = RLock()

    def subscribe(self, listener: EventListener) -> None:
        """Register a listener for future events."""
        with self._lock:
            self._listeners.append(listener)

    def unsubscribe(self, listener: EventListener) -> None:
        """Remove one listener if it is currently subscribed."""
        with self._lock:
            if listener in self._listeners:
                self._listeners.remove(listener)

    def emit(
        self,
        execution_id: str,
        event_type: str,
        data: Mapping[str, object] | None = None,
        *,
        trace_id: str | None = None,
        span_id: str | None = None,
    ) -> RuntimeEvent:
        """Publish and retain one event."""
        context = current_observability_context()
        if context is not None and context.execution_id == execution_id:
            trace_id = trace_id or context.trace_id
            span_id = span_id or context.span_id
        with self._lock:
            sequence = self._sequences.get(execution_id, 0) + 1
            self._sequences[execution_id] = sequence
            event = RuntimeEvent.create(
                execution_id,
                event_type,
                data,
                sequence=sequence,
                trace_id=trace_id,
                span_id=span_id,
            )
            self._events.setdefault(execution_id, []).append(event)
            listeners = tuple(self._listeners)
        for listener in listeners:
            try:
                listener(event)
            except Exception:
                with logging_context(
                    execution_id=execution_id,
                    trace_id=trace_id,
                    span_id=span_id,
                ):
                    logger.exception("Runtime Event listener 执行失败: {}", event_type)
        with logging_context(
            execution_id=execution_id,
            trace_id=trace_id,
            span_id=span_id,
        ):
            logger.debug("Runtime Event: {}", event_type)
        return event

    def list(self, execution_id: str) -> tuple[RuntimeEvent, ...]:
        """Return a stable snapshot of one execution's events."""
        with self._lock:
            return tuple(self._events.get(execution_id, ()))
