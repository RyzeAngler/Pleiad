"""Immutable trace records written by the local observability pipeline."""

import json
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from pleiad.errors import ErrorInfo


class TraceStatus(StrEnum):
    """Terminal and active states shared by traces and spans."""

    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"


class SpanStatus(StrEnum):
    """Span status vocabulary kept separate for a stable public contract."""

    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"


class SpanKind(StrEnum):
    """Runtime boundary represented by one span."""

    AGENT = "agent"
    GRAPH = "graph"
    NODE = "node"
    LOOP = "loop"
    MODEL = "model"
    TOOL = "tool"
    RETRIEVAL = "retrieval"
    COMPACTION = "compaction"
    EVALUATOR = "evaluator"
    GUARDRAIL = "guardrail"
    STORAGE = "storage"
    SYSTEM = "system"


class TraceRecordType(StrEnum):
    """Append-only record types in a local TraceStore."""

    TRACE_STARTED = "trace.started"
    TRACE_ENDED = "trace.ended"
    SPAN_STARTED = "span.started"
    SPAN_ENDED = "span.ended"
    SPAN_EVENT = "span.event"
    LINK_CREATED = "link.created"


@dataclass(frozen=True, slots=True)
class TraceLink:
    """Explicit relationship between spans or traces outside one parent tree."""

    source_trace_id: str
    source_span_id: str | None
    target_trace_id: str
    target_span_id: str | None
    relation: str
    correlation_id: str | None = None


@dataclass(frozen=True, slots=True)
class TraceRecord:
    """One flattened JSONL record containing enough data to rebuild a trace."""

    sequence: int
    record_type: TraceRecordType
    timestamp: datetime
    execution_id: str
    trace_id: str
    span_id: str | None = None
    parent_span_id: str | None = None
    kind: SpanKind = SpanKind.SYSTEM
    name: str = ""
    status: TraceStatus = TraceStatus.RUNNING
    attributes: dict[str, Any] = field(default_factory=dict)
    error: ErrorInfo | None = None
    source_trace_id: str | None = None
    source_span_id: str | None = None
    target_trace_id: str | None = None
    target_span_id: str | None = None
    relation: str | None = None
    correlation_id: str | None = None
    schema_version: int = 2

    def as_dict(self) -> dict[str, object]:
        """Return a JSON-compatible durable representation."""
        return {
            "schema_version": self.schema_version,
            "sequence": self.sequence,
            "record_type": self.record_type.value,
            "timestamp": self.timestamp.isoformat(),
            "execution_id": self.execution_id,
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
            "kind": self.kind.value,
            "name": self.name,
            "status": self.status.value,
            "attributes": _json_value(self.attributes),
            "error": _json_value(self.error.as_dict()) if self.error else None,
            "source_trace_id": self.source_trace_id,
            "source_span_id": self.source_span_id,
            "target_trace_id": self.target_trace_id,
            "target_span_id": self.target_span_id,
            "relation": self.relation,
            "correlation_id": self.correlation_id,
        }

    def as_json(self) -> str:
        """Encode this record as one Unicode JSON line."""
        return json.dumps(self.as_dict(), ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "TraceRecord":
        """Load one persisted record and validate its stable vocabulary."""
        raw_error = value.get("error")
        error = None
        if isinstance(raw_error, dict):
            from pleiad.errors import ErrorKind

            error = ErrorInfo(
                code=str(raw_error["code"]),
                message=str(raw_error["message"]),
                kind=ErrorKind(str(raw_error["kind"])),
                retryable=bool(raw_error.get("retryable", False)),
                terminal=bool(raw_error.get("terminal", False)),
                details=dict(raw_error.get("details", {})),
            )
        return cls(
            schema_version=int(value.get("schema_version", 1)),
            sequence=int(value["sequence"]),
            record_type=TraceRecordType(str(value["record_type"])),
            timestamp=datetime.fromisoformat(str(value["timestamp"])),
            execution_id=str(value["execution_id"]),
            trace_id=str(value["trace_id"]),
            span_id=_optional_string(value.get("span_id")),
            parent_span_id=_optional_string(value.get("parent_span_id")),
            kind=SpanKind(str(value.get("kind", SpanKind.SYSTEM.value))),
            name=str(value.get("name", "")),
            status=TraceStatus(str(value.get("status", TraceStatus.RUNNING.value))),
            attributes=dict(value.get("attributes", {})),
            error=error,
            source_trace_id=_optional_string(value.get("source_trace_id")),
            source_span_id=_optional_string(value.get("source_span_id")),
            target_trace_id=_optional_string(value.get("target_trace_id")),
            target_span_id=_optional_string(value.get("target_span_id")),
            relation=_optional_string(value.get("relation")),
            correlation_id=_optional_string(value.get("correlation_id")),
        )


def _optional_string(value: object) -> str | None:
    """Convert nullable persisted identifiers without accepting empty noise."""
    return str(value) if value is not None else None


def _json_value(value: object) -> object:
    """Convert common runtime values into JSON-compatible values."""
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_json_value(item) for item in value]
    return str(value)
