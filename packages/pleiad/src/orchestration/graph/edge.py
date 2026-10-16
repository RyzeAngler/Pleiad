from dataclasses import dataclass
from typing import Literal

from pleiad.contracts import JsonValue
from pleiad.orchestration.graph.mapping import get_pointer


@dataclass(frozen=True, slots=True)
class EdgeMapping:
    """Map one JSON Pointer from a source output into a target input."""

    source_path: str = ""
    target_path: str = ""


@dataclass(frozen=True, slots=True)
class EdgeCondition:
    """Explicit data condition for activating one dependency edge."""

    source_path: str
    operator: Literal["equals", "not_equals", "exists", "truthy", "falsy"] = "truthy"
    value: JsonValue | None = None

    def matches(self, output: JsonValue) -> bool:
        """Evaluate this condition without executing arbitrary code."""
        try:
            selected = get_pointer(output, self.source_path)
        except (KeyError, IndexError, ValueError, TypeError):
            selected = None
            exists = False
        else:
            exists = True
        if self.operator == "exists":
            return exists
        if self.operator == "equals":
            return exists and selected == self.value
        if self.operator == "not_equals":
            return not exists or selected != self.value
        if self.operator == "falsy":
            return not exists or not bool(selected)
        return exists and bool(selected)


@dataclass(frozen=True, slots=True)
class Edge:
    """One dependency/data-flow relation in a DAG."""

    edge_id: str
    source_node_id: str
    target_node_id: str
    mappings: tuple[EdgeMapping, ...] = ()
    condition: EdgeCondition | None = None

    def __post_init__(self) -> None:
        """Reject blank edge identities and self loops."""
        if not self.edge_id.strip() or not self.source_node_id.strip() or not self.target_node_id.strip():
            raise ValueError("Edge identifiers must not be blank")
        if self.source_node_id == self.target_node_id:
            raise ValueError("self-loop is not allowed")
