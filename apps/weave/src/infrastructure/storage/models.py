"""Persisted execution and artifact models."""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from pleiad.contracts import ContractModel, JsonObject
from pleiad.orchestration.budget import BudgetState
from pleiad.orchestration.graph.state import GraphState, GraphStatus, NodeStatus
from pleiad.runtime.lifecycle.state import RunState, RunStatus, RunUsage
from pydantic import Field, model_validator

from pleiad_weave.application.errors import ErrorRecord


class ExecutionStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    RESUME_QUEUED = "resume_queued"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ExecutionRecord(ContractModel):
    """Durable summary of one user objective execution."""

    execution_id: str = Field(min_length=1)
    parent_execution_id: str | None = Field(default=None, min_length=1)
    revision: int = Field(default=0, ge=0)
    conversation_id: str = Field(min_length=1)
    objective: str = Field(min_length=1)
    input: JsonObject = Field(default_factory=dict)
    status: ExecutionStatus
    graph_id: str | None = None
    graph_revision: int | None = None
    output: JsonObject | None = None
    error: ErrorRecord | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ExecutionEventRecord(ContractModel):
    """One append-only event in an Execution's ordered journal."""

    event_id: str = Field(min_length=1)
    execution_id: str = Field(min_length=1)
    sequence: int = Field(ge=1)
    event_type: str = Field(min_length=1)
    data: JsonObject = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    trace_id: str | None = None
    span_id: str | None = None


class ApprovalDecision(ContractModel):
    """Record one owner's accepted decision for an immutable pending Tool call."""

    interruption_id: str = Field(min_length=1)
    decision: Literal["approve", "reject"]
    decided_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ExecutionResumeState(ContractModel):
    """Keep SDK recovery data subordinate to its one durable Execution."""

    checkpoint: RunState
    approval_tool_codes: frozenset[str]
    graph_budget: BudgetState | None
    decisions: tuple[ApprovalDecision, ...] = ()

    @model_validator(mode="after")
    def validate_approval_state(self) -> "ExecutionResumeState":
        """Reject incomplete or incompatible saved approval contracts."""
        if not self.checkpoint.interruptions or any(
            item.tool_code not in self.approval_tool_codes or item.details.get("kind") != "weave.approval"
            for item in self.checkpoint.interruptions
        ):
            raise ValueError("Recovery requires pending actions covered by the saved approval policy")
        ids = tuple(item.interruption_id for item in self.decisions)
        if len(ids) != len(set(ids)):
            raise ValueError("An approval action can have only one accepted decision")
        return self


class ExecutionEventInput(ContractModel):
    """Event contents appended atomically with an Execution journal write."""

    event_type: str = Field(min_length=1)
    data: JsonObject = Field(default_factory=dict)
    event_id: str | None = Field(default=None, min_length=1)
    timestamp: datetime | None = None
    trace_id: str | None = None
    span_id: str | None = None


class ArtifactRef(ContractModel):
    """Content-addressed reference passed between Nodes."""

    artifact_id: str
    media_type: str
    digest: str
    location: str


class GraphRunRecord(ContractModel):
    """Durable outcome and SDK scheduling state of one Graph invocation.

    State remains absent until the SDK returns a checkpoint. Historical records
    and interrupted invocations may have no checkpoint; absence is not pending.
    """

    graph_run_id: str = Field(min_length=1)
    execution_id: str = Field(min_length=1)
    graph_id: str = Field(min_length=1)
    graph_revision: int = Field(ge=1)
    status: GraphStatus
    state: GraphState | None = None
    outputs: JsonObject = Field(default_factory=dict)
    error: ErrorRecord | None = None
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None


class NodeRunRecord(ContractModel):
    """Durable attempt belonging to one exact Graph invocation.

    Historical records without a Graph invocation identity remain unassociated;
    callers must not infer their ownership from a shared definition revision.
    """

    node_run_id: str = Field(min_length=1)
    graph_run_id: str | None = Field(default=None, min_length=1)
    execution_id: str = Field(min_length=1)
    graph_id: str = Field(min_length=1)
    graph_revision: int = Field(ge=1)
    node_id: str = Field(min_length=1)
    input: JsonObject | None = None
    model_name: str | None = Field(default=None, min_length=1)
    sdk_run_id: str | None = Field(default=None, min_length=1)
    sdk_run_status: RunStatus | None = None
    usage: RunUsage | None = None
    status: NodeStatus
    artifact_id: str | None = None
    error: ErrorRecord | None = None
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None
