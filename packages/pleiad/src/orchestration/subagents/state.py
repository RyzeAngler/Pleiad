from __future__ import annotations

from pydantic import Field, model_validator

from pleiad.budgets import BudgetCheckpoint
from pleiad.contracts import ContractModel, JsonObject
from pleiad.events.subagents import DelegationStatus, SubagentLifecycleEvent


class DelegationRecord(ContractModel):
    """Serializable delegation facts without live Tasks or Runner objects."""

    delegation_id: str = Field(min_length=1)
    subagent_id: str = Field(min_length=1)
    task: str = Field(min_length=1)
    status: DelegationStatus
    parent_run_id: str | None = Field(default=None, min_length=1)
    child_run_id: str | None = Field(default=None, min_length=1)
    result: JsonObject | None = None
    error_code: str | None = Field(default=None, min_length=1)
    error_message: str | None = Field(default=None, min_length=1)


class SubagentSnapshot(ContractModel):
    """Serializable coordination state for host-controlled recovery."""

    owner_run_id: str | None = Field(default=None, min_length=1)
    budget: BudgetCheckpoint | None = None
    delegations: tuple[DelegationRecord, ...] = ()
    events: tuple[SubagentLifecycleEvent, ...] = ()
    delivered_event_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_owner(self) -> SubagentSnapshot:
        """Reject delegation and reservation facts from another parent Run."""
        if any(item.parent_run_id != self.owner_run_id or item.parent_run_id is None for item in self.delegations):
            raise ValueError("Subagent snapshots must belong to one explicit parent Run")
        if self.budget is not None and self.budget.parent_run_id != self.owner_run_id:
            raise ValueError("Subagent budget and snapshot owners must match")
        return self


SubagentSnapshot.model_rebuild()
