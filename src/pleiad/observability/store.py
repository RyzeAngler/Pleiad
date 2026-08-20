"""Storage protocol for local and future remote TraceStore implementations."""

from typing import Protocol

from pleiad.observability.models import TraceRecord


class TraceStore(Protocol):
    """Append and query trace records without exposing a concrete backend."""

    def append(self, record: TraceRecord) -> None:
        """Persist one immutable record."""

    def list(self, execution_id: str) -> tuple[TraceRecord, ...]:
        """Return records for one execution in durable sequence order."""

    def close(self) -> None:
        """Release backend resources."""
