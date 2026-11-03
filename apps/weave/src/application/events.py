import asyncio
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from threading import RLock
from uuid import uuid4

from opentelemetry.trace import get_current_span
from pleiad.events.contracts import Event
from pleiad.events.message import (
    MessageCompletedEvent,
    MessageRefusalDeltaEvent,
    MessageTextDeltaEvent,
    MessageToolCallDeltaEvent,
)
from pleiad.logging.context import current_log_context

from pleiad_weave.infrastructure.storage.models import ExecutionEventInput, ExecutionEventRecord
from pleiad_weave.infrastructure.storage.repository import ExecutionRepository

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ExecutionEvent:
    """Keep product event order independent from SDK Run and Message identities."""

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
    ) -> "ExecutionEvent":
        """Construct a product event with a unique identity."""
        return cls(
            uuid4().hex,
            execution_id,
            event_type,
            datetime.now(UTC),
            {} if data is None else dict(data),
            sequence,
            trace_id,
            span_id,
        )


ExecutionEventListener = Callable[[ExecutionEvent], None]


class ExecutionEventBus:
    """Dispatch committed events to process-local UI subscribers."""

    def __init__(self) -> None:
        """Create an empty listener set."""
        self.listeners: list[ExecutionEventListener] = []
        self.lock = RLock()

    def subscribe(self, listener: ExecutionEventListener) -> None:
        """Register a listener for future product events."""
        with self.lock:
            self.listeners.append(listener)

    def unsubscribe(self, listener: ExecutionEventListener) -> None:
        """Detach a previously registered listener."""
        with self.lock:
            if listener in self.listeners:
                self.listeners.remove(listener)

    def publish(self, event: ExecutionEvent) -> None:
        """Dispatch one event without allowing UI failures to alter Execution state."""
        with self.lock:
            listeners = tuple(self.listeners)
        for listener in listeners:
            try:
                listener(event)
            except Exception:
                logger.exception(f"Execution event listener failed: {event.event_type}")


def current_trace_ids(execution_id: str) -> tuple[str | None, str | None]:
    """Read actual OpenTelemetry IDs only in the matching product scope."""
    if current_log_context().execution_id != execution_id:
        return None, None
    context = get_current_span().get_span_context()
    if not context.is_valid:
        return None, None
    return f"{context.trace_id:032x}", f"{context.span_id:016x}"


@dataclass(slots=True)
class ExecutionEventPublisher:
    """Persist SDK lifecycle metadata without retaining sensitive Message bodies."""

    repository: ExecutionRepository
    event_bus: ExecutionEventBus

    async def emit(
        self,
        execution_id: str,
        event_type: str,
        data: Mapping[str, object] | None = None,
        *,
        live_data: Mapping[str, object] | None = None,
    ) -> ExecutionEvent:
        """Append product metadata before publishing to live subscribers."""
        trace_id, span_id = current_trace_ids(execution_id)
        return await self.append(
            execution_id,
            ExecutionEventInput(
                event_type=event_type, data={} if data is None else dict(data), trace_id=trace_id, span_id=span_id
            ),
            live_data=live_data,
        )

    async def publish_sdk_event(
        self, execution_id: str, event: Event, *, trace_ids: tuple[str | None, str | None] | None = None
    ) -> None:
        """Correlate a current SDK event with its host-owned durable Execution."""
        trace_id, span_id = current_trace_ids(execution_id) if trace_ids is None else trace_ids
        if isinstance(event, MessageTextDeltaEvent | MessageToolCallDeltaEvent | MessageRefusalDeltaEvent):
            self.event_bus.publish(
                ExecutionEvent(
                    event.event_id,
                    execution_id,
                    event.kind,
                    event.timestamp,
                    event.model_dump(mode="json"),
                    trace_id=trace_id,
                    span_id=span_id,
                )
            )
            return
        data = event.model_dump(mode="json", exclude={"event_id", "timestamp", "kind"})
        if isinstance(event, MessageCompletedEvent):
            data.pop("message")
            data["message_id"] = event.message.message_id
            data["role"] = event.message.role
        await self.append(
            execution_id,
            ExecutionEventInput(
                event_type=event.kind,
                data=data,
                event_id=event.event_id,
                timestamp=event.timestamp,
                trace_id=trace_id,
                span_id=span_id,
            ),
        )

    async def append(
        self, execution_id: str, event: ExecutionEventInput, *, live_data: Mapping[str, object] | None = None
    ) -> ExecutionEvent:
        """Finish an owned durable write before propagating cancellation."""
        task = asyncio.create_task(
            asyncio.to_thread(
                self.repository.append_event,
                execution_id,
                event.event_type,
                event.data,
                event_id=event.event_id,
                timestamp=event.timestamp,
                trace_id=event.trace_id,
                span_id=event.span_id,
            )
        )
        cancelled = False
        try:
            record = await asyncio.shield(task)
        except asyncio.CancelledError:
            record = await task
            cancelled = True
        projected = execution_event_from_record(record)
        if live_data is not None:
            projected = replace(projected, data=dict(live_data))
        self.event_bus.publish(projected)
        if cancelled:
            raise asyncio.CancelledError
        return projected


def execution_event_from_record(record: ExecutionEventRecord) -> ExecutionEvent:
    """Project a committed journal row into the live event contract."""
    return ExecutionEvent(
        record.event_id,
        record.execution_id,
        record.event_type,
        record.timestamp,
        record.data,
        record.sequence,
        record.trace_id,
        record.span_id,
    )
