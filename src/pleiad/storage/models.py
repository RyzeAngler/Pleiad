"""Persisted execution and artifact models."""

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import Field

from pleiad.contracts import ContractModel, JsonObject
from pleiad.errors import ErrorInfo
from pleiad.graph.contracts import GraphStatus, NodeStatus


class ExecutionStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ExecutionRecord(ContractModel):
    """Durable summary of one user objective execution."""

    execution_id: str = Field(min_length=1)
    conversation_id: str = Field(min_length=1)
    objective: str = Field(min_length=1)
    status: ExecutionStatus
    graph_id: str | None = None
    graph_revision: int | None = None
    output: JsonObject | None = None
    error_code: str | None = None
    error: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ArtifactRef(ContractModel):
    """Content-addressed reference passed between Nodes."""

    artifact_id: str
    media_type: str
    digest: str
    location: str


class GraphRunRecord(ContractModel):
    """Durable runtime state for one Graph revision execution."""

    graph_run_id: str = Field(min_length=1)
    execution_id: str = Field(min_length=1)
    graph_id: str = Field(min_length=1)
    graph_revision: int = Field(ge=1)
    status: GraphStatus
    outputs: JsonObject = Field(default_factory=dict)
    error: ErrorInfo | None = None
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None


class NodeRunRecord(ContractModel):
    """Durable runtime state for one temporary Node Agent run."""

    node_run_id: str = Field(min_length=1)
    execution_id: str = Field(min_length=1)
    graph_id: str = Field(min_length=1)
    graph_revision: int = Field(ge=1)
    node_id: str = Field(min_length=1)
    agent_run_id: str | None = None
    status: NodeStatus
    artifact_id: str | None = None
    error: ErrorInfo | None = None
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None
