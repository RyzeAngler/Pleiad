from enum import StrEnum
from typing import Literal, Self

from pydantic import Field, model_validator

from pleiad.events.contracts import Event


class GraphEventKind(StrEnum):
    """Stable event names emitted by the Graph executor."""

    GRAPH_STARTED = "graph.started"
    GRAPH_COMPLETED = "graph.completed"
    GRAPH_FAILED = "graph.failed"
    GRAPH_PAUSED = "graph.paused"
    GRAPH_CANCELLED = "graph.cancelled"
    JOIN_READY = "graph.join.ready"
    NODE_STARTED = "graph.node.started"
    NODE_COMPLETED = "graph.node.completed"
    NODE_SKIPPED = "graph.node.skipped"


class GraphEvent(Event):
    """One safe Graph lifecycle event."""

    kind: GraphEventKind
    graph_id: str = Field(min_length=1)
    revision: int = Field(ge=1)
    node_id: str | None = Field(default=None, min_length=1)
    node_run_id: str | None = Field(default=None, min_length=1)
    status: Literal["running", "succeeded", "failed", "cancelled", "paused"] | None = None

    @model_validator(mode="after")
    def validate_payload(self) -> Self:
        """Require node identity only for node-scoped event kinds."""
        node_kinds = {
            GraphEventKind.JOIN_READY,
            GraphEventKind.NODE_STARTED,
            GraphEventKind.NODE_COMPLETED,
            GraphEventKind.NODE_SKIPPED,
        }
        if self.kind in node_kinds and self.node_id is None:
            raise ValueError("node events require node_id")
        if self.kind not in node_kinds and self.node_id is not None:
            raise ValueError("Graph lifecycle events cannot contain node_id")
        return self
