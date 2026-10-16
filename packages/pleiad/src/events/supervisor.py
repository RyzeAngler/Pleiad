from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field

from pleiad.contracts import JsonObject
from pleiad.events.contracts import Event


class DelegationStatus(StrEnum):
    """Lifecycle states owned by one Supervisor delegation."""

    ACCEPTED = "accepted"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    CANCELLED = "cancelled"


class WorkerEvent(Event):
    """Correlate one Worker lifecycle fact with its parent and child Runs."""

    delegation_id: str = Field(min_length=1)
    sequence: int = Field(ge=1)
    parent_run_id: str | None = Field(default=None, min_length=1)
    child_run_id: str | None = Field(default=None, min_length=1)
    worker_id: str = Field(min_length=1)


class WorkerSubmittedEvent(WorkerEvent):
    """Report acceptance of one Worker delegation."""

    kind: Literal["worker.submitted"] = "worker.submitted"


class WorkerStartedEvent(WorkerEvent):
    """Report that one accepted Worker has begun execution."""

    kind: Literal["worker.started"] = "worker.started"


class WorkerCompletedEvent(WorkerEvent):
    """Carry one persisted terminal Worker result."""

    kind: Literal["worker.completed"] = "worker.completed"
    status: DelegationStatus
    result: JsonObject | None = None
    error_code: str | None = Field(default=None, min_length=1)
    error_message: str | None = Field(default=None, min_length=1)


class WorkerConsumedEvent(WorkerEvent):
    """Report consumption of one completed Worker result."""

    kind: Literal["worker.consumed"] = "worker.consumed"
    completion_event_id: str = Field(min_length=1)


WorkerLifecycleEvent = Annotated[
    WorkerSubmittedEvent | WorkerStartedEvent | WorkerCompletedEvent | WorkerConsumedEvent,
    Field(discriminator="kind"),
]
