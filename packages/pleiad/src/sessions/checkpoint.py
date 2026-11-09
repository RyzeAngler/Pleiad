from datetime import datetime
from typing import Literal, Self

from pydantic import Field, model_validator

from pleiad.budgets import BudgetCheckpoint
from pleiad.context.budget import ContextBudget
from pleiad.context.compaction.models import CompactionState
from pleiad.context.selection.models import ContextBlock
from pleiad.contracts import CapabilityGrants, ContractModel, JsonObject, UsageCharge
from pleiad.sessions.usage import RunUsage
from pleiad.tools.contracts import ToolResult
from pleiad.tools.interruption import ToolInterruption


class RunCheckpoint(ContractModel):
    """Persist a safe Run position and the original constraints for recovery."""

    version: Literal[1] = 1
    session_id: str = Field(min_length=1, max_length=256)
    run_id: str = Field(min_length=1, max_length=256)
    revision: int = Field(ge=1)
    writer_epoch: int = Field(ge=1)
    root_agent: str = Field(min_length=1, max_length=256)
    active_agent: str = Field(min_length=1, max_length=256)
    agent_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    step_id: str | None = Field(default=None, min_length=1, max_length=256)
    deadline: datetime
    execution_timeout_seconds: float = Field(gt=0, allow_inf_nan=False)
    absolute_deadline: datetime | None = None
    execution_seconds_used: float = Field(default=0.0, ge=0, allow_inf_nan=False)
    max_model_steps: int = Field(gt=0)
    max_output_tokens: int = Field(gt=0)
    tool_concurrency: int = Field(gt=0)
    max_handoffs: int = Field(gt=0)
    grants: CapabilityGrants
    usage: RunUsage
    usage_charges: tuple[UsageCharge, ...] = ()
    budget: BudgetCheckpoint | None = None
    tool_scope_states: dict[str, JsonObject] = Field(default_factory=dict)
    parent_run_id: str | None = Field(default=None, min_length=1, max_length=256)
    handoff_count: int = Field(default=0, ge=0)
    interruptions: tuple[ToolInterruption, ...] = ()
    tool_results: tuple[ToolResult, ...] = ()
    compaction: CompactionState | None = None
    context: tuple[ContextBlock, ...] = ()
    context_budget: ContextBudget

    @model_validator(mode="after")
    def validate_checkpoint(self) -> Self:
        """Reject duplicate pending actions and overlapping completed results."""
        identities = tuple(value.charge_id for value in self.usage_charges)
        if len(identities) != len(set(identities)) or any(
            value.parent_run_id != self.run_id for value in self.usage_charges
        ):
            raise ValueError("checkpoint child charges must uniquely belong to its Run")
        if self.usage.child_model_calls != sum(
            value.model_steps for value in self.usage_charges
        ) or self.usage.child_budget_output_tokens != sum(value.output_tokens for value in self.usage_charges):
            raise ValueError("checkpoint usage must retain its cumulative child charges")
        if self.budget is not None and self.budget.parent_run_id != self.run_id:
            raise ValueError("checkpoint budget must belong to its Run")
        interruptions = tuple(item.interruption_id for item in self.interruptions)
        calls = tuple(item.call_id for item in self.interruptions)
        results = tuple(item.call_id for item in self.tool_results)
        if len(interruptions) != len(set(interruptions)) or len(calls) != len(set(calls)):
            raise ValueError("checkpoint pending action identities must be unique")
        if len(results) != len(set(results)) or set(calls) & set(results):
            raise ValueError("checkpoint results must be unique and separate from pending actions")
        return self

    def to_json(self) -> str:
        """Serialize this checkpoint for host persistence."""
        return self.model_dump_json()

    @classmethod
    def from_json(cls, value: str) -> Self:
        """Validate a persisted checkpoint without old-schema coercion."""
        return cls.model_validate_json(value)
