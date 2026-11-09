from typing import Protocol

from pleiad.contracts import JsonObject
from pleiad.orchestration.graph.node import Node
from pleiad.orchestration.graph.state import NodeResult
from pleiad.runtime.checkpoint import ResumeDecision
from pleiad.sessions.checkpoint import RunCheckpoint


class NodeExecutor(Protocol):
    """Application-provided execution boundary for one Graph Node."""

    async def execute(
        self,
        node: Node,
        input_value: JsonObject,
        *,
        checkpoint: RunCheckpoint | None = None,
        decisions: tuple[ResumeDecision, ...] = (),
    ) -> NodeResult:
        """Return a confirmed Node result or raise a system failure."""
