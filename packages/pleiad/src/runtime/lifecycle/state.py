from datetime import datetime
from enum import StrEnum
from typing import Literal, Self

from pydantic import Field, model_validator

from pleiad.context.compaction.models import CompactionState
from pleiad.contracts import CapabilityGrants, ContractModel
from pleiad.tools.contracts import ToolResult
from pleiad.tools.interruption import ToolInterruption


class RunStatus(StrEnum):
    """Public terminal statuses for one non-durable SDK invocation."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    TIMED_OUT = "timed_out"
    LIMIT_REACHED = "limit_reached"


class RunUsage(ContractModel):
    """Measured counts and separate conservative output charges for one Run."""

    input_tokens: int | None = Field(default=0, ge=0)
    output_tokens: int | None = Field(default=0, ge=0)
    total_tokens: int | None = Field(default=0, ge=0)
    budget_output_tokens: int = Field(default=0, ge=0)
    model_calls: int = Field(default=0, ge=0)
    tool_calls: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_total(self) -> Self:
        """Validate known measurements and their separate budget charge."""
        if (
            self.total_tokens is not None
            and self.input_tokens is not None
            and self.output_tokens is not None
            and self.total_tokens != self.input_tokens + self.output_tokens
        ):
            raise ValueError("total_tokens must equal input_tokens plus output_tokens")
        if self.output_tokens is not None and self.budget_output_tokens < self.output_tokens:
            raise ValueError("budget_output_tokens must cover reported output tokens")
        if self.output_tokens is None and self.model_calls > 0 and self.budget_output_tokens == 0:
            raise ValueError("unknown output usage requires a positive budget charge")
        return self


class RunState(ContractModel):
    """Serializable checkpoint containing only state needed for Run recovery."""

    version: Literal[2] = 2
    run_id: str = Field(min_length=1, max_length=256)
    root_agent: str = Field(min_length=1, max_length=256)
    active_agent: str = Field(min_length=1, max_length=256)
    agent_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    memory_id: str = Field(min_length=1, max_length=256)
    turn_id: str = Field(min_length=1, max_length=256)
    deadline: datetime
    max_turns: int = Field(gt=0)
    max_output_tokens: int = Field(gt=0)
    tool_concurrency: int = Field(gt=0)
    max_handoffs: int = Field(gt=0)
    grants: CapabilityGrants
    usage: RunUsage
    parent_run_id: str | None = Field(default=None, min_length=1, max_length=256)
    handoff_count: int = Field(default=0, ge=0)
    interruptions: tuple[ToolInterruption, ...] = ()
    tool_results: tuple[ToolResult, ...] = ()
    compaction: CompactionState | None = None

    @model_validator(mode="after")
    def validate_checkpoint(self) -> Self:
        """Reject duplicate interruption and Tool result identities."""
        interruption_ids = tuple(item.interruption_id for item in self.interruptions)
        call_ids = tuple(item.call_id for item in self.interruptions)
        result_ids = tuple(item.call_id for item in self.tool_results)
        if len(interruption_ids) != len(set(interruption_ids)):
            raise ValueError("RunState interruption IDs must be unique")
        if len(call_ids) != len(set(call_ids)):
            raise ValueError("RunState interruption call IDs must be unique")
        if len(result_ids) != len(set(result_ids)):
            raise ValueError("RunState Tool result call IDs must be unique")
        if set(call_ids) & set(result_ids):
            raise ValueError("RunState calls cannot be both completed and interrupted")
        return self

    def to_json(self) -> str:
        """Serialize this versioned checkpoint."""
        return self.model_dump_json()

    @classmethod
    def from_json(cls, value: str) -> Self:
        """Validate and restore a serialized checkpoint."""
        return cls.model_validate_json(value)
