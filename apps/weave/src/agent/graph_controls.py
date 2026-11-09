import asyncio
from collections.abc import Mapping
from datetime import UTC, datetime

from pleiad.budgets import BudgetLedger
from pleiad.context.selection.models import ContextBlock
from pleiad.contracts import ContractModel, JsonObject, JsonSchema
from pleiad.errors.base import PleiadError
from pleiad.events.contracts import Event
from pleiad.models.anthropic.client import AnthropicModelClient
from pleiad.models.openai.client import OpenAIModelClient
from pleiad.orchestration.graph.executor import GraphExecutor
from pleiad.orchestration.graph.state import GraphStatus, NodeResult, NodeResume, NodeStatus
from pleiad.orchestration.graph.validation import GraphValidator
from pleiad.runtime.runner import Runner
from pleiad.sessions.records import RunStatus
from pleiad.sessions.store import SessionStore
from pleiad.tools.base import BaseTool, FunctionTool, ToolOutput
from pleiad.tools.contracts import ToolContext, ToolDescriptor, ToolPolicy, ToolResult
from pleiad.tools.interruption import InterruptionResponse, ToolExecutionOutcome, ToolInterruption
from pydantic import Field, TypeAdapter

from pleiad_weave.agent.contracts import GraphSpec
from pleiad_weave.agent.graph import build_graph
from pleiad_weave.agent.nodes import NodeExecutor
from pleiad_weave.application.errors import ErrorRecord
from pleiad_weave.infrastructure.storage.product_content import ProductContent
from pleiad_weave.infrastructure.storage.product_details import GraphRunDetail, ProductDetails
from pleiad_weave.infrastructure.storage.product_models import ProductConflict

NODE_RESUMES = TypeAdapter[tuple[NodeResume, ...]](tuple[NodeResume, ...])


class SubmitGraphArguments(ContractModel):
    """Submit a complete revision with an explicit compare-and-swap position."""

    graph: GraphSpec
    expected_revision: int | None = Field(default=None, ge=1)


class InspectGraphArguments(ContractModel):
    """Read an existing owned graph revision."""

    graph_id: str = Field(min_length=1)
    graph_revision: int | None = Field(default=None, ge=1)


class ExecuteGraphArguments(InspectGraphArguments):
    """Invoke one immutable graph using validated structured input."""

    input: JsonObject = Field(default_factory=dict)


class GraphControls:
    """Compose Worker-only graph tools around SDK scheduling and product facts."""

    def __init__(
        self,
        execution_id: str,
        details: ProductDetails,
        content: ProductContent,
        models: Mapping[str, OpenAIModelClient | AnthropicModelClient],
        runner: Runner,
        session_store: SessionStore,
        *,
        context_blocks: tuple[ContextBlock, ...] = (),
        executor: GraphExecutor | None = None,
    ) -> None:
        """Bind real Worker resources without creating another durable job."""
        self.execution_id = execution_id
        self.details = details
        self.content = content
        self.models = models
        self.runner = runner
        self.session_store = session_store
        self.context_blocks = context_blocks
        self.executor = GraphExecutor() if executor is None else executor
        self.lock = asyncio.Lock()

    def tools(self) -> tuple[BaseTool, ...]:
        """Return graph definition, inspection, and recoverable execution tools."""
        return (
            FunctionTool(
                ToolDescriptor(
                    name="submit_graph",
                    code="weave.submit_graph",
                    description="提交完整任务图并校验节点依赖、输入输出与能力绑定。",
                ),
                self.submit,
                SubmitGraphArguments,
            ),
            FunctionTool(
                ToolDescriptor(
                    name="inspect_graph", code="weave.inspect_graph", description="读取当前任务所属的图定义与明确版本。"
                ),
                self.inspect,
                InspectGraphArguments,
            ),
            ExecuteGraphTool(self),
        )

    async def submit(self, context: ToolContext, arguments: JsonObject) -> JsonObject:
        """Validate a definition with the SDK before committing its product revision."""
        selected = SubmitGraphArguments.model_validate(arguments)
        for node in selected.graph.nodes:
            for code in node.tool_codes:
                self.runner.tools.resolve(code)
            for code in node.skill_codes:
                self.runner.skills.resolve(code)
        graph = build_graph(
            selected.graph,
            self.models,
            self.runner,
            session_store=self.session_store,
            context_blocks=self.context_blocks,
        )
        validation = GraphValidator().validate(graph)
        if not validation.valid:
            return {
                "accepted": False,
                "diagnostics": [
                    {"code": value.code, "message": value.message, "path": value.path}
                    for value in validation.diagnostics
                ],
            }
        stored = await asyncio.to_thread(
            self.details.submit, self.execution_id, selected.graph, expected_revision=selected.expected_revision
        )
        return {"accepted": True, "graph_id": stored.graph.graph_id, "revision": stored.graph.revision}

    async def inspect(self, context: ToolContext, arguments: JsonObject) -> JsonObject:
        """Read an owned definition without starting any model invocation."""
        selected = InspectGraphArguments.model_validate(arguments)
        value = await asyncio.to_thread(
            self.details.definition, self.execution_id, selected.graph_id, selected.graph_revision
        )
        return value.model_dump(mode="json")

    async def execute(self, context: ToolContext, arguments: JsonObject) -> ToolOutput:
        """Execute one original tool invocation or recover its already saved result."""
        selected = ExecuteGraphArguments.model_validate(arguments)
        if context.run_id is None:
            raise ProductConflict("A graph invocation requires its owning SDK Run")
        definition = await asyncio.to_thread(
            self.details.definition, self.execution_id, selected.graph_id, selected.graph_revision
        )
        async with self.lock:
            record, duplicate = await asyncio.to_thread(
                self.details.start_graph,
                self.execution_id,
                definition,
                parent_run_id=context.run_id,
                call_id=context.call_id,
                input_value=selected.input,
                requested_revision=selected.graph_revision,
            )
            if duplicate:
                if record.result is not None and record.status is not GraphStatus.RUNNING:
                    return self.outcome(record, context, arguments)
                raise ProductConflict("The original graph invocation has no confirmed resumable result")
            record = await self.run(record, context)
            return self.outcome(record, context, arguments)

    async def run(
        self, record: GraphRunDetail, context: ToolContext, resumes: tuple[NodeResume, ...] = ()
    ) -> GraphRunDetail:
        """Resume SDK GraphState while retaining completed sibling results and budget."""
        if record.status is GraphStatus.PAUSED:
            record = await asyncio.to_thread(
                self.details.save_graph,
                record.model_copy(update={"status": GraphStatus.RUNNING}),
                expected_revision=record.revision,
            )
        definition = await asyncio.to_thread(
            self.details.definition, self.execution_id, record.graph_id, record.graph_revision
        )
        graph = build_graph(
            definition.graph,
            self.models,
            self.runner,
            session_store=self.session_store,
            context_blocks=self.context_blocks,
        )

        async def publish(event: Event) -> None:
            await asyncio.to_thread(self.publish_event, record.graph_run_id, event)

        try:
            result = await self.executor.execute(
                graph,
                record.input,
                NodeExecutor(self.execution_id, record.graph_run_id, self.details, self.content),
                state=record.state,
                expected_revision=record.graph_revision,
                event_sink=publish,
                context=context,
                resumes=resumes,
            )
        except asyncio.CancelledError:
            await asyncio.to_thread(
                self.details.save_graph,
                record.model_copy(update={"status": GraphStatus.CANCELLED, "finished_at": datetime.now(UTC)}),
                expected_revision=record.revision,
            )
            raise
        except PleiadError as error:
            await asyncio.to_thread(
                self.details.save_graph,
                record.model_copy(
                    update={
                        "status": GraphStatus.FAILED,
                        "error": ErrorRecord.from_exception(error),
                        "finished_at": datetime.now(UTC),
                    }
                ),
                expected_revision=record.revision,
            )
            raise
        updated = record.model_copy(
            update={
                "status": result.status,
                "state": result.state,
                "result": result,
                "finished_at": None if result.status is GraphStatus.PAUSED else datetime.now(UTC),
            }
        )
        return await asyncio.to_thread(self.details.save_graph, updated, expected_revision=record.revision)

    def publish_event(self, graph_run_id: str, event: Event) -> None:
        """Commit SDK graph observation with its verified product source identity."""
        with self.details.store.transaction(write=True) as connection:
            self.details.store.emit(
                connection,
                self.execution_id,
                "execution.sdk_event",
                {"graph_run_id": graph_run_id, "event": event.model_dump(mode="json")},
            )

    def outcome(self, record: GraphRunDetail, context: ToolContext, arguments: JsonObject) -> ToolOutput:
        """Project paused node requests or an already committed terminal graph result."""
        if record.result is None:
            raise ProductConflict("The graph invocation has no saved SDK outcome")
        if record.status is GraphStatus.PAUSED:
            pending: list[JsonObject] = []
            for node in record.result.state.node_states:
                checkpoint = node.checkpoint
                if checkpoint is not None:
                    pending.append(
                        {
                            "node_id": node.node_id,
                            "checkpoint": checkpoint.model_dump(mode="json"),
                            "interruptions": [value.model_dump(mode="json") for value in checkpoint.interruptions],
                        }
                    )
            return ToolInterruption(
                interruption_id=f"graph:{record.graph_run_id}:pending",
                call_id=context.call_id,
                tool_code="weave.execute_graph",
                tool_name="execute_graph",
                arguments=arguments,
                reason="Graph nodes require host decisions before execution can continue.",
                details={"kind": "weave.graph", "graph_run_id": record.graph_run_id, "pending": pending},
            )
        return {"graph_run_id": record.graph_run_id, **record.result.model_dump(mode="json")}

    async def resume(
        self, graph_run_id: str, resumes: tuple[NodeResume, ...], context: ToolContext, arguments: JsonObject
    ) -> ToolOutput:
        """Apply trusted node decisions to the original paused GraphRun."""
        async with self.lock:
            record = await asyncio.to_thread(self.details.graph_run, self.execution_id, graph_run_id)
            if record.parent_run_id != context.run_id or record.call_id != context.call_id:
                raise ProductConflict("The GraphRun does not belong to this pending tool invocation")
            if record.status is GraphStatus.PAUSED:
                record = await self.run(record, context, resumes)
            elif record.status is GraphStatus.RUNNING:
                raise ProductConflict("The original graph invocation is still running")
            return self.outcome(record, context, arguments)

    async def cancel_pending(self, graph_run_id: str, reason: str) -> GraphRunDetail:
        """Cancel paused node Sessions through SDK graph cleanup and persist facts."""
        async with self.lock:
            record = await asyncio.to_thread(self.details.graph_run, self.execution_id, graph_run_id)
            if record.status in {GraphStatus.CANCELLED, GraphStatus.SUCCEEDED, GraphStatus.FAILED}:
                return record
            if record.status is not GraphStatus.PAUSED or record.state is None:
                raise ProductConflict("The GraphRun must reach a safe paused position before pending cleanup")
            result = await self.executor.cancel_paused(
                None,
                record.state,
                runner=self.runner,
                session_store=self.session_store,
                reason=reason,
                context=ToolContext(call_id=record.call_id, run_id=record.parent_run_id),
            )
            for node in result.state.node_states:
                if node.status is not NodeStatus.CANCELLED:
                    continue
                attempts = await asyncio.to_thread(
                    self.details.node_attempts, self.execution_id, graph_run_id, node.node_id
                )
                for attempt in attempts.items:
                    if attempt.checkpoint is not None:
                        if node.usage is None:
                            raise ProductConflict("A cancelled Agent node requires confirmed SDK usage")
                        cancelled = NodeResult(
                            node_id=node.node_id,
                            node_run_id=attempt.checkpoint.run_id,
                            session_id=attempt.checkpoint.session_id,
                            status=NodeStatus.CANCELLED,
                            run_status=RunStatus.CANCELLED,
                            usage=node.usage,
                        )
                        await asyncio.to_thread(
                            self.details.save_node,
                            attempt.model_copy(
                                update={
                                    "status": node.status,
                                    "usage": node.usage,
                                    "checkpoint": None,
                                    "result": cancelled,
                                    "finished_at": datetime.now(UTC),
                                }
                            ),
                        )
            return await asyncio.to_thread(
                self.details.save_graph,
                record.model_copy(
                    update={
                        "status": result.status,
                        "state": result.state,
                        "result": result,
                        "finished_at": datetime.now(UTC),
                    }
                ),
                expected_revision=record.revision,
            )


class ExecuteGraphTool(BaseTool):
    """Retain a parent call while its SDK graph contains paused child nodes."""

    def __init__(self, controls: GraphControls) -> None:
        """Bind the actual Worker graph composition."""
        self.controls = controls

    @property
    def descriptor(self) -> ToolDescriptor:
        """Describe the recoverable graph execution tool."""
        return ToolDescriptor(
            name="execute_graph",
            code="weave.execute_graph",
            description="执行已提交的任务图并保留待决节点、过程和产物。",
        )

    @property
    def input_schema(self) -> JsonSchema:
        """Expose explicit graph identity, version, and structured input."""
        return ExecuteGraphArguments.model_json_schema()

    @property
    def policy(self) -> ToolPolicy:
        """Keep graph execution inside its parent Run's controlled tool scope."""
        return ToolPolicy(timeout_seconds=1800, background=False)

    @property
    def budget_ledger(self) -> BudgetLedger:
        """Share parent reservations with every other scoped execution tool."""
        return self.controls.executor.budgets

    async def execute(self, context: ToolContext, arguments: JsonObject) -> ToolOutput:
        """Start the exact graph or replay its confirmed accepted outcome."""
        return await self.controls.execute(context, arguments)

    async def resume_interruption(
        self, context: ToolContext, interruption: ToolInterruption, response: InterruptionResponse
    ) -> ToolExecutionOutcome:
        """Translate host-validated node decisions into the SDK's graph resume path."""
        output = response.result.output
        if output is None or "node_resumes" not in output:
            raise ProductConflict("Graph recovery requires explicit node resume decisions")
        graph_run_id = interruption.details.get("graph_run_id")
        if not isinstance(graph_run_id, str):
            raise ProductConflict("The pending graph has no saved graph-run identity")
        resumes = NODE_RESUMES.validate_python(output["node_resumes"])
        started = datetime.now(UTC)
        result = await self.controls.resume(graph_run_id, resumes, context, interruption.arguments)
        if isinstance(result, ToolInterruption):
            return result
        if not isinstance(result, dict):
            raise RuntimeError("Graph execution returned an unsupported tool outcome")
        return ToolResult(
            call_id=context.call_id, ok=True, output=result, started_at=started, finished_at=datetime.now(UTC)
        )
