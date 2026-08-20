"""Run one NodeSpec as a temporary Agent."""

from datetime import UTC, datetime
from uuid import uuid4

from jsonschema import Draft202012Validator

from pleiad.agent.factory import AgentFactory
from pleiad.agent.loop import AgentLoop
from pleiad.errors import ErrorCode, PleiadError, make_error
from pleiad.graph.contracts import NodeResult, NodeSpec, NodeStatus
from pleiad.observability import ObservabilityPort, SpanKind, observed
from pleiad.runtime.events import EventBus
from pleiad.storage.artifacts import ArtifactStore
from pleiad.storage.models import NodeRunRecord
from pleiad.storage.repository import NodeRunRepository


class NodeRunner:
    """Build, execute, validate, and persist one dynamic Node output."""

    def __init__(
        self,
        factory: AgentFactory,
        loop: AgentLoop,
        artifacts: ArtifactStore,
        node_runs: NodeRunRepository | None = None,
        observability: ObservabilityPort | None = None,
    ) -> None:
        self.factory = factory
        self.loop = loop
        self.artifacts = artifacts
        self.node_runs = node_runs
        self.observability = observability

    @observed(
        name="node.execute",
        kind=SpanKind.NODE,
        attributes=lambda args, kwargs: {
            "node_id": getattr(args[1], "node_id", "unknown"),
            "graph_id": kwargs.get("graph_id", "unknown"),
        },
    )
    async def run(
        self,
        node: NodeSpec,
        input_value: dict[str, object],
        *,
        execution_id: str,
        graph_id: str,
        graph_revision: int,
        events: EventBus | None = None,
    ) -> NodeResult:
        """Execute one Node with an automatically managed Node Span."""
        return await self._run(
            node,
            input_value,
            execution_id=execution_id,
            graph_id=graph_id,
            graph_revision=graph_revision,
            events=events,
        )

    async def _run(
        self,
        node: NodeSpec,
        input_value: dict[str, object],
        *,
        execution_id: str,
        graph_id: str,
        graph_revision: int,
        events: EventBus | None = None,
    ) -> NodeResult:
        """Execute one node and turn its structured output into an Artifact."""
        node_run_id = uuid4().hex
        started_at = datetime.now(UTC)
        if self.node_runs:
            self.node_runs.save(
                NodeRunRecord(
                    node_run_id=node_run_id,
                    execution_id=execution_id,
                    graph_id=graph_id,
                    graph_revision=graph_revision,
                    node_id=node.node_id,
                    status=NodeStatus.RUNNING,
                    started_at=started_at,
                )
            )
        events and events.emit(execution_id, "node.started", {"node_id": node.node_id})
        agent_run_id: str | None = None
        try:
            Draft202012Validator(node.input_schema).validate(input_value)
            definition = self.factory.for_node(
                node,
                agent_id=f"node-agent:{node.node_id}:{node_run_id}",
            )
            result = await self.loop.run(
                definition,
                self.factory.prompts.user_prompt(
                    node.objective,
                    input_value,
                ),
                execution_id=execution_id,
                node_run_id=node_run_id,
            )
            agent_run_id = result.run_id
            if result.status.value != "succeeded":
                node_result = NodeResult(
                    node_id=node.node_id,
                    node_run_id=node_run_id,
                    agent_run_id=agent_run_id,
                    status=NodeStatus.FAILED,
                    error=result.error
                    or make_error(
                        ErrorCode.SYSTEM_UNEXPECTED,
                        details={"reason": "Agent Node failed"},
                    ),
                )
            elif not isinstance(result.structured_output, dict):
                node_result = NodeResult(
                    node_id=node.node_id,
                    node_run_id=node_run_id,
                    agent_run_id=agent_run_id,
                    status=NodeStatus.FAILED,
                    error=make_error(
                        ErrorCode.AGENT_OUTPUT,
                        details={"reason": "Node output must be a JSON object"},
                    ),
                )
            else:
                artifact = self.artifacts.put_json(result.structured_output)
                node_result = NodeResult(
                    node_id=node.node_id,
                    node_run_id=node_run_id,
                    agent_run_id=agent_run_id,
                    status=NodeStatus.SUCCEEDED,
                    output=result.structured_output,
                    artifact_id=artifact.artifact_id,
                )
        except PleiadError as exc:
            node_result = NodeResult(
                node_id=node.node_id,
                node_run_id=node_run_id,
                agent_run_id=agent_run_id,
                status=NodeStatus.FAILED,
                error=exc.info,
            )
        except Exception as exc:
            node_result = NodeResult(
                node_id=node.node_id,
                node_run_id=node_run_id,
                status=NodeStatus.FAILED,
                error=make_error(
                    ErrorCode.SYSTEM_UNEXPECTED,
                    details={"exception": exc.__class__.__name__},
                ),
            )
        if self.node_runs:
            self.node_runs.save(
                NodeRunRecord(
                    node_run_id=node_run_id,
                    execution_id=execution_id,
                    graph_id=graph_id,
                    graph_revision=graph_revision,
                    node_id=node.node_id,
                    agent_run_id=agent_run_id,
                    status=node_result.status,
                    artifact_id=node_result.artifact_id,
                    error=node_result.error,
                    started_at=started_at,
                    finished_at=datetime.now(UTC),
                )
            )
        events and events.emit(
            execution_id,
            "node.succeeded" if node_result.status is NodeStatus.SUCCEEDED else "node.failed",
            {
                "node_id": node.node_id,
                "status": node_result.status.value,
                "error": node_result.error.as_dict() if node_result.error else None,
            },
        )
        return node_result
