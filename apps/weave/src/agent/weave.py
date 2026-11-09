import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from pleiad.agent.agent import Agent
from pleiad.capabilities.search import BM25Index
from pleiad.context.selection.models import ContextBlock
from pleiad.contracts import CapabilityGrants, ContractModel, JsonObject, SkillRef, ToolRef
from pleiad.events.contracts import Event, EventSink
from pleiad.messages.message import AssistantMessage
from pleiad.models.anthropic.client import AnthropicModelClient
from pleiad.models.openai.client import OpenAIModelClient
from pleiad.observability.tracing import Observability
from pleiad.orchestration.subagents.group import SubagentGroup
from pleiad.orchestration.subagents.subagent import Subagent
from pleiad.orchestration.subagents.tools import as_tool, submit_subagent, wait_subagents
from pleiad.runtime.limits import RunLimits
from pleiad.runtime.policy import HostPolicy
from pleiad.runtime.runner import Runner
from pleiad.sandbox.policy import SandboxPolicy
from pleiad.sandbox.sandbox import Sandbox
from pleiad.sessions.recovery import ActionRef
from pleiad.sessions.store import SessionStore
from pleiad.skills.loader import SkillLoader
from pleiad.tools.base import BaseTool, FunctionTool, ToolOutput
from pleiad.tools.contracts import DEFAULT_TOOL_CODES, ToolContext, ToolDescriptor
from pleiad.tools.errors import ToolError
from pleiad.tools.interruption import ToolInterruption
from pleiad.tools.registry import ToolFactory, ToolRegistry
from pleiad.tools.runtime import ToolRuntime
from pydantic import Field

from pleiad_weave.agent.content_tools import ExecutionContent, WorkspaceResource
from pleiad_weave.agent.graph_controls import GraphControls
from pleiad_weave.capabilities.approval import with_approvals
from pleiad_weave.capabilities.catalog import load_capabilities, refresh_capabilities
from pleiad_weave.capabilities.mcp import load_mcp_configuration
from pleiad_weave.capabilities.resources import (
    ResourceDetail,
    ResourceKind,
    environment_detail,
    model_credentials,
    model_detail,
    redact_value,
    skill_detail,
    tool_detail,
)
from pleiad_weave.infrastructure.storage.memory import FactStore, MemoryFact
from pleiad_weave.infrastructure.storage.model_inputs import (
    MAX_MODEL_SOURCE_PAGE_CHARS,
    MODEL_INLINE_BYTES,
    MODEL_PAGE_ITEMS,
    MODEL_SOURCE_PAGE_CHARS,
    input_projection,
    page_projection,
    record_projection,
    snapshot_projection,
    source_page,
    source_reference,
)
from pleiad_weave.infrastructure.storage.product import ProductStore, stable_id
from pleiad_weave.infrastructure.storage.product_content import ProductContent
from pleiad_weave.infrastructure.storage.product_details import ProductDetails, ToolResultDetail
from pleiad_weave.infrastructure.storage.product_models import (
    ArtifactMetadata,
    Attention,
    Execution,
    ExecutionSpec,
    InboxItem,
    InputDisposition,
    Message,
    Report,
    RouteAction,
    RouteAttempt,
    Space,
    SpaceConfiguration,
)


class RouteDraft(ContractModel):
    """Validate model-authored choices while the host owns batch identity and versions."""

    reply: str = ""
    actions: tuple[RouteAction, ...] = Field(default=(), max_length=20)


class VerificationDraft(ContractModel):
    """Reference real evidence while the host supplies its recorded execution time."""

    name: str = Field(min_length=1)
    status: Literal["passed", "failed", "unknown"]
    evidence_refs: tuple[str, ...] = Field(
        default=(), description="Exact artifact_id, node_attempt_id, or tool_result_id returned by host tools."
    )


class CompletionDraft(ContractModel):
    """Declare delivery evidence and the exact inputs answered by the final output."""

    kind: Literal["ready", "failed"] = "ready"
    summary: str = Field(min_length=1)
    handled_input_ids: tuple[str, ...]
    artifact_ids: tuple[str, ...] = ()
    verification: tuple[VerificationDraft, ...] = ()


class QueryArguments(ContractModel):
    """Select a read-only product fact or the current resource catalog."""

    kind: Literal[
        "snapshot",
        "space",
        "execution",
        "attention",
        "resources",
        "chat",
        "message",
        "report",
        "artifact",
        "inputs",
        "input",
        "spaces",
        "executions",
        "attentions",
    ] = "snapshot"
    id: str | None = None
    before_sequence: int | None = Field(default=None, ge=1)
    limit: int = Field(default=MODEL_PAGE_ITEMS, ge=1, le=MODEL_PAGE_ITEMS)
    cursor: int = Field(default=0, ge=0)
    start_char: int = Field(default=0, ge=0)
    max_chars: int = Field(default=MODEL_SOURCE_PAGE_CHARS, ge=1, le=MAX_MODEL_SOURCE_PAGE_CHARS)
    expected_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class QuestionArguments(ContractModel):
    """Request concrete information before continuing the same Worker Run."""

    question: str = Field(min_length=1)
    options: tuple[str, ...] = ()


class ProgressArguments(ContractModel):
    """Record a bounded progress summary in execution details."""

    summary: str = Field(min_length=1, max_length=16_000)


class ReturnArguments(ContractModel):
    """Return an unresolved owned input with all already performed effects."""

    input_id: str
    expected_revision: int = Field(ge=1)
    reason: str = Field(min_length=1)
    already_handled_refs: tuple[str, ...] = ()
    effect_refs: tuple[str, ...] = ()
    unknown_effects: bool = False


class DelegateArguments(ContractModel):
    """Delegate real independently owned work from an existing Execution."""

    space_id: str
    objective: str = Field(min_length=1)
    budget_limit: int = Field(default=20_000, gt=0)


class SearchArguments(ContractModel):
    """Search metadata within the current authorized capability snapshot."""

    query: str = ""
    limit: int = Field(default=10, ge=1, le=30)


class WorkQueryArguments(ContractModel):
    """Read bounded sources within the current Worker's bound Execution."""

    kind: Literal["overview", "input", "message", "execution", "space", "objective", "disposition", "fact"] = "overview"
    id: str | None = None
    cursor: int = Field(default=0, ge=0)
    limit: int = Field(default=MODEL_PAGE_ITEMS, ge=1, le=MODEL_PAGE_ITEMS)
    start_char: int = Field(default=0, ge=0)
    max_chars: int = Field(default=MODEL_SOURCE_PAGE_CHARS, ge=1, le=MAX_MODEL_SOURCE_PAGE_CHARS)
    expected_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


@dataclass(slots=True)
class RouterBinding:
    """Keep one short routing invocation and its host-collected read versions."""

    agent: Agent
    runner: Runner
    context: tuple[ContextBlock, ...]
    versions: dict[str, int]
    ready: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass(slots=True)
class WorkerBinding:
    """Own execution-local graph and scoped delegation resources."""

    agent: Agent
    runner: Runner
    group: SubagentGroup
    context: tuple[ContextBlock, ...]
    graph: GraphControls
    ready: asyncio.Event = field(default_factory=asyncio.Event)


class WeaveAgents:
    """Compose read-only Router and task-local Worker definitions through public SDK APIs."""

    def __init__(
        self,
        models: Mapping[str, OpenAIModelClient | AnthropicModelClient],
        workspace: Path,
        manifest: Path,
        sandbox: Sandbox,
        sandbox_policy: SandboxPolicy,
        store: ProductStore,
        sessions: SessionStore,
        content: ProductContent,
        details: ProductDetails,
        observability: Observability,
        approval_tool_codes: frozenset[str] = frozenset(),
        *,
        facts: FactStore | None = None,
        memory_recall_max_results: int = 5,
    ) -> None:
        """Bind host-authorized models, capability roots, and product stores."""
        self.models = models
        self.workspace = workspace
        self.manifest = manifest
        self.sandbox = sandbox
        self.sandbox_policy = sandbox_policy
        self.store = store
        self.sessions = sessions
        self.content = content
        self.details = details
        self.observability = observability
        self.approval_tool_codes = approval_tool_codes
        self.facts = facts
        self.memory_recall_max_results = memory_recall_max_results

    async def resources(self) -> JsonObject:
        """Read available capability metadata without exposing credential values."""
        snapshot = await refresh_capabilities(self.workspace, self.manifest, self.sandbox, self.sandbox_policy)
        values: JsonObject = {
            "models": [{"code": role, "name": model.model, "available": True} for role, model in self.models.items()],
            "tools": [snapshot.tools.resolve(code).model_dump(mode="json") for code in snapshot.tools.codes()],
            "skills": [descriptor.model_dump(mode="json") for descriptor, package in snapshot.skills.bindings.values()],
            "environments": [
                {"code": "workspace", "name": "Local workspace", "host_resource_id": "workspace", "available": True}
            ],
        }
        credentials = tuple(secret for model in self.models.values() for secret in model_credentials(model))
        redacted = redact_value(values, secrets=credentials)
        if not isinstance(redacted, dict):
            raise TypeError("Resource catalog must serialize to an object")
        return redacted

    async def resource_detail(self, kind: ResourceKind, code: str) -> ResourceDetail:
        """Read one complete resource contract with credentials masked at the host boundary."""
        credentials = tuple(secret for model in self.models.values() for secret in model_credentials(model))
        if kind == "models":
            model = self.models.get(code)
            if model is None:
                raise LookupError("The requested model is unavailable")
            return model_detail(code, model, secrets=credentials)
        if kind == "environments":
            if code != "workspace":
                raise LookupError("The requested environment is unavailable")
            return environment_detail(self.workspace, self.sandbox, self.sandbox_policy, secrets=credentials)
        if kind == "skills":
            snapshot = await asyncio.to_thread(
                load_capabilities, self.workspace, self.manifest, self.sandbox, self.sandbox_policy
            )
            if not snapshot.skills.contains(code):
                raise LookupError("The requested Skill is unavailable")
            return await asyncio.to_thread(skill_detail, snapshot.skills, code, secrets=credentials)
        snapshot = await refresh_capabilities(self.workspace, self.manifest, self.sandbox, self.sandbox_policy)
        if not snapshot.tools.contains(code):
            raise LookupError("The requested Tool is unavailable")
        tool = snapshot.tools.load(code)
        configuration = await asyncio.to_thread(load_mcp_configuration, self.workspace)
        connection = next((item for item in configuration.servers if code.startswith(f"mcp.{item.code}.")), None)
        return tool_detail(tool, self.approval_tool_codes, connection, secrets=credentials)

    async def validate_space(self, space: Space) -> None:
        """Resolve every resource reference without expanding host authorization."""
        if space.model_role not in self.models:
            raise ValueError(f"Model role is unavailable: {space.model_role}")
        if any(value != "workspace" for value in space.resource_bindings):
            raise ValueError("The Space requests a resource outside the host allowlist")
        snapshot = await refresh_capabilities(self.workspace, self.manifest, self.sandbox, self.sandbox_policy)
        for code in space.tool_codes:
            snapshot.tools.resolve(code)
        for code in space.skill_codes:
            snapshot.skills.resolve(code)

    async def validate_configuration(self, configuration: SpaceConfiguration) -> None:
        """Validate host resource references before committing a Space mutation."""
        await self.validate_space(Space(space_id="configuration-validation", **configuration.model_dump()))

    async def router(self, attempt: RouteAttempt, event_sink: EventSink) -> RouterBinding:
        """Build a short, read-only router with host-owned input and revision context."""
        snapshot = await asyncio.to_thread(self.store.snapshot, limit=MODEL_PAGE_ITEMS)
        versions = {f"space:{value.space_id}": value.revision for value in snapshot.spaces.items}
        versions.update({f"execution:{value.execution_id}": value.revision for value in snapshot.executions.items})
        versions.update({f"attention:{value.attention_id}": value.revision for value in snapshot.attentions.items})

        def read_inputs() -> tuple[InboxItem, ...]:
            with self.store.transaction() as connection:
                return self.store.routing_inputs(connection, attempt.source_input_ids)

        inputs = await asyncio.to_thread(read_inputs)
        fixed_ids = set(attempt.source_input_ids)
        ordered_inputs = (
            *tuple(value for value in inputs if value.input_id in fixed_ids),
            *tuple(value for value in inputs if value.input_id not in fixed_ids),
        )
        origins = [input_projection(item) for item in ordered_inputs[: MODEL_PAGE_ITEMS * 2]]
        for item in ordered_inputs[: MODEL_PAGE_ITEMS * 2]:
            versions[f"input:{item.input_id}"] = item.routing_revision
        registry = ToolRegistry()

        async def query(context: ToolContext, arguments: JsonObject) -> JsonObject:
            selected = QueryArguments.model_validate(arguments)
            if selected.kind == "resources":
                return await self.resources()
            if selected.kind == "inputs":
                current_inputs = await asyncio.to_thread(read_inputs)
                end = selected.cursor + selected.limit
                return {
                    "items": [input_projection(item) for item in current_inputs[selected.cursor : end]],
                    "total": len(current_inputs),
                    "next_cursor": end if end < len(current_inputs) else None,
                }
            if selected.kind == "chat":
                page = await asyncio.to_thread(
                    self.store.messages, before_sequence=selected.before_sequence, limit=selected.limit
                )
                return page_projection(page, "message", tool="query_product")
            if selected.kind == "spaces":
                spaces = await asyncio.to_thread(self.store.list_spaces, cursor=selected.cursor, limit=selected.limit)
                for space in spaces.items:
                    versions[f"space:{space.space_id}"] = space.revision
                return page_projection(spaces, "space", tool="query_product")
            if selected.kind == "executions":
                executions = await asyncio.to_thread(
                    self.store.list_executions, cursor=selected.cursor, limit=selected.limit
                )
                for execution in executions.items:
                    versions[f"execution:{execution.execution_id}"] = execution.revision
                return page_projection(executions, "execution", tool="query_product")
            if selected.kind == "attentions":
                attentions = await asyncio.to_thread(
                    self.store.attentions, cursor=selected.cursor, limit=selected.limit
                )
                for attention in attentions.items:
                    versions[f"attention:{attention.attention_id}"] = attention.revision
                return page_projection(attentions, "attention", tool="query_product")
            if selected.kind == "snapshot":
                current = await asyncio.to_thread(self.store.snapshot, limit=selected.limit)
                for space in current.spaces.items:
                    versions[f"space:{space.space_id}"] = space.revision
                for execution in current.executions.items:
                    versions[f"execution:{execution.execution_id}"] = execution.revision
                for attention in current.attentions.items:
                    versions[f"attention:{attention.attention_id}"] = attention.revision
                return snapshot_projection(current)
            if selected.id is None:
                raise ToolError("weave.source_identity_required", "An object query requires its exact identity.")
            source_id = selected.id
            if selected.kind == "input":
                item = await asyncio.to_thread(self.store.get_input, source_id)
                if item is None:
                    raise ToolError("weave.source_not_found", "The requested product input does not exist.")
                return {
                    "input": input_projection(item),
                    **source_page(
                        "input",
                        item.input_id,
                        item.content,
                        start_char=selected.start_char,
                        max_chars=selected.max_chars,
                        expected_digest=selected.expected_digest,
                        tool="query_product",
                    ),
                }
            if selected.kind in {"message", "report"}:

                def read_source() -> JsonObject:
                    with self.store.transaction() as connection:
                        if selected.kind == "message":
                            row = connection.execute(
                                "SELECT payload FROM chat_messages WHERE id=?", (selected.id,)
                            ).fetchone()
                            if row is None:
                                raise ToolError("weave.source_not_found", "The requested chat message does not exist.")
                            message = Message.model_validate_json(row["payload"])
                            return source_query(self.store.project_message(connection, message), selected)
                        report = self.store.load(connection, "reports", source_id, Report)
                        if report is None:
                            raise ToolError("weave.source_not_found", "The requested report does not exist.")
                        return source_query(report, selected)

                return await asyncio.to_thread(read_source)
            if selected.kind == "artifact":
                artifact = await asyncio.to_thread(self.store.get_artifact, selected.id)
                if artifact is None:
                    raise ToolError("weave.source_not_found", "The requested artifact does not exist.")
                return source_query(
                    ArtifactMetadata.model_validate(artifact.model_dump(exclude={"relative_path"})), selected
                )
            record: Space | Execution | Attention | None
            if selected.kind == "space":
                record = await asyncio.to_thread(self.store.get_space, selected.id)
            elif selected.kind == "execution":
                record = await asyncio.to_thread(self.store.get_execution, selected.id)
            else:
                record = await asyncio.to_thread(self.store.get_attention, selected.id)
            if record is None:
                raise ToolError("weave.source_not_found", "The requested product source does not exist.")
            versions[f"{selected.kind}:{selected.id}"] = record.revision
            return source_query(record, selected)

        query_tool = FunctionTool(
            ToolDescriptor(
                name="query_product",
                code="weave.query_product",
                description="只读查询空间、工作、问题、报告与宿主能力目录。",
            ),
            query,
            QueryArguments,
        )
        registry.register(query_tool.descriptor, bind_tool(query_tool))
        ready = asyncio.Event()

        async def publish(event: Event) -> None:
            await ready.wait()
            await event_sink(event)

        runner = Runner(
            tools=registry,
            skills=SkillLoader(),
            tool_runtime=ToolRuntime(),
            event_sink=publish,
            observability=self.observability,
            host_policy=HostPolicy(timeout_seconds=120, max_model_steps=8),
        )
        agent = Agent(
            name="router",
            model=self.models["text"],
            tools=(ToolRef(query_tool.descriptor.code),),
            prompt=(
                "You coordinate the permanent main chat. Query real product facts and return a routing decision. "
                "Use no mutation tools. Pure communication and status queries create no Execution. "
                "Dispatch each independent objective as a root job in a suitable Space. "
                "For an ambiguous request, use request_clarification with its exact source_input_ids and a concrete "
                "question. Wait for the saved answer before dispatching that request. A resolved global question "
                "includes its original inputs as context. Action source_input_ids must use only the fixed batch "
                "IDs from the current SDK input, and the host carries their original responsibilities forward. "
                "Detailed planning and implementation belong to Workers. Never grant permissions through text. "
                "Deliver saved report facts. Create Space definitions using available host resources. "
                "Input IDs and read versions are assigned by the host. Return reply and actions only."
                " Large sources carry exact references. Use query_product kind=input or the referenced source kind "
                "with id/start_char/max_chars/expected_digest to read full text. Previews are partial context. "
                "Use kind=inputs with cursor to inspect additional causal inputs."
            ),
            output_schema=RouteDraft.model_json_schema(),
        )
        context: tuple[ContextBlock, ...] = (
            ContextBlock(
                kind="routing",
                origin=f"decision:{attempt.decision_id}",
                content=json.dumps(
                    {
                        "inputs": origins,
                        "source_input_ids": list(attempt.source_input_ids),
                        "total_inputs": len(inputs),
                        "more_inputs": {"kind": "inputs", "cursor": 0, "limit": MODEL_PAGE_ITEMS},
                        "snapshot": snapshot_projection(snapshot),
                    },
                    ensure_ascii=False,
                ),
            ),
        )
        return RouterBinding(agent, runner, context, versions, ready)

    async def worker(
        self,
        execution: Execution,
        event_sink: EventSink,
        delegate: Callable[[ExecutionSpec, str], Awaitable[Execution]],
        progress: Callable[[str, str, str], Awaitable[None]],
    ) -> WorkerBinding:
        """Bind one immutable Worker snapshot and its separately owned execution resources."""
        await self.validate_space(execution.config_snapshot)
        scratch = self.workspace / "work" / execution.space_id / execution.execution_id
        snapshot = await refresh_capabilities(
            self.workspace, self.manifest, self.sandbox, self.sandbox_policy, work_root=scratch
        )
        snapshot_tools = snapshot.tools
        ready = asyncio.Event()

        async def publish(event: Event) -> None:
            await ready.wait()
            await event_sink(event)

        host = HostPolicy(
            timeout_seconds=1800,
            max_model_steps=64,
            max_output_tokens=100_000,
            grants=CapabilityGrants(capabilities=frozenset({"file.read", "file.write", "sandbox.command"})),
        )
        runner = Runner(
            tools=snapshot_tools,
            skills=snapshot.skills,
            tool_runtime=ToolRuntime(),
            event_sink=publish,
            host_policy=host,
            observability=self.observability,
        )
        context: tuple[ContextBlock, ...] = (
            ContextBlock(
                kind="objective",
                origin=f"execution:{execution.execution_id}",
                content=execution.objective
                if len(execution.objective.encode("utf-8")) <= MODEL_INLINE_BYTES
                else source_reference(
                    "objective", execution.execution_id, execution.objective, tool="query_work"
                ).model_dump_json(),
            ),
            ContextBlock(
                kind="worker_definition",
                origin=f"space:{execution.space_id}:{execution.config_snapshot.revision}",
                content=json.dumps(
                    record_projection(execution.config_snapshot, "space", execution.space_id, tool="query_work"),
                    ensure_ascii=False,
                ),
            ),
        )
        recalled: tuple[MemoryFact, ...] = ()
        if self.facts is not None:
            recalled = await asyncio.to_thread(
                self.facts.recall, execution.space_id, execution.objective, self.memory_recall_max_results
            )
            context += tuple(
                ContextBlock(
                    kind="product_fact",
                    origin=f"fact:{fact.memory_id}:{fact.revision}",
                    content=fact.content
                    if len(fact.content.encode("utf-8")) <= MODEL_INLINE_BYTES
                    else source_reference("fact", fact.memory_id, fact.content, tool="query_work").model_dump_json(),
                )
                for fact in recalled
            )
        graph = GraphControls(
            execution.execution_id,
            self.details,
            self.content,
            self.models,
            runner,
            self.sessions,
            context_blocks=context,
        )

        async def question(context: ToolContext, arguments: JsonObject) -> ToolOutput:
            selected = QuestionArguments.model_validate(arguments)
            return ToolInterruption(
                call_id=context.call_id,
                tool_code="weave.ask_user",
                tool_name="ask_user",
                arguments=arguments,
                reason=selected.question,
                details={"kind": "weave.clarification", "options": list(selected.options)},
            )

        async def report(context: ToolContext, arguments: JsonObject) -> JsonObject:
            if context.run_id is None:
                raise ValueError("Progress requires its owning SDK Run")
            await progress(execution.execution_id, context.run_id, ProgressArguments.model_validate(arguments).summary)
            return {"recorded": True}

        async def return_input(context: ToolContext, arguments: JsonObject) -> JsonObject:
            selected = ReturnArguments.model_validate(arguments)
            current = await asyncio.to_thread(self.store.get_execution, execution.execution_id)
            if context.run_id is None or current is None or current.current_run_id != context.run_id:
                raise ValueError("The input return does not belong to the current Worker")
            journal = await self.sessions.snapshot(execution.session_id)
            calls = {value.call_id: value for value in journal.tool_calls if value.run_id == context.run_id}
            pending = {
                value.call_id
                for value in calls.values()
                if value.state in {"prepared", "running", "unknown"} and value.call_id != context.call_id
            }
            for step in journal.steps:
                if step.step_id == context.step_id:
                    for message in step.messages:
                        if isinstance(message, AssistantMessage):
                            pending.update(
                                call.id
                                for call in message.tool_calls
                                if call.id != context.call_id
                                and (
                                    call.id not in calls
                                    or calls[call.id].state not in {"succeeded", "failed", "cancelled"}
                                )
                            )
            actions = tuple(
                ActionRef(
                    run_id=context.run_id,
                    call_id=identity,
                    operation_id=calls[identity].operation_id if identity in calls else None,
                )
                for identity in sorted(pending)
            )
            if selected.unknown_effects and not actions:
                raise ToolError(
                    "weave.handback_action_required", "Unknown effects require an unsettled SDK action identity."
                )
            value = await asyncio.to_thread(
                self.store.return_to_router,
                selected.input_id,
                execution_id=execution.execution_id,
                expected_revision=selected.expected_revision,
                reason=selected.reason,
                already_handled_refs=selected.already_handled_refs,
                effect_refs=selected.effect_refs,
                unknown_effects=bool(actions),
                action_refs=actions,
            )
            return value.model_dump(mode="json")

        async def delegate_work(context: ToolContext, arguments: JsonObject) -> ToolOutput:
            if context.run_id is None:
                raise ToolError("weave.run_required", "Independent delegation requires its owning SDK Run")
            selected = DelegateArguments.model_validate(arguments)
            child = await delegate(
                ExecutionSpec(
                    space_id=selected.space_id,
                    objective=selected.objective,
                    budget_limit=selected.budget_limit,
                    parent_execution_id=execution.execution_id,
                ),
                f"delegate:{context.run_id}:{context.call_id}",
            )
            return ToolInterruption(
                call_id=context.call_id,
                tool_code="weave.delegate_execution",
                tool_name="delegate_execution",
                arguments=arguments,
                reason="Waiting for the independently owned child work.",
                details={"kind": "weave.wait_children", "child_execution_id": child.execution_id},
            )

        execution_content = ExecutionContent(execution.execution_id, self.store, self.content, scratch)

        async def query_work(context: ToolContext, arguments: JsonObject) -> JsonObject:
            selected = WorkQueryArguments.model_validate(arguments)

            def read() -> JsonObject:
                with self.store.transaction() as connection:
                    current = self.store.require(connection, "executions", execution.execution_id, Execution)
                    responsibilities = tuple(
                        value
                        for value in self.store.records(connection, "input_dispositions", InputDisposition)
                        if value.target_execution_id == execution.execution_id
                    )
                    messages = execution_content.source_messages(connection)
                    observations = tuple(
                        value
                        for value in self.store.records(connection, "tool_results", ToolResultDetail)
                        if value.execution_id == execution.execution_id
                    )
                    inputs = tuple(
                        value
                        for value in self.store.records(connection, "inbox_items", InboxItem)
                        if value.target_kind == "execution" and value.target_id == execution.execution_id
                    )
                    if selected.kind != "overview":
                        if selected.id is None:
                            raise ToolError(
                                "weave.source_identity_required", "A source page requires its exact identity."
                            )
                        content: str | None = None
                        if selected.kind == "input":
                            item = next((value for value in inputs if value.input_id == selected.id), None)
                            content = None if item is None else item.content
                        elif selected.kind == "message":
                            message = next((value for value in messages if value.message_id == selected.id), None)
                            if message is not None:
                                content = self.store.project_message(connection, message).model_dump_json()
                        elif selected.kind == "execution" and selected.id == execution.execution_id:
                            content = current.model_dump_json()
                        elif selected.kind == "space" and selected.id == execution.space_id:
                            content = execution.config_snapshot.model_dump_json()
                        elif selected.kind == "objective" and selected.id == execution.execution_id:
                            content = execution.objective
                        elif selected.kind == "disposition":
                            disposition = next(
                                (
                                    value
                                    for value in responsibilities
                                    if stable_id(f"disposition/{value.origin_input_id}/{value.target_execution_id}")
                                    == selected.id
                                ),
                                None,
                            )
                            content = None if disposition is None else disposition.model_dump_json()
                        elif selected.kind == "fact":
                            fact = next((value for value in recalled if value.memory_id == selected.id), None)
                            content = None if fact is None else fact.content
                        if content is None:
                            raise ToolError(
                                "weave.source_not_found", "The source is outside this Execution's accepted scope."
                            )
                        return source_page(
                            selected.kind,
                            selected.id,
                            content,
                            start_char=selected.start_char,
                            max_chars=selected.max_chars,
                            expected_digest=selected.expected_digest,
                            tool="query_work",
                        )
                    end = selected.cursor + selected.limit
                    return {
                        "execution": record_projection(current, "execution", current.execution_id, tool="query_work"),
                        "responsibilities": [
                            record_projection(
                                value,
                                "disposition",
                                stable_id(f"disposition/{value.origin_input_id}/{value.target_execution_id}"),
                                tool="query_work",
                            )
                            for value in responsibilities[selected.cursor : end]
                        ],
                        "messages": [
                            record_projection(
                                self.store.project_message(connection, value),
                                "message",
                                value.message_id,
                                tool="query_work",
                            )
                            for value in messages[selected.cursor : end]
                        ],
                        "inputs": [input_projection(value) for value in inputs[selected.cursor : end]],
                        "tool_results": [
                            value.model_dump(mode="json", exclude={"text"})
                            for value in observations[selected.cursor : end]
                        ],
                        "next_cursor": end
                        if end < max(len(messages), len(responsibilities), len(observations), len(inputs))
                        else None,
                        "total_inputs": len(inputs),
                        "total_messages": len(messages),
                        "total_responsibilities": len(responsibilities),
                    }

            return await asyncio.to_thread(read)

        async def search(context: ToolContext, arguments: JsonObject) -> JsonObject:
            selected = SearchArguments.model_validate(arguments)
            index = BM25Index(descriptor for descriptor, package in snapshot.skills.bindings.values())
            return {
                "tools": [
                    item.model_dump(mode="json") for item in snapshot_tools.search(selected.query, selected.limit)
                ],
                "skills": [item.model_dump(mode="json") for item in index.search(selected.query, selected.limit)],
            }

        resource_tools: tuple[BaseTool, ...] = (
            (WorkspaceResource(self.workspace, scratch).tool(),)
            if "workspace" in execution.config_snapshot.resource_bindings
            else ()
        )
        own_tools = (
            *graph.tools(),
            *resource_tools,
            execution_content.tool(),
            FunctionTool(
                ToolDescriptor(
                    name="query_work",
                    code="weave.query_work",
                    description="Read this work's current inputs and responsibility revisions.",
                ),
                query_work,
                WorkQueryArguments,
            ),
            FunctionTool(
                ToolDescriptor(
                    name="search_capabilities",
                    code="weave.search_capabilities",
                    description="Search tools and Skills in the current host-authorized snapshot.",
                ),
                search,
                SearchArguments,
            ),
            FunctionTool(
                ToolDescriptor(
                    name="ask_user", code="weave.ask_user", description="向用户提出具体澄清问题并保存可恢复等待。"
                ),
                question,
                QuestionArguments,
            ),
            FunctionTool(
                ToolDescriptor(
                    name="report_progress",
                    code="weave.report_progress",
                    description="Record progress in execution details without waking the Router.",
                ),
                report,
                ProgressArguments,
            ),
            FunctionTool(
                ToolDescriptor(
                    name="return_to_router",
                    code="weave.return_to_router",
                    description="将错投或超出目标的输入连同已处理事实交回 Router。",
                ),
                return_input,
                ReturnArguments,
            ),
            FunctionTool(
                ToolDescriptor(
                    name="delegate_execution",
                    code="weave.delegate_execution",
                    description="Delegate independent child work using the actual parent budget.",
                ),
                delegate_work,
                DelegateArguments,
            ),
        )
        selected_codes = tuple(dict.fromkeys((*DEFAULT_TOOL_CODES, *execution.config_snapshot.tool_codes)))
        subagent = Subagent(
            "specialist",
            Agent(
                name="specialist",
                model=self.models[execution.config_snapshot.model_role],
                prompt="Complete the delegated bounded task. Preserve evidence and report the concrete result.",
                tools=tuple(ToolRef(code) for code in selected_codes),
            ),
            runner,
            session_store=self.sessions,
            limits=RunLimits(execution_timeout_seconds=600, max_model_steps=16, max_output_tokens=16_384),
        )
        group = SubagentGroup((subagent,), budgets=graph.executor.budgets)
        delegated = (
            as_tool(
                subagent,
                name="consult_specialist",
                description="Delegate bounded work to a scoped specialist.",
                group=group,
            ),
            submit_subagent(group),
            wait_subagents(group),
        )
        for tool in (*own_tools, *delegated):
            snapshot_tools.register(tool.descriptor, bind_tool(tool))
        runner.tools = with_approvals(snapshot_tools, self.approval_tool_codes)
        agent = Agent(
            name=f"worker:{execution.execution_id}",
            model=self.models[execution.config_snapshot.model_role],
            prompt=(
                "Complete this Execution's objective using its Space definition. Plan, execute and verify the work. "
                "Use graphs or scoped subagents when needed. Independent work delegated here creates child Executions. "
                "Accept supplements within this objective. Return wrongly assigned inputs through return_to_router. "
                "Use ask_user for missing required information. Progress belongs in report_progress. "
                "Your workspace and scratch roots contain only this Execution's files. "
                "Use execution_content to materialize assigned attachments and publish owned artifacts. "
                "Read shared host data only through an explicitly bound workspace_resource. "
                "Finish with kind ready for completed delivery or failed when the objective cannot be completed, "
                "the summary, artifacts, verification evidence, and consumed input_ids you answered. "
                "Use exact input_id values from the input scope or query_work responsibilities. "
                "A message_id is a different identity. "
                "Verification evidence_refs contain exact artifact_id, node_attempt_id, or tool_result_id values. "
                "The host supplies verification timestamps from evidence. "
                "Required checks have concrete tool arguments and expected result fields or a delivered artifact name. "
                "Execute those checks exactly. The host evaluates required checks independently of your claims. "
                "Use an empty verification array for no checks. "
                "Reading an input alone does not count as answering it. Never claim unknown actions succeeded."
                " Large source references retain exact IDs and hashes. Read their full contents with query_work "
                "using kind/id/start_char/max_chars/expected_digest before relying on their partial previews."
            ),
            tools=tuple(
                ToolRef(code)
                for code in (*selected_codes, *(tool.descriptor.code for tool in (*own_tools, *delegated)))
            ),
            skills=tuple(SkillRef(code) for code in execution.config_snapshot.skill_codes),
            output_schema=CompletionDraft.model_json_schema(),
        )
        return WorkerBinding(agent, runner, group, context, graph, ready)

    async def aclose(self) -> None:
        """Close model transports from their single product owner."""
        async with asyncio.TaskGroup() as group:
            for model in self.models.values():
                group.create_task(model.aclose())


def source_query(record: ContractModel, selected: QueryArguments) -> JsonObject:
    """Read one product record inline or through a bounded, digest-checked JSON page."""
    if selected.id is None:
        raise ToolError("weave.source_identity_required", "A source query requires its exact identity.")
    content = record.model_dump_json()
    if (
        selected.expected_digest is None
        and selected.start_char == 0
        and len(content.encode("utf-8")) <= MODEL_INLINE_BYTES
    ):
        return record.model_dump(mode="json")
    return source_page(
        selected.kind,
        selected.id,
        content,
        start_char=selected.start_char,
        max_chars=selected.max_chars,
        expected_digest=selected.expected_digest,
        tool="query_product",
    )


def bind_tool(tool: BaseTool) -> ToolFactory:
    """Retain an already authorized task-local Tool binding."""

    def create(descriptor: ToolDescriptor) -> BaseTool:
        return tool

    return create
