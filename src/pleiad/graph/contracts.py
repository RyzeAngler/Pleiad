"""Deterministic task graph definitions and runtime result contracts."""

from enum import StrEnum
from typing import Self

from pydantic import Field, model_validator

from pleiad.config import ModelRole
from pleiad.contracts import ContractModel, JsonObject, JsonSchema
from pleiad.errors import ErrorInfo


class GraphStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class NodeStatus(StrEnum):
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class EdgeMapping(ContractModel):
    """Map a JSON Pointer from one Node output into another Node input."""

    source_path: str = ""
    target_path: str = ""


class OutputBinding(ContractModel):
    """Expose one Node result as a graph output."""

    source_node_id: str = Field(min_length=1)
    source_path: str = ""
    target_path: str = ""


class NodeSpec(ContractModel):
    """A dynamically generated Agent Node definition."""

    node_id: str = Field(min_length=1, max_length=128)
    objective: str = Field(min_length=1)
    prompt: str = ""
    model_role: ModelRole = ModelRole.TEXT
    tool_codes: tuple[str, ...] = ()
    skill_codes: tuple[str, ...] = ()
    input_schema: JsonSchema = Field(default_factory=lambda: {"type": "object"})
    output_schema: JsonSchema = Field(default_factory=lambda: {"type": "object"})


class NodeState(ContractModel):
    """Current runtime state of a NodeSpec, separate from its definition."""

    node_id: str
    status: NodeStatus = NodeStatus.PENDING
    attempt: int = Field(default=0, ge=0)
    output_artifact_id: str | None = None
    error: ErrorInfo | None = None


class EdgeSpec(ContractModel):
    """A dependency and data-flow relation without execution logic."""

    edge_id: str = Field(min_length=1, max_length=128)
    source_node_id: str = Field(min_length=1)
    target_node_id: str = Field(min_length=1)
    mappings: tuple[EdgeMapping, ...] = ()


class GraphSpec(ContractModel):
    """One immutable, validated revision proposed by PleiadAgent."""

    graph_id: str = Field(min_length=1, max_length=128)
    revision: int = Field(default=1, ge=1)
    objective: str = Field(min_length=1)
    input_schema: JsonSchema = Field(default_factory=lambda: {"type": "object"})
    output_schema: JsonSchema = Field(default_factory=lambda: {"type": "object"})
    nodes: tuple[NodeSpec, ...] = Field(min_length=1)
    edges: tuple[EdgeSpec, ...] = ()
    outputs: tuple[OutputBinding, ...] = ()
    max_concurrency: int = Field(default=8, ge=1, le=256)
    timeout_seconds: float = Field(default=3_600.0, gt=0.0, le=86_400.0)

    @model_validator(mode="after")
    def validate_references(self) -> Self:
        """Reject duplicate IDs, dangling edges, and self loops."""
        node_ids = [node.node_id for node in self.nodes]
        if len(node_ids) != len(set(node_ids)):
            raise ValueError("graph node_id values must be unique")
        edge_ids = [edge.edge_id for edge in self.edges]
        if len(edge_ids) != len(set(edge_ids)):
            raise ValueError("graph edge_id values must be unique")
        known = set(node_ids)
        for edge in self.edges:
            if edge.source_node_id not in known or edge.target_node_id not in known:
                raise ValueError(f"edge references an unknown node: {edge.edge_id}")
            if edge.source_node_id == edge.target_node_id:
                raise ValueError(f"self-loop is not allowed: {edge.edge_id}")
        for output in self.outputs:
            if output.source_node_id not in known:
                raise ValueError(f"output references an unknown node: {output.source_node_id}")
        return self


class GraphDiagnostic(ContractModel):
    """A deterministic graph validation finding."""

    code: str
    message: str
    path: str = ""


class GraphValidation(ContractModel):
    """Validation result returned before a revision can execute."""

    valid: bool
    diagnostics: tuple[GraphDiagnostic, ...] = ()


class NodeResult(ContractModel):
    """Structured output of one completed Node."""

    node_id: str
    node_run_id: str
    agent_run_id: str | None = None
    status: NodeStatus
    output: JsonObject | None = None
    error: ErrorInfo | None = None
    artifact_id: str | None = None


class GraphResult(ContractModel):
    """Structured result of one TaskGraph execution."""

    graph_id: str
    revision: int
    status: GraphStatus
    outputs: JsonObject = Field(default_factory=dict)
    node_results: tuple[NodeResult, ...] = ()
    error: ErrorInfo | None = None
