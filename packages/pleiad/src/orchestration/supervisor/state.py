from __future__ import annotations

from pydantic import Field

from pleiad.contracts import ContractModel, JsonObject
from pleiad.events.supervisor import DelegationStatus, WorkerLifecycleEvent


class DelegationRecord(ContractModel):
    """Serializable delegation facts without live Tasks or Runner objects."""

    delegation_id: str = Field(min_length=1)
    worker_id: str = Field(min_length=1)
    task: str = Field(min_length=1)
    status: DelegationStatus
    parent_run_id: str | None = Field(default=None, min_length=1)
    child_run_id: str | None = Field(default=None, min_length=1)
    result: JsonObject | None = None
    error_code: str | None = Field(default=None, min_length=1)
    error_message: str | None = Field(default=None, min_length=1)


class SupervisorSnapshot(ContractModel):
    """Serializable coordination state for host-controlled recovery."""

    delegations: tuple[DelegationRecord, ...] = ()
    events: tuple[WorkerLifecycleEvent, ...] = ()
    delivered_event_ids: tuple[str, ...] = ()


SupervisorSnapshot.model_rebuild()
