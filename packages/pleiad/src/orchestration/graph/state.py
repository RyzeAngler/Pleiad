from enum import StrEnum

from pydantic import Field, model_validator

from pleiad.contracts import ContractModel, JsonObject
from pleiad.errors.base import ErrorType


class GraphStatus(StrEnum):
    """Terminal and active states of one Graph run."""

    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    PAUSED = "paused"


class NodeStatus(StrEnum):
    """States of one Graph node independent from its definition."""

    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    UNKNOWN = "unknown"
    PAUSED = "paused"


class NodeResult(ContractModel):
    """One executable node's confirmed result."""

    node_id: str = Field(min_length=1)
    node_run_id: str = Field(min_length=1)
    status: NodeStatus
    output: JsonObject | None = None
    error_code: str | None = Field(default=None, min_length=1)
    error_message: str | None = Field(default=None, min_length=1)
    error_type: ErrorType | None = None

    @model_validator(mode="after")
    def validate_error(self) -> "NodeResult":
        """Keep node business errors complete and typed."""
        fields = (self.error_code, self.error_message, self.error_type)
        if any(value is not None for value in fields) and not all(value is not None for value in fields):
            raise ValueError("Node error fields must be provided together")
        if self.error_type is not None and self.error_type is not ErrorType.BUSINESS:
            raise ValueError("NodeResult only carries business errors")
        return self


class NodeState(ContractModel):
    """Graph recovery state for one node, without message history."""

    node_id: str = Field(min_length=1)
    status: NodeStatus = NodeStatus.PENDING
    attempt: int = Field(default=0, ge=0)
    node_run_id: str | None = Field(default=None, min_length=1)
    output: JsonObject | None = None
    error_code: str | None = Field(default=None, min_length=1)
    error_message: str | None = Field(default=None, min_length=1)
    error_type: ErrorType | None = None


class BranchDecision(ContractModel):
    """Confirmed activation decision for one conditional edge."""

    edge_id: str = Field(min_length=1)
    activated: bool


class GraphState(ContractModel):
    """Serializable Graph checkpoint for host-controlled recovery."""

    graph_id: str = Field(min_length=1)
    revision: int = Field(ge=1)
    graph_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    node_states: tuple[NodeState, ...]
    branch_decisions: tuple[BranchDecision, ...] = ()
    scheduling_position: int = Field(default=0, ge=0)


class GraphResult(ContractModel):
    """Public result of one Graph execution and its recoverable state."""

    graph_id: str
    revision: int
    status: GraphStatus
    state: GraphState
    outputs: JsonObject = Field(default_factory=dict)
    error_code: str | None = None
    error_message: str | None = None
    error_type: ErrorType | None = None
