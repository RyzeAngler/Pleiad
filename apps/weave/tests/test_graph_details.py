import asyncio
from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pleiad.contracts import ContractModel, JsonObject
from pleiad.models.binding import binding_fingerprint
from pleiad.models.openai.client import OpenAIModelClient
from pleiad.models.openai.request import OpenAIRequest
from pleiad.models.openai.response import (
    OpenAIStreamChoice,
    OpenAIStreamChunk,
    OpenAIStreamDelta,
    OpenAIStreamFunction,
    OpenAIStreamToolCall,
    OpenAIUsage,
)
from pleiad.orchestration.graph.edge import Edge, EdgeCondition
from pleiad.orchestration.graph.state import GraphStatus, NodeResume, NodeStatus
from pleiad.runtime.checkpoint import ResumeDecision
from pleiad.runtime.limits import RunLimits
from pleiad.runtime.runner import Runner
from pleiad.sessions.backends.sqlite import SqliteSessionStore
from pleiad.skills.loader import SkillLoader
from pleiad.tools.base import FunctionTool, ToolOutput
from pleiad.tools.contracts import ParentRunLimits, ToolContext, ToolDescriptor, ToolResult
from pleiad.tools.interruption import InterruptionResponse, ToolInterruption
from pleiad.tools.registry import ToolRegistry
from pleiad.tools.runtime import ToolRuntime

from pleiad_weave.agent.contracts import GraphSpec, NodeSpec
from pleiad_weave.agent.graph_controls import GraphControls
from pleiad_weave.capabilities.approval import RequiresApprovalTool
from pleiad_weave.infrastructure.storage.product import JSON_OBJECT, ProductStore
from pleiad_weave.infrastructure.storage.product_content import ProductContent
from pleiad_weave.infrastructure.storage.product_details import ProductDetails, ToolResultDetail
from pleiad_weave.infrastructure.storage.product_models import (
    ChatMessageRequest,
    ContextReference,
    Execution,
    ExecutionSpec,
    ProductConflict,
    SpaceConfiguration,
)
from tests.test_super_agent_application import ProductScenarioModel, product_service


class EmptyArguments(ContractModel):
    pass


class NodeModel(OpenAIModelClient):
    def __init__(self) -> None:
        self.provider = "fake"
        self.image_input = "unverified"
        self.model = "test-model"
        self.binding_fingerprint = binding_fingerprint(
            protocol="openai", provider=self.provider, model=self.model, base_url=None, provider_options={}
        )
        self.requests: list[str] = []

    async def stream(self, request: OpenAIRequest | Mapping[str, object]) -> AsyncIterator[OpenAIStreamChunk]:
        assert isinstance(request, OpenAIRequest)
        source = "needs approval" in str(request.messages[0].content)
        resumed = any(value.role == "tool" for value in request.messages)
        self.requests.append("source-resumed" if source and resumed else "source" if source else "sibling")
        if source and not resumed:
            delta = OpenAIStreamDelta(
                tool_calls=(
                    OpenAIStreamToolCall(
                        index=0, id="approval-call", function=OpenAIStreamFunction(name="approve", arguments="{}")
                    ),
                )
            )
            finish = "tool_calls"
        else:
            delta = OpenAIStreamDelta(content='{"value":42}' if source else '{"confirmed":true}')
            finish = "stop"
        yield OpenAIStreamChunk(model=self.model, choices=(OpenAIStreamChoice(delta=delta),))
        yield OpenAIStreamChunk(
            model=self.model,
            choices=(OpenAIStreamChoice(delta=OpenAIStreamDelta(), finish_reason=finish),),
            usage=OpenAIUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        )


async def unused_tool(context: ToolContext, arguments: JsonObject) -> JsonObject:
    return {"value": "unused"}


async def pending_approval(context: ToolContext, arguments: JsonObject) -> ToolOutput:
    return ToolInterruption(
        interruption_id="node-approval",
        call_id=context.call_id,
        tool_code="approve",
        tool_name="approve",
        arguments=arguments,
        reason="Approve the external action.",
        details={"kind": "weave.approval"},
    )


def node_runner(model: NodeModel) -> Runner:
    registry = ToolRegistry()
    for code in ("read_file", "write_file", "command", "approve"):
        descriptor = ToolDescriptor(name=code, code=code, description=f"Test boundary {code}")

        def factory(selected: ToolDescriptor) -> FunctionTool:
            if selected.code == "approve":
                return FunctionTool(selected, pending_approval, EmptyArguments)
            return FunctionTool(selected, unused_tool, EmptyArguments)

        registry.register(descriptor, factory)
    return Runner(tools=registry, skills=SkillLoader(), tool_runtime=ToolRuntime())


def product(tmp_path: Path) -> tuple[ProductStore, Execution]:
    store = ProductStore(tmp_path / "runtime")
    space = store.create_space(SpaceConfiguration(name="Worker"), request_id="space")
    execution = store.create_execution(ExecutionSpec(space_id=space.space_id, objective="Task"), request_id="task")
    return store, execution


def test_owned_graph_pause_restores_original_attempt_and_does_not_repeat_completed_sibling(tmp_path: Path) -> None:
    store, execution = product(tmp_path)
    details = ProductDetails(store)
    content = ProductContent(store)
    model = NodeModel()
    sessions_path = tmp_path / "sessions.sqlite3"
    context = ToolContext(
        call_id="graph-call",
        run_id="parent",
        parent_limits=ParentRunLimits(
            deadline=datetime.now(UTC) + timedelta(minutes=2),
            remaining_model_steps=10,
            remaining_output_tokens=10_000,
            tool_concurrency=2,
        ),
    )
    spec = GraphSpec(
        graph_id="graph",
        nodes=(
            NodeSpec(
                node_id="source",
                objective="needs approval",
                tool_codes=("approve",),
                limits=RunLimits(max_model_steps=3, max_output_tokens=20),
            ),
            NodeSpec(
                node_id="sibling",
                objective="confirmed sibling",
                limits=RunLimits(max_model_steps=1, max_output_tokens=10),
            ),
        ),
        max_concurrency=2,
    )

    async def scenario() -> None:
        controls = GraphControls(
            execution.execution_id,
            details,
            content,
            {"text": model},
            node_runner(model),
            SqliteSessionStore(sessions_path),
        )
        submitted = await controls.submit(context, {"graph": spec.model_dump(mode="json")})
        assert submitted["accepted"] is True
        paused = await controls.execute(context, {"graph_id": "graph", "input": {}})
        assert isinstance(paused, ToolInterruption)
        graph_run_id = paused.details["graph_run_id"]
        assert isinstance(graph_run_id, str)
        saved = details.graph_run(execution.execution_id, graph_run_id)
        assert saved.status is GraphStatus.PAUSED and saved.state is not None
        attempts = {
            node: details.node_attempts(execution.execution_id, graph_run_id, node).items
            for node in ("source", "sibling")
        }
        assert attempts["source"][0].status is NodeStatus.PAUSED
        assert attempts["sibling"][0].status is NodeStatus.SUCCEEDED
        assert attempts["sibling"][0].artifact_ids
        assert len(model.requests) == 2
        retry = await controls.execute(context, {"graph_id": "graph", "input": {}})
        assert isinstance(retry, ToolInterruption) and retry.details == paused.details
        assert len(model.requests) == 2
        newer = spec.model_copy(update={"revision": 2, "objective": "A separate new graph definition"})
        await controls.submit(context, {"graph": newer.model_dump(mode="json"), "expected_revision": 1})
        pinned_retry = await controls.execute(context, {"graph_id": "graph", "input": {}})
        assert isinstance(pinned_retry, ToolInterruption) and pinned_retry.details == paused.details
        checkpoint = attempts["source"][0].checkpoint
        assert checkpoint is not None
        interruption = checkpoint.interruptions[0]
        now = datetime.now(UTC)
        decision = ResumeDecision(
            interruption_id=interruption.interruption_id,
            result=ToolResult(
                call_id=interruption.call_id, ok=True, output={"approved": True}, started_at=now, finished_at=now
            ),
        )
        restored = GraphControls(
            execution.execution_id,
            ProductDetails(ProductStore(store.root)),
            ProductContent(store),
            {"text": model},
            node_runner(model),
            SqliteSessionStore(sessions_path),
        )
        completed = await restored.resume(
            graph_run_id, (NodeResume(node_id="source", decisions=(decision,)),), context, paused.arguments
        )
        assert isinstance(completed, dict) and completed["status"] == "succeeded"
        source = details.node_attempts(execution.execution_id, graph_run_id, "source").items
        assert len(source) == 1 and source[0].node_attempt_id == attempts["source"][0].node_attempt_id
        assert source[0].sdk_run_id == checkpoint.run_id and source[0].session_id == checkpoint.session_id
        assert model.requests.count("sibling") == 1 and model.requests.count("source-resumed") == 1
        assert source[0].artifact_ids
        assert content.read_artifact(source[0].artifact_ids[0]) == b'{"value": 42}'
        assert details.graph_runs(execution.execution_id).total == 1
        assert details.graph_run(execution.execution_id, graph_run_id).graph_revision == 1

    asyncio.run(scenario())


def test_graph_revision_conflict_preserves_the_original_definition(tmp_path: Path) -> None:
    store, execution = product(tmp_path)
    details = ProductDetails(store)
    first = GraphSpec(graph_id="graph", nodes=(NodeSpec(node_id="node", objective="first"),))
    stored = details.submit(execution.execution_id, first, expected_revision=None)
    assert details.submit(execution.execution_id, first, expected_revision=None) == stored
    with pytest.raises(ProductConflict):
        details.submit(
            execution.execution_id,
            first.model_copy(update={"nodes": (NodeSpec(node_id="node", objective="changed"),)}),
            expected_revision=None,
        )
    assert details.definition(execution.execution_id, "graph").graph == first
    second = first.model_copy(update={"revision": 2})
    details.submit(execution.execution_id, second, expected_revision=1)
    assert details.definition(execution.execution_id, "graph", 1).graph == first
    assert details.definition(execution.execution_id, "graph").graph == second


def test_tool_observation_pages_enforce_owner_and_utf8_byte_boundaries(tmp_path: Path) -> None:
    store, execution = product(tmp_path)
    details = ProductDetails(store)
    text = "原始观察🙂" * 10_000
    details.save_tool_result(
        ToolResultDetail(
            tool_result_id="result",
            execution_id=execution.execution_id,
            run_id="run",
            step_id="step",
            call_id="call",
            text=text,
        )
    )
    chunks: list[str] = []
    offset = 0
    while True:
        page = details.tool_result_page(execution.execution_id, "result", offset=offset)
        assert len(page.text.encode("utf-8")) <= 8192
        chunks.append(page.text)
        if page.next_offset is None:
            break
        offset = page.next_offset
    assert "".join(chunks) == text
    with pytest.raises(ValueError):
        details.tool_result_page(execution.execution_id, "result", max_bytes=32769)
    with pytest.raises(ValueError):
        details.tool_result_page(execution.execution_id, "result", offset=1)
    other = store.create_execution(ExecutionSpec(space_id=execution.space_id, objective="Other"), request_id="other")
    with pytest.raises(LookupError):
        details.tool_result_page(other.execution_id, "result")


def test_cancelling_paused_graph_settles_original_node_sessions_and_preserves_completed_artifacts(
    tmp_path: Path,
) -> None:
    store, execution = product(tmp_path)
    details = ProductDetails(store)
    content = ProductContent(store)
    model = NodeModel()
    sessions = SqliteSessionStore(tmp_path / "sessions.sqlite3")
    context = ToolContext(
        call_id="graph-call",
        run_id="parent",
        parent_limits=ParentRunLimits(
            deadline=datetime.now(UTC) + timedelta(minutes=2),
            remaining_model_steps=10,
            remaining_output_tokens=10_000,
            tool_concurrency=2,
        ),
    )
    spec = GraphSpec(
        graph_id="graph",
        nodes=(
            NodeSpec(
                node_id="source",
                objective="needs approval",
                tool_codes=("approve",),
                limits=RunLimits(max_model_steps=3, max_output_tokens=20),
            ),
            NodeSpec(
                node_id="sibling",
                objective="confirmed sibling",
                limits=RunLimits(max_model_steps=1, max_output_tokens=10),
            ),
        ),
    )

    async def scenario() -> None:
        controls = GraphControls(
            execution.execution_id, details, content, {"text": model}, node_runner(model), sessions
        )
        await controls.submit(context, {"graph": spec.model_dump(mode="json")})
        pending = await controls.execute(context, {"graph_id": "graph", "input": {}})
        assert isinstance(pending, ToolInterruption)
        graph_run_id = pending.details["graph_run_id"]
        assert isinstance(graph_run_id, str)
        sibling = details.node_attempts(execution.execution_id, graph_run_id, "sibling").items[0]
        source = details.node_attempts(execution.execution_id, graph_run_id, "source").items[0]
        assert source.checkpoint is not None

        cancelled = await controls.cancel_pending(graph_run_id, "user cancelled")
        retried = await controls.cancel_pending(graph_run_id, "user cancelled")

        assert cancelled == retried and cancelled.status is GraphStatus.CANCELLED
        assert cancelled.state is not None and cancelled.state.budget is not None
        assert cancelled.state.budget.reservations == ()
        actual_source = details.node_attempt(execution.execution_id, source.node_attempt_id)
        assert actual_source.status is NodeStatus.CANCELLED and actual_source.checkpoint is None
        snapshot = await sessions.run(source.checkpoint.session_id, source.checkpoint.run_id)
        assert snapshot.receipt is not None and snapshot.receipt.state == "cancelled"
        assert details.node_attempt(execution.execution_id, sibling.node_attempt_id) == sibling
        assert content.read_artifact(sibling.artifact_ids[0]) == b'{"confirmed": true}'
        assert len(model.requests) == 2

    asyncio.run(scenario())


def test_conditional_graph_skip_has_no_fabricated_node_attempt(tmp_path: Path) -> None:
    store, execution = product(tmp_path)
    details = ProductDetails(store)
    model = NodeModel()
    sessions = SqliteSessionStore(tmp_path / "sessions.sqlite3")
    spec = GraphSpec(
        graph_id="conditional",
        nodes=(
            NodeSpec(node_id="source", objective="confirmed source"),
            NodeSpec(node_id="branch", objective="unreachable branch"),
        ),
        edges=(
            Edge(
                edge_id="only-false",
                source_node_id="source",
                target_node_id="branch",
                condition=EdgeCondition(source_path="/confirmed", operator="equals", value=False),
            ),
        ),
    )

    async def scenario() -> None:
        controls = GraphControls(
            execution.execution_id, details, ProductContent(store), {"text": model}, node_runner(model), sessions
        )
        context = ToolContext(call_id="call", run_id="parent")
        await controls.submit(context, {"graph": spec.model_dump(mode="json")})
        result = await controls.execute(context, {"graph_id": spec.graph_id})
        assert isinstance(result, dict) and result["status"] == "succeeded"
        saved = details.graph_runs(execution.execution_id).items[0]
        assert saved.state is not None
        branch = next(value for value in saved.state.node_states if value.node_id == "branch")
        assert branch.status is NodeStatus.SKIPPED
        assert details.node_attempts(execution.execution_id, saved.graph_run_id, "branch").items == ()
        assert len(model.requests) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("graph_id", ["shared-graph", "../../opaque-graph"])
def test_same_graph_identity_is_owned_by_each_execution_and_each_invocation(tmp_path: Path, graph_id: str) -> None:
    store, first = product(tmp_path)
    second = store.create_execution(ExecutionSpec(space_id=first.space_id, objective="Second"), request_id="second")
    details = ProductDetails(store)
    content = ProductContent(store)
    model = NodeModel()
    sessions = SqliteSessionStore(tmp_path / "sessions.sqlite3")
    spec = GraphSpec(graph_id=graph_id, nodes=(NodeSpec(node_id="node", objective="confirmed node"),))

    async def scenario() -> None:
        for execution in (first, second):
            controls = GraphControls(
                execution.execution_id, details, content, {"text": model}, node_runner(model), sessions
            )
            for call_id in ("first-call", "second-call"):
                context = ToolContext(call_id=call_id, run_id="same-parent-run")
                await controls.submit(context, {"graph": spec.model_dump(mode="json")})
                result = await controls.execute(context, {"graph_id": graph_id})
                assert isinstance(result, dict) and result["status"] == "succeeded"
        restored = ProductDetails(ProductStore(store.root))
        first_runs = restored.graph_runs(first.execution_id).items
        second_runs = restored.graph_runs(second.execution_id).items
        assert len(first_runs) == len(second_runs) == 2
        assert len({value.graph_run_id for value in (*first_runs, *second_runs)}) == 4
        for run in first_runs:
            attempt = restored.node_attempts(first.execution_id, run.graph_run_id, "node").items
            assert len(attempt) == 1 and attempt[0].execution_id == first.execution_id
            with pytest.raises(LookupError):
                restored.graph_run(second.execution_id, run.graph_run_id)
        assert len(model.requests) == 4

    asyncio.run(scenario())


def test_graph_node_invalid_model_output_is_persisted_as_failure(tmp_path: Path) -> None:
    store, execution = product(tmp_path)
    details = ProductDetails(store)
    model = NodeModel()
    spec = GraphSpec(
        graph_id="invalid-output",
        nodes=(
            NodeSpec(
                node_id="node",
                objective="confirmed node",
                output_schema={"type": "object", "required": ["value"], "properties": {"value": {"type": "integer"}}},
            ),
        ),
    )

    async def scenario() -> None:
        controls = GraphControls(
            execution.execution_id,
            details,
            ProductContent(store),
            {"text": model},
            node_runner(model),
            SqliteSessionStore(tmp_path / "sessions.sqlite3"),
        )
        context = ToolContext(call_id="call", run_id="parent")
        await controls.submit(context, {"graph": spec.model_dump(mode="json")})
        result = await controls.execute(context, {"graph_id": spec.graph_id})
        assert isinstance(result, dict) and result["status"] == "failed"
        run = details.graph_runs(execution.execution_id).items[0]
        assert run.status is GraphStatus.FAILED
        attempt = details.node_attempts(execution.execution_id, run.graph_run_id, "node").items[0]
        assert attempt.status is NodeStatus.FAILED and attempt.artifact_ids == ()
        assert attempt.error is not None
        assert attempt.error.code == "agent.output.invalid"

    asyncio.run(scenario())


def test_graph_host_approval_preserves_the_underlying_node_resume_protocol(tmp_path: Path) -> None:
    async def scenario() -> None:
        store, execution = product(tmp_path)
        model = NodeModel()
        details = ProductDetails(store)
        controls = GraphControls(
            execution.execution_id,
            details,
            ProductContent(store),
            {"text": model},
            node_runner(model),
            SqliteSessionStore(tmp_path / "sessions.sqlite3"),
        )
        context = ToolContext(
            call_id="graph-call",
            run_id="parent",
            parent_limits=ParentRunLimits(
                deadline=datetime.now(UTC) + timedelta(seconds=60),
                remaining_model_steps=20,
                remaining_output_tokens=20_000,
                tool_concurrency=2,
            ),
        )
        graph = GraphSpec(
            graph_id="approved-graph",
            nodes=(NodeSpec(node_id="source", objective="needs approval", tool_codes=("approve",)),),
        )
        await controls.submit(context, {"graph": graph.model_dump(mode="json")})
        tool = RequiresApprovalTool(controls.tools()[-1])
        initial = await tool.authorize(context, {"graph_id": graph.graph_id})
        assert initial.details["kind"] == "weave.approval"
        now = datetime.now(UTC)
        pending = await tool.execute(context, initial.arguments)
        assert isinstance(pending, ToolInterruption) and pending.details["kind"] == "weave.graph"
        record = details.graph_runs(execution.execution_id).items[0]
        assert record.state is not None
        checkpoint = record.state.node_states[0].checkpoint
        assert checkpoint is not None
        decision = ResumeDecision(
            interruption_id=checkpoint.interruptions[0].interruption_id,
            result=ToolResult(
                call_id="approval-call", ok=True, output={"approved": True}, started_at=now, finished_at=now
            ),
        )
        resumed = await tool.resume_interruption(
            context,
            pending,
            InterruptionResponse(
                interruption_id=pending.interruption_id,
                result=ToolResult(
                    call_id=context.call_id,
                    ok=True,
                    started_at=now,
                    finished_at=now,
                    output={
                        "node_resumes": [NodeResume(node_id="source", decisions=(decision,)).model_dump(mode="json")]
                    },
                ),
            ),
        )
        assert isinstance(resumed, ToolResult) and resumed.ok and resumed.output is not None
        assert resumed.output["status"] == "succeeded" and resumed.output["graph_run_id"] == record.graph_run_id
        assert len(details.node_attempts(execution.execution_id, record.graph_run_id).items) == 1
        assert model.requests == ["source", "source-resumed"]

    asyncio.run(scenario())


def test_cited_graph_node_artifacts_become_readable_sources_for_the_assigned_worker(tmp_path: Path) -> None:
    async def scenario() -> None:
        service = product_service(tmp_path, ProductScenarioModel())
        source_space = service.store.create_space(SpaceConfiguration(name="Source"), request_id="source-space")
        source = service.store.create_execution(
            ExecutionSpec(space_id=source_space.space_id, objective="Generate a result"), request_id="source"
        )
        model = NodeModel()
        controls = GraphControls(
            source.execution_id, service.details, service.content, {"text": model}, node_runner(model), service.sessions
        )
        graph = GraphSpec(graph_id="source-graph", nodes=(NodeSpec(node_id="result", objective="confirmed node"),))
        context = ToolContext(
            call_id="graph",
            run_id="source-run",
            parent_limits=ParentRunLimits(
                deadline=datetime.now(UTC) + timedelta(seconds=60),
                remaining_model_steps=10,
                remaining_output_tokens=10_000,
                tool_concurrency=2,
            ),
        )
        await controls.submit(context, {"graph": graph.model_dump(mode="json")})
        await controls.execute(context, {"graph_id": graph.graph_id})
        record = service.details.graph_runs(source.execution_id).items[0]
        node = service.details.node_attempts(source.execution_id, record.graph_run_id).items[0]
        target_space = service.store.create_space(SpaceConfiguration(name="Target"), request_id="target-space")
        target = service.store.create_execution(
            ExecutionSpec(space_id=target_space.space_id, objective="Use the cited node result"), request_id="target"
        )
        receipt = service.store.submit_message(
            ChatMessageRequest(
                client_message_id="cite",
                text="Use this node result",
                target_execution_ids=(target.execution_id,),
                context_refs=(
                    ContextReference(
                        kind="graph_node",
                        id=node.node_attempt_id,
                        execution_id=source.execution_id,
                        graph_run_id=record.graph_run_id,
                        node_id=node.node_id,
                        node_attempt_id=node.node_attempt_id,
                    ),
                ),
            )
        )
        binding = await service.agents.worker(target, service.ignore_event, service.delegate, service.progress)
        try:
            facts = await binding.runner.tools.load("weave.query_work").execute(ToolContext(call_id="facts"), {})
            assert isinstance(facts, dict) and isinstance(facts["messages"], list)
            assert any(
                isinstance(value, dict) and value["message_id"] == receipt.message_id for value in facts["messages"]
            )
            materialized = await binding.runner.tools.load("weave.execution_content").execute(
                ToolContext(call_id="source"), {"action": "materialize", "kind": "artifact", "id": node.artifact_ids[0]}
            )
            assert isinstance(materialized, dict) and isinstance(materialized["path"], str)
            path = service.agents.workspace / "work" / target.space_id / target.execution_id / materialized["path"]
            assert JSON_OBJECT.validate_json(path.read_bytes()) == {"confirmed": True}
        finally:
            await binding.runner.aclose()
            await service.agents.aclose()

    asyncio.run(scenario())
