import asyncio
from collections.abc import Mapping
from datetime import UTC, datetime
from uuid import uuid4

from pleiad.agent.agent import Agent
from pleiad.agent.errors import AgentConfigurationError
from pleiad.context.selection.models import ContextBlock
from pleiad.contracts import JsonObject, SkillRef, ToolRef
from pleiad.errors.base import PleiadError
from pleiad.events.contracts import Event
from pleiad.models.anthropic.client import AnthropicModelClient
from pleiad.models.openai.client import OpenAIModelClient
from pleiad.orchestration.budget import BudgetState
from pleiad.orchestration.graph.executor import GraphExecutor
from pleiad.orchestration.graph.graph import Graph
from pleiad.orchestration.graph.node import Node
from pleiad.orchestration.graph.state import GraphResult, GraphStatus
from pleiad.runtime.execution.runner import Runner
from pleiad.tools.contracts import DEFAULT_TOOL_CODES, ToolContext

from pleiad_weave.agent.contracts import GraphSpec
from pleiad_weave.agent.nodes import NodeExecutor
from pleiad_weave.agent.prompt import build_node_prompt
from pleiad_weave.application.errors import ErrorRecord
from pleiad_weave.application.events import ExecutionEventPublisher
from pleiad_weave.infrastructure.storage.artifacts import ArtifactStore
from pleiad_weave.infrastructure.storage.models import GraphRunRecord
from pleiad_weave.infrastructure.storage.repository import GraphRunRepository, NodeRunRepository


def build_graph(
    spec: GraphSpec,
    models: Mapping[str, OpenAIModelClient | AnthropicModelClient],
    runner: Runner,
    *,
    approval_tool_codes: frozenset[str] = frozenset(),
    context_blocks: tuple[ContextBlock, ...] = (),
) -> Graph:
    """Bind persisted product instructions to the SDK's executable Graph.

    Args:
        spec: Validated product graph proposal.
        models: Explicit host model-role bindings.
        runner: SDK Runner owning the Tool and Skill snapshot.
        approval_tool_codes: Tool codes that cannot run inside a Graph yet.
        context_blocks: Execution-local references shared by every Agent Node.

    Returns:
        An executable SDK Graph.

    Raises:
        LookupError: If a requested product capability or model is unavailable.
    """
    nodes: list[Node] = []
    for node in spec.nodes:
        if approval_tool_codes.intersection((*DEFAULT_TOOL_CODES, *node.tool_codes)):
            raise AgentConfigurationError("Graph nodes requiring approval are not supported by Graph recovery yet.")
        try:
            model = models[node.model_role]
        except KeyError as error:
            raise LookupError(f"Model role is not configured: {node.model_role}") from error
        for code in node.tool_codes:
            runner.tools.resolve(code)
        for code in node.skill_codes:
            if not runner.skills.contains(code):
                raise LookupError(f"Skill is not configured: {code}")
            runner.skills.resolve(code)
        agent = Agent(
            name=f"node:{node.node_id}",
            model=model,
            prompt=build_node_prompt(node),
            tools=tuple(ToolRef(code) for code in node.tool_codes),
            skills=tuple(SkillRef(code) for code in node.skill_codes),
            output_schema=node.output_schema,
        )
        nodes.append(
            Node(
                node_id=node.node_id,
                input_schema=node.input_schema,
                output_schema=node.output_schema,
                agent=agent,
                runner=runner,
                context_blocks=context_blocks,
            )
        )
    return Graph(
        graph_id=spec.graph_id,
        revision=spec.revision,
        objective=spec.objective,
        nodes=tuple(nodes),
        edges=spec.edges,
        outputs=spec.outputs,
        input_schema=spec.input_schema,
        output_schema=spec.output_schema,
        max_concurrency=spec.max_concurrency,
        timeout_seconds=spec.timeout_seconds,
    )


class GraphRunner:
    """Add product persistence around SDK Graph scheduling and event emission."""

    def __init__(
        self,
        executor: GraphExecutor,
        publisher: ExecutionEventPublisher,
        graphs: GraphRunRepository,
        nodes: NodeRunRepository,
        artifacts: ArtifactStore,
    ) -> None:
        """Bind product persistence separately from the SDK executor."""
        self.executor = executor
        self.publisher = publisher
        self.graphs = graphs
        self.nodes = nodes
        self.artifacts = artifacts

    def budget_state(self, parent_run_id: str) -> BudgetState | None:
        """Read the SDK's settled child pool for a product checkpoint."""
        return self.executor.budget_state(parent_run_id)

    def restore_budget(self, parent_run_id: str, state: BudgetState) -> None:
        """Restore SDK accounting without constructing a second product ledger."""
        self.executor.restore_budget(parent_run_id, state)

    def release_budget(self, parent_run_id: str) -> None:
        """Release SDK child accounting after the owning product Run settles."""
        self.executor.release_budget(parent_run_id)

    async def execute(
        self,
        graph: Graph,
        graph_input: JsonObject,
        *,
        execution_id: str,
        context: ToolContext | None = None,
    ) -> GraphResult:
        """Execute one bound Graph and save its independent product outcome."""
        record = GraphRunRecord(
            graph_run_id=uuid4().hex,
            execution_id=execution_id,
            graph_id=graph.graph_id,
            graph_revision=graph.revision,
            status=GraphStatus.RUNNING,
        )
        await asyncio.to_thread(self.graphs.save, record)

        async def publish(event: Event) -> None:
            """Correlate SDK Graph events with this Execution."""
            await self.publisher.publish_sdk_event(execution_id, event)

        try:
            result = await self.executor.execute(
                graph,
                graph_input,
                NodeExecutor(
                    execution_id,
                    graph.graph_id,
                    graph.revision,
                    self.nodes,
                    self.artifacts,
                    self.publisher,
                    graph_run_id=record.graph_run_id,
                ),
                event_sink=publish,
                context=context,
            )
        except asyncio.CancelledError:
            await asyncio.to_thread(
                self.graphs.save,
                record.model_copy(update={"status": GraphStatus.CANCELLED, "finished_at": datetime.now(UTC)}),
            )
            raise
        except PleiadError as failure:
            await asyncio.to_thread(
                self.graphs.save,
                record.model_copy(
                    update={
                        "status": GraphStatus.FAILED,
                        "error": ErrorRecord.from_exception(failure),
                        "finished_at": datetime.now(UTC),
                    }
                ),
            )
            raise
        error = None
        if result.error_code is not None and result.error_message is not None and result.error_type is not None:
            error = ErrorRecord(code=result.error_code, message=result.error_message, type=result.error_type)
        await asyncio.to_thread(
            self.graphs.save,
            record.model_copy(
                update={
                    "status": result.status,
                    "state": result.state,
                    "outputs": result.outputs,
                    "error": error,
                    "finished_at": datetime.now(UTC),
                }
            ),
        )
        return result
