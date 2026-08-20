"""Logger-context, Runtime Event, and local Trace behavior tests."""

from pathlib import Path

import pytest

from pleiad.observability import SpanKind, TraceRecordType, TraceService, create_tracer
from pleiad.observability.stores.jsonl import JsonlTraceStore
from pleiad.runtime.events import EventBus


def test_event_bus_assigns_execution_order_and_does_not_trust_listeners() -> None:
    """A broken observer cannot interrupt the runtime publisher."""
    events = EventBus()

    def broken_listener(event: object) -> None:
        raise RuntimeError("observer failed")

    events.subscribe(broken_listener)
    first = events.emit("execution-1", "execution.started")
    second = events.emit("execution-1", "execution.completed")

    assert (first.sequence, second.sequence) == (1, 2)
    assert [event.event_type for event in events.list("execution-1")] == [
        "execution.started",
        "execution.completed",
    ]


def test_tracer_writes_root_nested_span_and_event_references(tmp_path: Path) -> None:
    """Trace records are independent from Runtime Events but share their IDs."""
    store = JsonlTraceStore(tmp_path / "traces")
    service = TraceService(store)
    tracer = create_tracer(service)
    events = EventBus()

    with tracer.trace("execution-1", conversation_id="conversation-1") as trace:
        root_event = events.emit("execution-1", "execution.started")
        with tracer.span("model.complete", kind=SpanKind.MODEL) as span:
            nested_event = events.emit("execution-1", "model.requested")
            span.event("model.completed", {"request_id": "request-1"})
        trace.span_event("execution.observed")

    records = service.list("execution-1")
    service.close()

    assert root_event.trace_id == trace.trace_id
    assert root_event.span_id is None
    assert nested_event.trace_id == trace.trace_id
    assert nested_event.span_id == span.span_id
    assert [record.record_type for record in records] == [
        TraceRecordType.TRACE_STARTED,
        TraceRecordType.SPAN_STARTED,
        TraceRecordType.SPAN_EVENT,
        TraceRecordType.SPAN_ENDED,
        TraceRecordType.SPAN_EVENT,
        TraceRecordType.TRACE_ENDED,
    ]
    assert [record.sequence for record in records] == list(range(1, 7))
    assert records[-1].status.value == "succeeded"


def test_tracer_records_unhandled_exception_as_terminal_error(tmp_path: Path) -> None:
    """An unhandled exception closes the Trace with a unified system error."""
    store = JsonlTraceStore(tmp_path / "traces")
    service = TraceService(store)
    tracer = create_tracer(service)

    with pytest.raises(RuntimeError, match="boom"), tracer.trace("execution-2"):
        raise RuntimeError("boom")

    records = service.list("execution-2")
    service.close()

    ended = records[-1]
    assert ended.record_type is TraceRecordType.TRACE_ENDED
    assert ended.status.value == "failed"
    assert ended.error is not None
    assert ended.error.code == "system.unexpected_error"
