"""Execute a validated TaskGraph with bounded parallelism."""

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

from jsonschema import Draft202012Validator

from pleiad.errors import ErrorCode, PleiadError, make_error
from pleiad.graph.contracts import (
    GraphResult,
    GraphSpec,
    GraphStatus,
    NodeResult,
    NodeSpec,
    NodeStatus,
)
from pleiad.graph.validator import GraphValidator
from pleiad.observability import (
    ObservabilityPort,
    SpanKind,
    observed,
)
from pleiad.runtime.capabilities import WorkspaceCapabilitySync
from pleiad.runtime.events import EventBus
from pleiad.runtime.mapping import get_pointer, set_pointer
from pleiad.runtime.node_runner import NodeRunner
from pleiad.runtime.scheduler import edges_into, topological_stages
from pleiad.storage.models import GraphRunRecord
from pleiad.storage.repository import GraphRunRepository


class GraphExecutor:
    """Schedule independent Nodes and pass structured outputs over Edges."""

    def __init__(
        self,
        validator: GraphValidator,
        runner: NodeRunner,
        events: EventBus | None = None,
        graph_runs: GraphRunRepository | None = None,
        observability: ObservabilityPort | None = None,
        capability_sync: WorkspaceCapabilitySync | None = None,
    ) -> None:
        self.validator = validator
        self.runner = runner
        self.events = events
        self.graph_runs = graph_runs
        self.observability = observability
        self.capability_sync = capability_sync

    @observed(
        name="graph.execute",
        kind=SpanKind.GRAPH,
        attributes=lambda args, kwargs: {
            "graph_id": getattr(args[1], "graph_id", "unknown"),
            "revision": getattr(args[1], "revision", "unknown"),
        },
    )
    async def execute(
        self,
        graph: GraphSpec,
        graph_input: dict[str, object],
        *,
        execution_id: str,
    ) -> GraphResult:
        """Execute one graph with an automatically managed Graph Span."""
        return await self._execute(
            graph,
            graph_input,
            execution_id=execution_id,
        )

    async def _execute(
        self,
        graph: GraphSpec,
        graph_input: dict[str, object],
        *,
        execution_id: str,
    ) -> GraphResult:
        """Validate and execute one immutable graph revision."""
        if self.capability_sync:
            self.capability_sync.sync(execution_id=execution_id)
        graph_run = GraphRunRecord(
            graph_run_id=uuid4().hex,
            execution_id=execution_id,
            graph_id=graph.graph_id,
            graph_revision=graph.revision,
            status=GraphStatus.RUNNING,
        )
        if self.graph_runs:
            self.graph_runs.save(graph_run)
        self.events and self.events.emit(
            execution_id,
            "graph.started",
            {"graph_id": graph.graph_id, "revision": graph.revision},
        )
        validation = self.validator.validate(graph)
        if not validation.valid:
            result = GraphResult(
                graph_id=graph.graph_id,
                revision=graph.revision,
                status=GraphStatus.FAILED,
                error=make_error(
                    ErrorCode.GRAPH_INVALID,
                    details={
                        "diagnostics": [
                            item.model_dump(mode="json") for item in validation.diagnostics
                        ]
                    },
                ),
            )
            self._finish_graph(graph_run, result)
            return result
        try:
            Draft202012Validator(graph.input_schema).validate(graph_input)
            stages = topological_stages(graph)
        except Exception as exc:
            result = GraphResult(
                graph_id=graph.graph_id,
                revision=graph.revision,
                status=GraphStatus.FAILED,
                error=make_error(
                    ErrorCode.GRAPH_INVALID,
                    details={"exception": exc.__class__.__name__},
                ),
            )
            self._finish_graph(graph_run, result)
            return result

        node_inputs: dict[str, dict[str, object]] = {
            node.node_id: dict(graph_input)
            for node in graph.nodes
            if not edges_into(graph, node.node_id)
        }
        results: dict[str, NodeResult] = {}
        semaphore = asyncio.Semaphore(graph.max_concurrency)
        for stage in stages:

            async def run_node(node: NodeSpec) -> NodeResult:
                async with semaphore:
                    return await self.runner.run(
                        node,
                        node_inputs.get(node.node_id, {}),
                        execution_id=execution_id,
                        graph_id=graph.graph_id,
                        graph_revision=graph.revision,
                        events=self.events,
                    )

            for node in stage:
                self.events and self.events.emit(
                    execution_id,
                    "node.ready",
                    {"graph_id": graph.graph_id, "node_id": node.node_id},
                )
            try:
                stage_results = await asyncio.gather(*(run_node(node) for node in stage))
            except PleiadError as exc:
                self.events and self.events.emit(
                    execution_id,
                    "graph.failed",
                    {"graph_id": graph.graph_id, "status": GraphStatus.FAILED.value},
                )
                result = GraphResult(
                    graph_id=graph.graph_id,
                    revision=graph.revision,
                    status=GraphStatus.FAILED,
                    node_results=tuple(results.values()),
                    error=exc.info,
                )
                self._finish_graph(graph_run, result)
                return result
            except Exception as exc:
                self.events and self.events.emit(
                    execution_id,
                    "graph.failed",
                    {"graph_id": graph.graph_id, "status": GraphStatus.FAILED.value},
                )
                result = GraphResult(
                    graph_id=graph.graph_id,
                    revision=graph.revision,
                    status=GraphStatus.FAILED,
                    node_results=tuple(results.values()),
                    error=make_error(
                        ErrorCode.SYSTEM_UNEXPECTED,
                        details={"exception": exc.__class__.__name__},
                    ),
                )
                self._finish_graph(graph_run, result)
                return result
            for node_result in stage_results:
                results[node_result.node_id] = node_result
                if node_result.status is not NodeStatus.SUCCEEDED:
                    self.events and self.events.emit(
                        execution_id,
                        "graph.failed",
                        {"graph_id": graph.graph_id, "status": GraphStatus.FAILED.value},
                    )
                    graph_result = GraphResult(
                        graph_id=graph.graph_id,
                        revision=graph.revision,
                        status=GraphStatus.FAILED,
                        node_results=tuple(results.values()),
                        error=node_result.error
                        or make_error(
                            ErrorCode.GRAPH_EXECUTION,
                            details={"node_id": node_result.node_id},
                        ),
                    )
                    self._finish_graph(graph_run, graph_result)
                    return graph_result
            try:
                for node in stage:
                    output = results[node.node_id].output or {}
                    for edge in (
                        edge for edge in graph.edges if edge.source_node_id == node.node_id
                    ):
                        target = node_inputs.setdefault(edge.target_node_id, {})
                        for mapping in edge.mappings:
                            set_pointer(
                                target,
                                mapping.target_path,
                                get_pointer(output, mapping.source_path),
                            )
            except Exception as exc:
                self.events and self.events.emit(
                    execution_id,
                    "graph.failed",
                    {"graph_id": graph.graph_id, "status": GraphStatus.FAILED.value},
                )
                result = GraphResult(
                    graph_id=graph.graph_id,
                    revision=graph.revision,
                    status=GraphStatus.FAILED,
                    node_results=tuple(results.values()),
                    error=make_error(
                        ErrorCode.GRAPH_EXECUTION,
                        details={"exception": exc.__class__.__name__},
                    ),
                )
                self._finish_graph(graph_run, result)
                return result
        try:
            outputs = self._outputs(graph, results)
        except Exception as exc:
            self.events and self.events.emit(
                execution_id,
                "graph.failed",
                {"graph_id": graph.graph_id, "status": GraphStatus.FAILED.value},
            )
            result = GraphResult(
                graph_id=graph.graph_id,
                revision=graph.revision,
                status=GraphStatus.FAILED,
                node_results=tuple(results.values()),
                error=make_error(
                    ErrorCode.GRAPH_EXECUTION,
                    details={"exception": exc.__class__.__name__},
                ),
            )
            self._finish_graph(graph_run, result)
            return result
        try:
            Draft202012Validator(graph.output_schema).validate(outputs)
        except Exception as exc:
            self.events and self.events.emit(
                execution_id,
                "graph.failed",
                {"graph_id": graph.graph_id, "status": GraphStatus.FAILED.value},
            )
            result = GraphResult(
                graph_id=graph.graph_id,
                revision=graph.revision,
                status=GraphStatus.FAILED,
                outputs=outputs,
                node_results=tuple(results.values()),
                error=make_error(
                    ErrorCode.GRAPH_EXECUTION,
                    details={"exception": exc.__class__.__name__},
                ),
            )
            self._finish_graph(graph_run, result)
            return result
        self.events and self.events.emit(
            execution_id,
            "graph.completed",
            {"graph_id": graph.graph_id, "status": GraphStatus.SUCCEEDED.value},
        )
        result = GraphResult(
            graph_id=graph.graph_id,
            revision=graph.revision,
            status=GraphStatus.SUCCEEDED,
            outputs=outputs,
            node_results=tuple(results.values()),
        )
        self._finish_graph(graph_run, result)
        return result

    def _finish_graph(self, running: GraphRunRecord, result: GraphResult) -> None:
        """Persist the terminal Graph state without coupling it to definition storage."""
        if self.graph_runs:
            self.graph_runs.save(
                running.model_copy(
                    update={
                        "status": result.status,
                        "outputs": result.outputs,
                        "error": result.error,
                        "finished_at": datetime.now(UTC),
                    }
                )
            )

    @staticmethod
    def _outputs(graph: GraphSpec, results: dict[str, NodeResult]) -> dict[str, object]:
        if not graph.outputs:
            sinks = {
                node.node_id
                for node in graph.nodes
                if not any(edge.source_node_id == node.node_id for edge in graph.edges)
            }
            return {node_id: results[node_id].output for node_id in sorted(sinks)}
        outputs: dict[str, object] = {}
        for binding in graph.outputs:
            output = results[binding.source_node_id].output or {}
            set_pointer(outputs, binding.target_path, get_pointer(output, binding.source_path))
        return outputs
