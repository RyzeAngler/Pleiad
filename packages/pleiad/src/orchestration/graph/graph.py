import hashlib
import json
from dataclasses import asdict, dataclass, field
from math import isfinite

from pleiad.contracts import JsonSchema
from pleiad.orchestration.graph.edge import Edge
from pleiad.orchestration.graph.node import Node
from pleiad.schemas import validate_json_schema


@dataclass(frozen=True, slots=True)
class OutputBinding:
    """Expose one node output as a Graph output field."""

    source_node_id: str
    source_path: str = ""
    target_path: str = ""


@dataclass(frozen=True, slots=True)
class Graph:
    """Immutable, host-bound DAG definition with no product persistence assumptions."""

    graph_id: str
    nodes: tuple[Node, ...]
    edges: tuple[Edge, ...] = ()
    objective: str = ""
    revision: int = 1
    input_schema: JsonSchema = field(default_factory=lambda: {"type": "object"})
    output_schema: JsonSchema = field(default_factory=lambda: {"type": "object"})
    outputs: tuple[OutputBinding, ...] = ()
    max_concurrency: int = 8
    timeout_seconds: float = 3_600.0

    def __post_init__(self) -> None:
        """Validate local graph metadata before full reference validation."""
        if not self.graph_id.strip() or not self.nodes:
            raise ValueError("Graph requires a non-empty ID and at least one Node")
        if (
            self.revision < 1
            or self.max_concurrency < 1
            or not isfinite(self.timeout_seconds)
            or self.timeout_seconds <= 0
        ):
            raise ValueError("Graph revision and concurrency must be positive; timeout must be finite and positive")
        validate_json_schema(self.input_schema)
        validate_json_schema(self.output_schema)

    def fingerprint(self) -> str:
        """Return a stable definition fingerprint that excludes live callbacks."""
        payload = {
            "graph_id": self.graph_id,
            "revision": self.revision,
            "input_schema": self.input_schema,
            "output_schema": self.output_schema,
            "max_concurrency": self.max_concurrency,
            "timeout_seconds": self.timeout_seconds,
            "nodes": [
                {
                    "node_id": node.node_id,
                    "input_schema": node.input_schema,
                    "output_schema": node.output_schema,
                    "agent": node.agent.name if node.agent is not None else None,
                    "side_effect": node.side_effect,
                }
                for node in self.nodes
            ],
            "edges": [
                {
                    "edge_id": edge.edge_id,
                    "source": edge.source_node_id,
                    "target": edge.target_node_id,
                    "mappings": [asdict(mapping) for mapping in edge.mappings],
                    "condition": asdict(edge.condition) if edge.condition is not None else None,
                }
                for edge in self.edges
            ],
            "outputs": [asdict(binding) for binding in self.outputs],
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
