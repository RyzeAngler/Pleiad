from typing import Literal

from pleiad.contracts import ContractModel, JsonSchema
from pleiad.orchestration.graph.edge import Edge
from pleiad.orchestration.graph.graph import OutputBinding
from pydantic import Field


def object_schema() -> JsonSchema:
    """Return a fresh object schema for unspecified graph contracts."""
    return {"type": "object"}


class NodeSpec(ContractModel):
    """Persist the product instructions used to bind one SDK Agent Node."""

    node_id: str = Field(min_length=1)
    objective: str = Field(min_length=1)
    prompt: str = ""
    model_role: Literal["text", "vision", "speech"] = "text"
    tool_codes: tuple[str, ...] = ()
    skill_codes: tuple[str, ...] = ()
    input_schema: JsonSchema = Field(default_factory=object_schema)
    output_schema: JsonSchema = Field(default_factory=object_schema)


class GraphSpec(ContractModel):
    """Validate and serialize a graph proposal before binding live SDK objects."""

    graph_id: str = Field(min_length=1)
    revision: int = Field(default=1, ge=1)
    objective: str = ""
    input_schema: JsonSchema = Field(default_factory=object_schema)
    output_schema: JsonSchema = Field(default_factory=object_schema)
    nodes: tuple[NodeSpec, ...] = Field(min_length=1)
    edges: tuple[Edge, ...] = ()
    outputs: tuple[OutputBinding, ...] = ()
    max_concurrency: int = Field(default=8, ge=1)
    timeout_seconds: float = Field(default=3600.0, gt=0, allow_inf_nan=False)
