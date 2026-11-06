import asyncio
import json
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Literal

from pleiad.agent.agent import Agent
from pleiad.agent.errors import AgentConfigurationError
from pleiad.agent.policy import AgentPolicyHints
from pleiad.capabilities.search import BM25Index
from pleiad.context.selection.models import ContextBlock
from pleiad.contracts import CapabilityGrants, ContractModel, JsonObject, SkillRef, ToolRef
from pleiad.events.contracts import Event
from pleiad.events.runtime import TurnStartedEvent
from pleiad.models.anthropic.client import AnthropicModelClient
from pleiad.models.openai.client import OpenAIModelClient
from pleiad.observability.tracing import Observability
from pleiad.orchestration.graph.validation import GraphValidator
from pleiad.runtime.config import RunConfig
from pleiad.runtime.execution.runner import Runner
from pleiad.runtime.lifecycle.result import RunResult
from pleiad.runtime.lifecycle.state import RunState, RunStatus
from pleiad.runtime.policy import HostPolicy
from pleiad.sandbox.policy import SandboxPolicy
from pleiad.sandbox.sandbox import Sandbox
from pleiad.skills.contracts import SkillDescriptor
from pleiad.tools.base import BaseTool, FunctionTool, ToolOutput
from pleiad.tools.contracts import ToolCall, ToolContext, ToolDescriptor, ToolPolicy
from pleiad.tools.interruption import InterruptionResponse, ToolExecutionOutcome, ToolInterruption
from pleiad.tools.registry import ToolFactory
from pleiad.tools.runtime import ToolRuntime
from pydantic import Field

from pleiad_weave.agent.contracts import GraphSpec
from pleiad_weave.agent.graph import GraphRunner, build_graph
from pleiad_weave.agent.prompt import SYSTEM_PROMPT
from pleiad_weave.application.errors import ApprovalConflictError
from pleiad_weave.application.events import ExecutionEventPublisher, current_trace_ids
from pleiad_weave.capabilities.approval import RequiresApprovalTool, with_approvals
from pleiad_weave.capabilities.catalog import CapabilitySnapshot, refresh_capabilities
from pleiad_weave.infrastructure.storage.graphs import GraphConflictError, GraphRevision, TaskGraphStore
from pleiad_weave.infrastructure.storage.memory import MemoryStore
from pleiad_weave.infrastructure.storage.workspaces import SavedGraphReference


class SearchArguments(ContractModel):
    """Validate one model-authored capability search."""

    query: str = ""
    kind: Literal["all", "tool", "skill"] = "all"
    limit: int = Field(default=20, ge=1, le=50)


class SubmitArguments(ContractModel):
    """Validate a product graph proposal and its compare-and-swap revision."""

    graph: GraphSpec
    expected_revision: int | None = Field(default=None, ge=1)


class ExecuteArguments(ContractModel):
    """Select a stored product graph and validated JSON input."""

    graph_id: str = Field(min_length=1)
    graph_revision: int | None = Field(default=None, ge=1)
    input: JsonObject = Field(default_factory=dict)


class ApprovedGraph(ContractModel):
    """Bind an approval to one immutable product graph definition."""

    graph_id: str = Field(min_length=1)
    graph_revision: int = Field(ge=1)
    definition_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @classmethod
    def from_revision(cls, revision: GraphRevision) -> "ApprovedGraph":
        """Identify the exact plan that the workspace owner will inspect.

        Args:
            revision: Validated stored graph revision selected for execution.

        Returns:
            Its immutable revision identity and canonical definition digest.
        """
        payload = json.dumps(
            revision.graph.model_dump(mode="json"), sort_keys=True, ensure_ascii=False, allow_nan=False
        )
        return cls(
            graph_id=revision.graph.graph_id,
            graph_revision=revision.graph.revision,
            definition_sha256=sha256(payload.encode("utf-8")).hexdigest(),
        )


class InspectArguments(ContractModel):
    """Select one stored graph definition."""

    graph_id: str = Field(min_length=1)
    graph_revision: int | None = Field(default=None, ge=1)


class RecallArguments(ContractModel):
    """Search only the product memory scope authorized for this conversation."""

    query: str = Field(min_length=1)
    limit: int = Field(default=5, ge=1, le=20)


@dataclass(slots=True)
class GraphControls:
    """Bind ordinary SDK FunctionTools to one product Execution."""

    owner: "WeaveAgent"
    execution_id: str
    conversation_id: str
    snapshot: CapabilitySnapshot
    context_blocks: tuple[ContextBlock, ...]

    def tools(self) -> tuple[BaseTool, ...]:
        """Declare the product operations available to the supervisor Agent."""
        execute = FunctionTool(
            ToolDescriptor(name="execute_graph", code="weave.execute_graph", description="执行已提交的任务图"),
            self.execute,
            ExecuteArguments,
            policy=ToolPolicy(timeout_seconds=1800.0),
        )
        execute_tool: BaseTool = execute
        if execute.descriptor.code in self.owner.approval_tool_codes:
            execute_tool = RequiresGraphApproval(execute, self)
        return (
            FunctionTool(
                ToolDescriptor(
                    name="search_capabilities",
                    code="weave.search_capabilities",
                    description="查询可用工具和技能。返回名称、能力标识和用途。任务图中的能力标识应从查询结果中选择。",
                ),
                self.search,
                SearchArguments,
            ),
            FunctionTool(
                ToolDescriptor(
                    name="submit_graph",
                    code="weave.submit_graph",
                    description=(
                        "提交完整任务图并校验步骤依赖。节点之间的数据映射使用 RFC 6901 JSON Pointer。"
                        "图的最终结果由 outputs 映射指定。校验通过后才能执行。"
                    ),
                ),
                self.submit,
                SubmitArguments,
            ),
            execute_tool,
            FunctionTool(
                ToolDescriptor(name="inspect_graph", code="weave.inspect_graph", description="查看已提交图的完整定义"),
                self.inspect,
                InspectArguments,
            ),
            FunctionTool(
                ToolDescriptor(
                    name="recall_memory", code="weave.recall_memory", description="检索当前会话及已授权的用户事实"
                ),
                self.recall,
                RecallArguments,
            ),
        )

    async def refresh(self) -> None:
        """Read newly authored capabilities before graph planning or execution."""
        self.snapshot = await self.owner.capabilities()

    async def search(self, context: ToolContext, arguments: JsonObject) -> JsonObject:
        """Search SDK descriptor indices without constructing a second catalog."""
        selected = SearchArguments.model_validate(arguments)
        await self.refresh()
        tools = self.snapshot.tools.search(selected.query, selected.limit) if selected.kind in {"all", "tool"} else ()
        skills = tuple(binding[0] for binding in self.snapshot.skills.bindings.values())
        from_index = (
            self.owner.search_skills(skills, selected.query, selected.limit)
            if selected.kind in {"all", "skill"}
            else ()
        )
        return {
            "tools": [tool.model_dump(mode="json") for tool in tools],
            "skills": [skill.model_dump(mode="json") for skill in from_index],
        }

    async def submit(self, context: ToolContext, arguments: JsonObject) -> JsonObject:
        """Validate the SDK graph and atomically select its product revision."""
        selected = SubmitArguments.model_validate(arguments)
        await self.refresh()
        try:
            graph = build_graph(
                selected.graph,
                self.owner.models,
                self.node_runner(),
                approval_tool_codes=self.owner.approval_tool_codes,
            )
        except (LookupError, ValueError, AgentConfigurationError) as error:
            return {"accepted": False, "reason": str(error)}
        validation = GraphValidator().validate(graph)
        if not validation.valid:
            return {
                "accepted": False,
                "diagnostics": [
                    {"code": item.code, "message": item.message, "path": item.path} for item in validation.diagnostics
                ],
            }
        try:
            stored = await asyncio.to_thread(self.owner.graph_store.submit, selected.graph, selected.expected_revision)
        except GraphConflictError as error:
            return {"accepted": False, "reason": error.message}
        self.owner.execution_graphs[self.execution_id] = (stored.graph.graph_id, stored.graph.revision)
        await self.owner.publisher.emit(
            self.execution_id, "graph.revision.accepted", stored.graph.model_dump(mode="json")
        )
        return {"accepted": True, "graph_id": stored.graph.graph_id, "revision": stored.graph.revision}

    def node_runner(self) -> Runner:
        """Bind a node SDK Runner to the current validated capability snapshot."""

        async def publish(event: Event) -> None:
            """Correlate SDK node progress with its parent product Execution."""
            await self.owner.publisher.publish_sdk_event(self.execution_id, event)

        return Runner(
            host_policy=default_host_policy(),
            tools=self.snapshot.tools,
            skills=self.snapshot.skills,
            tool_runtime=ToolRuntime(),
            event_sink=publish,
            observability=self.owner.observability,
        )

    async def execute(self, context: ToolContext, arguments: JsonObject) -> JsonObject:
        """Delegate stored graph scheduling and Agent execution to the SDK."""
        selected = ExecuteArguments.model_validate(arguments)
        stored = await self.resolve_graph(selected)
        self.owner.execution_graphs[self.execution_id] = (stored.graph.graph_id, stored.graph.revision)
        await self.refresh()
        graph = build_graph(
            stored.graph,
            self.owner.models,
            self.node_runner(),
            approval_tool_codes=self.owner.approval_tool_codes,
            context_blocks=self.context_blocks,
        )
        result = await self.owner.graph_executor.execute(
            graph, selected.input, execution_id=self.execution_id, context=context
        )
        return result.model_dump(mode="json")

    async def resolve_graph(self, selected: ExecuteArguments) -> GraphRevision:
        """Resolve an explicit revision or this Execution's selected graph.

        Args:
            selected: Validated model arguments or server-resolved approval arguments.

        Returns:
            The selected stored revision without falling back from a missing revision.

        Raises:
            LookupError: If the selected revision is not available.
        """
        own_ref = self.owner.execution_graphs.get(self.execution_id)
        if selected.graph_revision is not None:
            stored = await asyncio.to_thread(
                self.owner.graph_store.revision, selected.graph_id, selected.graph_revision
            )
        elif own_ref is not None and own_ref[0] == selected.graph_id:
            stored = await asyncio.to_thread(self.owner.graph_store.revision, selected.graph_id, own_ref[1])
        else:
            stored = await asyncio.to_thread(self.owner.graph_store.current, selected.graph_id)
        if stored is None:
            raise LookupError(f"Graph revision not found: {selected.graph_id}@{selected.graph_revision}")
        return stored

    async def inspect(self, context: ToolContext, arguments: JsonObject) -> JsonObject:
        """Read one revisioned product graph without accessing runtime state."""
        selected = InspectArguments.model_validate(arguments)
        stored = (
            await asyncio.to_thread(self.owner.graph_store.current, selected.graph_id)
            if selected.graph_revision is None
            else await asyncio.to_thread(self.owner.graph_store.revision, selected.graph_id, selected.graph_revision)
        )
        if stored is None:
            raise LookupError(f"Graph revision not found: {selected.graph_id}@{selected.graph_revision}")
        return stored.graph.model_dump(mode="json")

    async def recall(self, context: ToolContext, arguments: JsonObject) -> JsonObject:
        """Apply conversation and user authorization outside reusable SDK memory."""
        selected = RecallArguments.model_validate(arguments)
        records = await asyncio.to_thread(
            self.owner.memory.recall, self.conversation_id, selected.query, min(selected.limit, self.owner.recall_limit)
        )
        return {"facts": [record.model_dump(mode="json") for record in records]}


class RequiresGraphApproval(RequiresApprovalTool):
    """Resolve the exact graph before approval and retain it across restarts."""

    def __init__(self, tool: FunctionTool, controls: GraphControls) -> None:
        """Bind the existing execution Tool and its host-owned revision store.

        Args:
            tool: The real graph execution implementation.
            controls: Execution-local graph selection and persistence binding.
        """
        super().__init__(tool)
        self.controls = controls

    async def execute(self, context: ToolContext, arguments: JsonObject) -> ToolOutput:
        """Record the resolved graph without changing the model's Tool arguments."""
        stored = await self.controls.resolve_graph(ExecuteArguments.model_validate(arguments))
        reference = ApprovedGraph.from_revision(stored)
        self.controls.owner.execution_graphs[self.controls.execution_id] = (
            reference.graph_id,
            reference.graph_revision,
        )
        pending = await super().execute(context, arguments)
        if not isinstance(pending, ToolInterruption):
            raise RuntimeError("Graph approval must create an interruption before execution")
        return pending.model_copy(update={"details": {**pending.details, "graph": reference.model_dump(mode="json")}})

    async def resume_interruption(
        self, context: ToolContext, interruption: ToolInterruption, response: InterruptionResponse
    ) -> ToolExecutionOutcome:
        """Execute only the saved revision after validating its full definition.

        Args:
            context: Current SDK-authorized Tool invocation context.
            interruption: Original action and its server-resolved graph identity.
            response: The product owner's persisted approval or rejection.

        Returns:
            The actual graph outcome or the server-authored rejection result.

        Raises:
            ApprovalConflictError: If the saved target is absent or has changed.
        """
        if not response.result.ok:
            return response.result
        try:
            approved = ApprovedGraph.model_validate(interruption.details.get("graph"))
            selected = ExecuteArguments.model_validate(interruption.arguments)
        except ValueError as error:
            raise ApprovalConflictError("The pending graph approval has no valid resolved revision.") from error
        if selected.graph_id != approved.graph_id or (
            selected.graph_revision is not None and selected.graph_revision != approved.graph_revision
        ):
            raise ApprovalConflictError("The approved graph identity does not match the original action.")
        resolved = selected.model_copy(update={"graph_revision": approved.graph_revision})
        try:
            stored = await self.controls.resolve_graph(resolved)
        except LookupError as error:
            raise ApprovalConflictError("The approved graph revision is no longer available.") from error
        if ApprovedGraph.from_revision(stored) != approved:
            raise ApprovalConflictError("The approved graph definition changed before execution.")
        return await self.execution_runtime.execute(
            self.tool,
            ToolCall(
                call_id=context.call_id,
                tool_code=self.descriptor.code,
                arguments=resolved.model_dump(mode="json"),
            ),
            context,
        )


class WeaveAgent:
    """Compose product graph tools with the public SDK Agent and Runner."""

    def __init__(
        self,
        models: Mapping[str, OpenAIModelClient | AnthropicModelClient],
        workspace: Path,
        manifest: Path,
        sandbox: Sandbox,
        sandbox_policy: SandboxPolicy,
        memory: MemoryStore,
        graph_store: TaskGraphStore,
        graph_executor: GraphRunner,
        publisher: ExecutionEventPublisher,
        observability: Observability,
        recall_limit: int = 5,
        approval_tool_codes: frozenset[str] = frozenset(),
    ) -> None:
        """Bind host-owned composition without implementing an Agent loop."""
        self.models = models
        self.workspace = workspace
        self.manifest = manifest
        self.sandbox = sandbox
        self.sandbox_policy = sandbox_policy
        self.memory = memory
        self.graph_store = graph_store
        self.graph_executor = graph_executor
        self.publisher = publisher
        self.observability = observability
        self.recall_limit = recall_limit
        self.approval_tool_codes = approval_tool_codes
        self.execution_graphs: dict[str, tuple[str, int]] = {}

    async def capabilities(self) -> CapabilitySnapshot:
        """Load capabilities at a product request boundary."""
        return await refresh_capabilities(self.workspace, self.manifest, self.sandbox, self.sandbox_policy)

    async def run(
        self,
        objective: str,
        business_input: JsonObject,
        *,
        conversation_id: str,
        execution_id: str,
        checkpoint: RunState | None = None,
        resolutions: tuple[InterruptionResponse, ...] = (),
    ) -> RunResult:
        """Execute the supervisor using SDK memory, streaming, and Tool runtime."""
        space = business_input.get("weave_space")
        if isinstance(space, dict) and space.get("saved_graph") is not None:
            saved_graph = SavedGraphReference.model_validate(space["saved_graph"])
            self.execution_graphs.setdefault(execution_id, (saved_graph.graph_id, saved_graph.revision))
        snapshot = await self.capabilities()
        blocks = (
            ()
            if not business_input
            else (
                ContextBlock(
                    kind="business_input",
                    origin="weave.request",
                    content=json.dumps(business_input, ensure_ascii=False),
                ),
            )
        )
        node_context = (
            ContextBlock(kind="task_request", origin="weave.execution", content=objective),
            *blocks,
        )
        controls = GraphControls(self, execution_id, conversation_id, snapshot, node_context)
        control_tools = controls.tools()
        for tool in control_tools:
            snapshot.tools.register(tool.descriptor, bind_tool(tool))
        approved_tools = with_approvals(snapshot.tools, self.approval_tool_codes)
        selected_skills = () if not snapshot.skills.contains("tool-development") else (SkillRef("tool-development"),)
        agent = Agent(
            name="weave-supervisor",
            model=self.models["text"],
            prompt=SYSTEM_PROMPT,
            tools=tuple(ToolRef(tool.descriptor.code) for tool in control_tools),
            skills=selected_skills,
            policy_hints=AgentPolicyHints(timeout_seconds=1800.0),
        )
        event_contexts: dict[str, tuple[str | None, str | None]] = {}
        owned_turns: set[str] = set() if checkpoint is None else {checkpoint.turn_id}
        owned_runs: set[str] = set() if checkpoint is None else {checkpoint.run_id}

        async def capture_context(event: Event) -> None:
            """Capture origin span IDs before events cross the SDK stream queue."""
            event_contexts[event.event_id] = current_trace_ids(execution_id)
            if isinstance(event, TurnStartedEvent):
                owned_turns.add(event.turn_id)
                owned_runs.add(event.run_id)

        runner = Runner(
            event_sink=capture_context,
            host_policy=default_host_policy(),
            tools=approved_tools,
            skills=snapshot.skills,
            tool_runtime=ToolRuntime(),
            observability=self.observability,
        )
        memory = self.memory.session(conversation_id)
        result: RunResult | None = None
        try:
            stream = (
                runner.run_streamed(
                    agent, objective, memory=memory, context_blocks=blocks, config=RunConfig(timeout_seconds=1800.0)
                )
                if checkpoint is None
                else runner.resume_streamed(
                    agent,
                    checkpoint,
                    memory=memory,
                    context_blocks=blocks,
                    resolutions=resolutions,
                    config=RunConfig(timeout_seconds=1800.0),
                )
            )
            async with stream:
                async for event in stream.events():
                    await self.publisher.publish_sdk_event(
                        execution_id, event, trace_ids=event_contexts.pop(event.event_id)
                    )
                result = await stream.result()
        finally:
            if result is None or result.status is not RunStatus.INTERRUPTED:
                for run_id in owned_runs:
                    self.graph_executor.release_budget(run_id)
            active = await memory.active()
            if (
                active is not None
                and active.turn_id in owned_turns
                and (result is None or result.status is not RunStatus.INTERRUPTED)
            ):
                await memory.abort(active.turn_id)
        if result.status is RunStatus.SUCCEEDED:
            await self.publisher.emit(
                execution_id,
                "answer.completed",
                {"output_location": "execution"},
                live_data={"text": result.final_output},
            )
        return result

    async def recover_conversation(self, conversation_id: str, turn_id: str | None = None) -> None:
        """Discard only the active Turn owned by a job failed during startup recovery."""
        memory = self.memory.session(conversation_id)
        active = await memory.active()
        if active is not None and (turn_id is None or active.turn_id == turn_id):
            await memory.abort(active.turn_id)

    def graph_for_execution(self, execution_id: str) -> tuple[str, int] | None:
        """Return the graph definition selected by this product Execution."""
        return self.execution_graphs.get(execution_id)

    async def aclose(self) -> None:
        """Close model clients from their product owner rather than from a Run."""
        async with asyncio.TaskGroup() as group:
            for model in self.models.values():
                group.create_task(model.aclose())

    @staticmethod
    def search_skills(descriptors: tuple[SkillDescriptor, ...], query: str, limit: int) -> tuple[SkillDescriptor, ...]:
        """Search Skill metadata with the SDK's deterministic BM25 algorithm."""
        return BM25Index(descriptors).search(query, limit)


def default_host_policy() -> HostPolicy:
    """Grant the workspace operations explicitly authorized by the Weave host."""
    return HostPolicy(
        timeout_seconds=1800.0,
        grants=CapabilityGrants(capabilities=frozenset({"file.read", "file.write", "sandbox.command"})),
    )


def bind_tool(tool: BaseTool) -> ToolFactory:
    """Bind one product control function to the SDK catalog factory contract."""

    def create(descriptor: ToolDescriptor) -> BaseTool:
        """Return the execution-local product control Tool."""
        return tool

    return create
