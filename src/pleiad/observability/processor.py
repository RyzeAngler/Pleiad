"""Direct Trace processing between Runtime scopes and durable storage."""

from __future__ import annotations

from dataclasses import replace
from threading import RLock

from pleiad.observability.models import TraceRecord
from pleiad.observability.service import TraceService


class TraceProcessor:
    """Assign per-Trace sequence numbers and submit records to TraceService."""

    def __init__(self, service: TraceService) -> None:
        self.service = service
        self._sequences: dict[str, int] = {}
        self._lock = RLock()

    def record(self, record: TraceRecord) -> None:
        """Process one completed lifecycle record without exposing storage to Runtime."""
        with self._lock:
            sequence = self._sequences.get(record.trace_id, 0) + 1
            self._sequences[record.trace_id] = sequence
        self.service.record(replace(record, sequence=sequence))
