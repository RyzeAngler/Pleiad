"""Application service request models shared by API and CLI."""

from datetime import datetime
from typing import Literal

from pleiad.contracts import ContractModel, JsonObject
from pleiad.tools.interruption import ToolInterruption
from pydantic import Field

from pleiad_weave.application.events import ExecutionEvent
from pleiad_weave.infrastructure.storage.models import ExecutionRecord


class ExecuteRequest(ContractModel):
    """One user objective submitted to Pleiad."""

    objective: str = Field(min_length=1)
    input: JsonObject = Field(default_factory=dict)
    conversation_id: str = "default"


class ApprovalRequest(ContractModel):
    """Expose the exact persisted action awaiting the workspace owner's decision."""

    execution_id: str
    interruption: ToolInterruption
    deadline: datetime


class ApprovalDecisionRequest(ContractModel):
    """Accept a decision without accepting client-authored Tool results or identity."""

    decision: Literal["approve", "reject"]


class ExecutionResponse(ContractModel):
    """Stable API projection of an execution."""

    execution: ExecutionRecord


class HealthResponse(ContractModel):
    """Health endpoint response."""

    status: str = "ok"


class EventResponse(ContractModel):
    """JSON projection of one replayable Runtime event."""

    event_id: str
    execution_id: str
    event_type: str
    timestamp: str
    sequence: int = 0
    trace_id: str | None = None
    span_id: str | None = None
    data: JsonObject = Field(default_factory=dict)

    @classmethod
    def from_event(cls, event: ExecutionEvent) -> "EventResponse":
        """Convert the internal immutable event into an API payload."""
        return cls(
            event_id=event.event_id,
            execution_id=event.execution_id,
            event_type=event.event_type,
            timestamp=event.timestamp.isoformat(),
            sequence=event.sequence,
            trace_id=event.trace_id,
            span_id=event.span_id,
            data=dict(event.data),
        )
