import asyncio
import logging

from jsonschema import Draft202012Validator, ValidationError

from pleiad.contracts import JsonObject
from pleiad.errors.base import ErrorType
from pleiad.events.contracts import EventSink
from pleiad.events.graph import GraphEvent, GraphEventKind
from pleiad.observability.attributes import GRAPH_EXECUTE_NAME, GRAPH_ID, NODE_ID
from pleiad.observability.tracing import Observability
from pleiad.orchestration.graph.edge import Edge
from pleiad.orchestration.graph.errors import (
    GraphNodeError,
    GraphRevisionConflictError,
    GraphSystemFault,
    GraphTimeoutError,
    InvalidGraphError,
)
from pleiad.orchestration.graph.graph import Graph
from pleiad.orchestration.graph.mapping import get_pointer, set_pointer
from pleiad.orchestration.graph.node import Node
from pleiad.orchestration.graph.ports import NodeExecutor
from pleiad.orchestration.graph.scheduler import incoming_edges, nodes_by_id, outgoing_edges
from pleiad.orchestration.graph.state import (
    BranchDecision,
    GraphResult,
    GraphState,
    GraphStatus,
    NodeResult,
    NodeState,
    NodeStatus,
)
from pleiad.orchestration.graph.validation import GraphValidator, terminal_status

logger = logging.getLogger(__name__)


class GraphExecutor:
    """Execute a validated DAG with event-driven readiness and recovery state."""

    def __init__(self, validator: GraphValidator | None = None, observability: Observability | None = None) -> None:
        """Create an executor with deterministic schema and mapping validation."""
        self.validator = GraphValidator() if validator is None else validator
        self.observability = Observability() if observability is None else observability

    async def execute(
        self,
        graph: Graph,
        graph_input: JsonObject,
        node_executor: NodeExecutor | None = None,
        *,
        state: GraphState | None = None,
        expected_revision: int | None = None,
        event_sink: EventSink | None = None,
    ) -> GraphResult:
        """Validate and execute one Graph revision or resume its confirmed state."""
        if expected_revision is not None and expected_revision != graph.revision:
            return self.failed_result(graph, GraphRevisionConflictError())
        validation = self.validator.validate(graph)
        if not validation.valid:
            summary = "; ".join(f"{item.code} at {item.path or 'graph'}" for item in validation.diagnostics)
            return self.failed_result(graph, InvalidGraphError(f"Graph validation failed: {summary}."), state)
        if state is not None:
            self.validate_state(graph, state)
        try:
            Draft202012Validator(graph.input_schema).validate(graph_input)
        except ValidationError:
            return self.failed_result(graph, InvalidGraphError("The Graph input does not satisfy its schema."), state)
        states = self.initial_states(graph, state)
        decisions = {decision.edge_id: decision.activated for decision in (state.branch_decisions if state else ())}
        try:
            async with asyncio.timeout(graph.timeout_seconds):
                with self.observability.span(
                    GRAPH_EXECUTE_NAME,
                    attributes={GRAPH_ID: graph.graph_id},
                    observation_type="chain",
                ) as graph_span:
                    await self.emit(event_sink, graph, GraphEventKind.GRAPH_STARTED, None, None)
                    result = await self.execute_ready(graph, graph_input, node_executor, states, decisions, event_sink)
                    self.observability.finish(
                        graph_span,
                        result.status.value,
                        error_code=result.error_code,
                        error_type=result.error_type.value if result.error_type is not None else None,
                    )
                    terminal_kind = {
                        GraphStatus.SUCCEEDED: GraphEventKind.GRAPH_COMPLETED,
                        GraphStatus.FAILED: GraphEventKind.GRAPH_FAILED,
                        GraphStatus.PAUSED: GraphEventKind.GRAPH_PAUSED,
                        GraphStatus.CANCELLED: GraphEventKind.GRAPH_CANCELLED,
                    }[result.status]
                    await self.emit(
                        event_sink,
                        graph,
                        terminal_kind,
                        None,
                        None,
                        status=result.status,
                    )
                    return result
        except TimeoutError:
            result = self.finish(graph, states, decisions, GraphTimeoutError())
            await self.emit(
                event_sink,
                graph,
                GraphEventKind.GRAPH_FAILED,
                None,
                None,
                status=result.status,
            )
            return result
        except asyncio.CancelledError:
            await self.emit(
                event_sink,
                graph,
                GraphEventKind.GRAPH_CANCELLED,
                None,
                None,
                status=GraphStatus.CANCELLED,
            )
            raise

    async def execute_ready(
        self,
        graph: Graph,
        graph_input: JsonObject,
        node_executor: NodeExecutor | None,
        states: dict[str, NodeState],
        decisions: dict[str, bool],
        event_sink: EventSink | None,
    ) -> GraphResult:
        """Run ready Nodes until all dependencies reach a terminal state."""
        running: dict[asyncio.Task[NodeResult], str] = {}
        try:
            async with asyncio.TaskGroup() as tasks:
                try:
                    result = await self.schedule_nodes(
                        graph, graph_input, node_executor, states, decisions, event_sink, tasks, running
                    )
                finally:
                    await self.cancel_tasks(tuple(running))
                    nodes = nodes_by_id(graph.nodes)
                    for task, node_id in running.items():
                        if task.cancelled() or task.exception() is not None:
                            continue
                        confirmed = task.result()
                        states[node_id] = self.confirm_node(nodes[node_id], confirmed).model_copy(
                            update={"attempt": states[node_id].attempt}
                        )
                        self.update_decisions(graph, node_id, states[node_id], decisions)
        except ExceptionGroup as errors:
            if len(errors.exceptions) == 1:
                raise errors.exceptions[0] from errors
            raise
        return result.model_copy(update={"state": self.finish(graph, states, decisions).state})

    async def schedule_nodes(
        self,
        graph: Graph,
        graph_input: JsonObject,
        node_executor: NodeExecutor | None,
        states: dict[str, NodeState],
        decisions: dict[str, bool],
        event_sink: EventSink | None,
        tasks: asyncio.TaskGroup,
        running: dict[asyncio.Task[NodeResult], str],
    ) -> GraphResult:
        """Schedule nodes while keeping recovery state owned by the invocation."""
        nodes = nodes_by_id(graph.nodes)
        while True:
            pending = tuple(node_id for node_id, item in states.items() if item.status is NodeStatus.PENDING)
            if not pending and not running:
                return self.finish(graph, states, decisions)
            progress = False
            for node_id in pending:
                readiness = self.readiness(graph, node_id, states, decisions)
                if readiness == "skip":
                    states[node_id] = states[node_id].model_copy(update={"status": NodeStatus.SKIPPED})
                    await self.emit(event_sink, graph, GraphEventKind.NODE_SKIPPED, node_id, None)
                    progress = True
                elif readiness == "ready" and len(running) < graph.max_concurrency:
                    node = nodes[node_id]
                    active_incoming = sum(decisions.get(edge.edge_id, False) for edge in incoming_edges(graph, node_id))
                    if active_incoming > 1:
                        await self.emit(event_sink, graph, GraphEventKind.JOIN_READY, node_id, None)
                    input_value = self.node_input(graph, node_id, graph_input, states, decisions)
                    try:
                        Draft202012Validator(node.input_schema).validate(input_value)
                    except ValidationError:
                        return self.finish(
                            graph,
                            states,
                            decisions,
                            GraphNodeError(f"Graph node input is invalid: {node_id}."),
                        )
                    states[node_id] = states[node_id].model_copy(
                        update={"status": NodeStatus.RUNNING, "attempt": states[node_id].attempt + 1}
                    )
                    await self.emit(event_sink, graph, GraphEventKind.NODE_STARTED, node_id, None)
                    task = tasks.create_task(self.run_node(node, input_value, node_executor))
                    running[task] = node_id
                    progress = True
            if not running:
                if progress:
                    continue
                raise GraphSystemFault("Graph scheduler reached a state with no ready Node.")
            done, _ = await asyncio.wait(tuple(running), return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                node_id = running.pop(task)
                result = task.result()
                node = nodes[node_id]
                states[node_id] = self.confirm_node(node, result).model_copy(
                    update={"attempt": states[node_id].attempt}
                )
                self.update_decisions(graph, node_id, states[node_id], decisions)
                await self.emit(
                    event_sink,
                    graph,
                    GraphEventKind.NODE_COMPLETED,
                    node_id,
                    result.node_run_id,
                )

    async def run_node(
        self,
        node: Node,
        input_value: JsonObject,
        node_executor: NodeExecutor | None,
    ) -> NodeResult:
        """Execute one Node through the injected boundary or its binding."""
        with self.observability.span(
            "pleiad.graph.node",
            attributes={NODE_ID: node.node_id},
            observation_type="chain",
        ) as node_span:
            if node_executor is None:
                result = await node.run(input_value)
            else:
                result = await node_executor.execute(node, input_value)
            self.observability.finish(
                node_span,
                result.status.value,
                error_code=result.error_code,
                error_type=result.error_type.value if result.error_type is not None else None,
            )
            return result

    def initial_states(self, graph: Graph, previous: GraphState | None) -> dict[str, NodeState]:
        """Create mutable execution state while preserving confirmed outputs."""
        if previous is None:
            return {node.node_id: NodeState(node_id=node.node_id) for node in graph.nodes}
        restored = {item.node_id: item for item in previous.node_states}
        states: dict[str, NodeState] = {}
        for node in graph.nodes:
            item = restored.get(node.node_id)
            if item is None:
                states[node.node_id] = NodeState(node_id=node.node_id)
            elif item.status in {
                NodeStatus.RUNNING,
                NodeStatus.PAUSED,
                NodeStatus.FAILED,
                NodeStatus.SKIPPED,
            }:
                if node.side_effect:
                    raise GraphSystemFault(f"Node result is unknown and cannot be retried: {node.node_id}.")
                states[node.node_id] = item.model_copy(update={"status": NodeStatus.PENDING})
            else:
                states[node.node_id] = item
        return states

    def validate_state(self, graph: Graph, state: GraphState) -> None:
        """Reject silent recovery against a different Graph definition."""
        if state.graph_id != graph.graph_id or state.revision != graph.revision:
            raise GraphSystemFault("Graph recovery state does not match the requested revision.")
        if state.graph_fingerprint != graph.fingerprint():
            raise GraphSystemFault("Graph recovery state fingerprint does not match the requested definition.")

    def readiness(
        self,
        graph: Graph,
        node_id: str,
        states: dict[str, NodeState],
        decisions: dict[str, bool],
    ) -> str:
        """Return ready, skip, or pending after evaluating all incoming edges."""
        edges = incoming_edges(graph, node_id)
        if not edges:
            return "ready"
        if any(not terminal_status(states[edge.source_node_id].status) for edge in edges):
            return "pending"
        active: list[Edge] = []
        for edge in edges:
            source = states[edge.source_node_id]
            if edge.edge_id not in decisions:
                decisions[edge.edge_id] = source.status is NodeStatus.SUCCEEDED and (
                    edge.condition is None or edge.condition.matches(source.output or {})
                )
            if decisions[edge.edge_id]:
                active.append(edge)
        if not active:
            return "skip"
        if any(states[edge.source_node_id].status is not NodeStatus.SUCCEEDED for edge in active):
            return "skip"
        return "ready"

    def node_input(
        self,
        graph: Graph,
        node_id: str,
        graph_input: JsonObject,
        states: dict[str, NodeState],
        decisions: dict[str, bool],
    ) -> JsonObject:
        """Build one node's input from graph input or activated edge mappings."""
        edges = incoming_edges(graph, node_id)
        if not edges:
            return dict(graph_input)
        target: JsonObject = {}
        for edge in edges:
            if not decisions.get(edge.edge_id, False):
                continue
            output = states[edge.source_node_id].output or {}
            for mapping in edge.mappings:
                set_pointer(target, mapping.target_path, get_pointer(output, mapping.source_path))
        return target

    def confirm_node(self, node: Node, result: NodeResult) -> NodeState:
        """Validate Node identity, output schema, and business status."""
        if result.node_id != node.node_id:
            raise GraphSystemFault("Node executor returned a mismatched Node identity.")
        if result.status is NodeStatus.SUCCEEDED:
            try:
                Draft202012Validator(node.output_schema).validate(result.output)
            except ValidationError:
                error = GraphNodeError(f"Graph node output is invalid: {node.node_id}.")
                return NodeState(
                    node_id=node.node_id,
                    status=NodeStatus.FAILED,
                    node_run_id=result.node_run_id,
                    error_code=error.code,
                    error_message=error.message,
                    error_type=error.type,
                )
        if result.status is NodeStatus.UNKNOWN:
            raise GraphSystemFault(f"Node result is unknown: {node.node_id}.")
        return NodeState(
            node_id=node.node_id,
            status=result.status,
            node_run_id=result.node_run_id,
            output=result.output,
            error_code=result.error_code,
            error_message=result.error_message,
            error_type=result.error_type,
        )

    def update_decisions(
        self,
        graph: Graph,
        source_node_id: str,
        result: NodeState,
        decisions: dict[str, bool],
    ) -> None:
        """Record all outgoing branch choices when a source result is confirmed."""
        for edge in outgoing_edges(graph, source_node_id):
            decisions[edge.edge_id] = result.status is NodeStatus.SUCCEEDED and (
                edge.condition is None or edge.condition.matches(result.output or {})
            )

    def finish(
        self,
        graph: Graph,
        states: dict[str, NodeState],
        decisions: dict[str, bool],
        error: GraphNodeError | InvalidGraphError | GraphTimeoutError | GraphRevisionConflictError | None = None,
    ) -> GraphResult:
        """Build a GraphResult from confirmed state and output bindings."""
        state = GraphState(
            graph_id=graph.graph_id,
            revision=graph.revision,
            graph_fingerprint=graph.fingerprint(),
            node_states=tuple(states[node.node_id] for node in graph.nodes),
            branch_decisions=tuple(
                BranchDecision(edge_id=edge_id, activated=activated) for edge_id, activated in sorted(decisions.items())
            ),
            scheduling_position=sum(item.status is NodeStatus.SUCCEEDED for item in states.values()),
        )
        if error is not None:
            return GraphResult(
                graph_id=graph.graph_id,
                revision=graph.revision,
                status=GraphStatus.FAILED,
                state=state,
                error_code=error.code,
                error_message=error.message,
                error_type=error.type,
            )
        if any(item.status is NodeStatus.FAILED for item in states.values()):
            failed = next(item for item in states.values() if item.status is NodeStatus.FAILED)
            return GraphResult(
                graph_id=graph.graph_id,
                revision=graph.revision,
                status=GraphStatus.FAILED,
                state=state,
                error_code=failed.error_code or GraphNodeError().code,
                error_message=failed.error_message or GraphNodeError().message,
                error_type=failed.error_type or ErrorType.BUSINESS,
            )
        if any(item.status is NodeStatus.PAUSED for item in states.values()):
            return GraphResult(graph_id=graph.graph_id, revision=graph.revision, status=GraphStatus.PAUSED, state=state)
        try:
            outputs = self.outputs(graph, states)
            Draft202012Validator(graph.output_schema).validate(outputs)
        except (KeyError, IndexError, TypeError, ValueError, ValidationError):
            return self.failed_result(graph, GraphNodeError("The Graph output does not satisfy its schema."), state)
        return GraphResult(
            graph_id=graph.graph_id,
            revision=graph.revision,
            status=GraphStatus.SUCCEEDED,
            state=state,
            outputs=outputs,
        )

    def outputs(self, graph: Graph, states: dict[str, NodeState]) -> JsonObject:
        """Project declared outputs or deterministic sink outputs."""
        if not graph.outputs:
            sinks = tuple(node.node_id for node in graph.nodes if not outgoing_edges(graph, node.node_id))
            return {node_id: states[node_id].output for node_id in sorted(sinks)}
        outputs: JsonObject = {}
        for binding in graph.outputs:
            output = states[binding.source_node_id].output or {}
            set_pointer(outputs, binding.target_path, get_pointer(output, binding.source_path))
        return outputs

    async def emit(
        self,
        sink: EventSink | None,
        graph: Graph,
        kind: GraphEventKind,
        node_id: str | None,
        node_run_id: str | None,
        *,
        status: GraphStatus | None = None,
    ) -> None:
        """Publish one Graph fact without changing scheduling semantics."""
        if sink is None:
            return
        try:
            await sink(
                GraphEvent(
                    kind=kind,
                    graph_id=graph.graph_id,
                    revision=graph.revision,
                    node_id=node_id,
                    node_run_id=node_run_id,
                    status=status,
                    run_id=node_run_id,
                )
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.error(
                f"Graph event notification failed graph_id={graph.graph_id} "
                f"kind={kind.value} error_type={type(error).__name__}"
            )

    async def cancel_tasks(self, tasks: tuple[asyncio.Task[NodeResult], ...]) -> None:
        """Cancel and await all Node tasks owned by this executor."""
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    @staticmethod
    def failed_result(
        graph: Graph,
        error: GraphNodeError | GraphRevisionConflictError | InvalidGraphError | GraphTimeoutError,
        state: GraphState | None = None,
    ) -> GraphResult:
        """Project a business failure before node execution begins."""
        current = state or GraphState(
            graph_id=graph.graph_id,
            revision=graph.revision,
            graph_fingerprint=graph.fingerprint(),
            node_states=tuple(NodeState(node_id=node.node_id) for node in graph.nodes),
        )
        return GraphResult(
            graph_id=graph.graph_id,
            revision=graph.revision,
            status=GraphStatus.FAILED,
            state=current,
            error_code=error.code,
            error_message=error.message,
            error_type=error.type,
        )
