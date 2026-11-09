import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from pleiad.agent.agent import Agent
from pleiad.budgets import BudgetLedger, BudgetStateError
from pleiad.context.selection.models import ContextBlock
from pleiad.contracts import CapabilityGrants, JsonObject, JsonSchema
from pleiad.events.graph import GraphEvent, GraphEventKind
from pleiad.events.runtime import RunFailedEvent
from pleiad.messages.message import UserMessage
from pleiad.orchestration.graph.edge import Edge, EdgeCondition, EdgeMapping
from pleiad.orchestration.graph.errors import GraphSystemFault
from pleiad.orchestration.graph.executor import GraphExecutor
from pleiad.orchestration.graph.graph import Graph, OutputBinding
from pleiad.orchestration.graph.node import Node
from pleiad.orchestration.graph.state import GraphResult, GraphStatus, NodeResult, NodeResume, NodeStatus
from pleiad.orchestration.handoff.handoff import Handoff
from pleiad.orchestration.subagents.group import SubagentGroup
from pleiad.orchestration.subagents.subagent import Subagent
from pleiad.runtime.checkpoint import ResumeDecision
from pleiad.runtime.limits import RunLimits
from pleiad.runtime.policy import HostPolicy
from pleiad.runtime.result import RunResult
from pleiad.sessions.backends.in_memory import InMemorySessionStore
from pleiad.sessions.checkpoint import RunCheckpoint
from pleiad.sessions.records import RunStatus
from pleiad.sessions.session import Session
from pleiad.tools.contracts import ParentRunLimits, ToolContext, ToolResult
from tests.fakes.runtime import (
    BlockingOpenAIModel,
    FakeAnthropicModel,
    FakeOpenAIModel,
    anthropic_text_batch,
    interrupting_tool,
    make_runner,
    text_batch,
)
from tests.orchestration.subagents.test_subagents import tool_batch
from tests.runtime_support import input_text


async def wait_for_event(event: asyncio.Event) -> None:
    """Wait for a test barrier without masking scheduler deadlocks."""
    await asyncio.wait_for(event.wait(), timeout=1)


def test_agent_node_references_reach_model_without_changing_input_contract_or_stored_step(tmp_path: Path) -> None:
    async def scenario() -> None:
        model = FakeOpenAIModel((text_batch("done", "final"),))
        runner, agent = make_runner(model, tmp_path)
        memory = Session("node-context", InMemorySessionStore())
        node = Node(
            "contextual",
            agent=agent,
            runner=runner,
            input_schema={"type": "object", "additionalProperties": False},
            session_store=memory.store,
            context_blocks=(ContextBlock(kind="reference", origin="test.host", content="Reference facts"),),
        )
        result = await GraphExecutor().execute(Graph(graph_id="contextual-node", nodes=(node,)), {})
        assert result.status is GraphStatus.SUCCEEDED
        user = next(message for message in model.requests[0].messages if message.role == "user")
        assert isinstance(user.content, list)
        block = user.content[0]
        assert isinstance(block, dict) and isinstance(block["text"], str)
        assert "Reference facts" in block["text"]
        assert '"content":"{}"' in block["text"]
        assert '"source":"graph:contextual"' in block["text"]
        system = next(message for message in model.requests[0].messages if message.role == "system")
        assert "Reference facts" not in str(system.content)
        session_id = result.state.node_states[0].session_id
        assert session_id is not None
        steps = await Session(session_id, memory.store).history()
        assert [
            input_text(message.content)
            for step in steps
            for message in step.messages
            if isinstance(message, UserMessage)
        ] == ["{}"]

    asyncio.run(scenario())


def test_agent_node_budget_limit_is_a_known_failure_with_its_run_identity_and_usage(tmp_path: Path) -> None:
    model = FakeOpenAIModel((tool_batch("echo", '{"value":"x"}', "call-1"),), report_usage=False)
    runner, agent = make_runner(model, tmp_path)
    events: list[RunFailedEvent] = []

    async def collect(event: object) -> None:
        if isinstance(event, RunFailedEvent):
            events.append(event)

    runner.event_sink = collect
    graph = Graph(
        graph_id="limited-node",
        nodes=(
            Node(
                "limited",
                agent=agent,
                runner=runner,
                limits=RunLimits(max_output_tokens=5),
            ),
        ),
    )
    result = asyncio.run(GraphExecutor().execute(graph, {}))

    assert result.status is GraphStatus.FAILED
    assert result.error_code == "orchestration.graph.node_limit_reached"
    state = result.state.node_states[0]
    assert state.status is NodeStatus.FAILED and state.run_status is RunStatus.FAILED
    assert len(events) == 1 and state.node_run_id == events[0].run_id
    assert state.usage is not None and state.usage.output_tokens is None
    assert result.child_usage is not None and result.child_usage.budget_output_tokens == 5
    assert result.child_usage.output_tokens is None


def test_agent_node_timeout_preserves_the_known_run_instead_of_raising_a_system_fault(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        scope = asyncio.Timeout(None)

        def controlled_timeout(delay: float | None) -> asyncio.Timeout:
            return scope

        monkeypatch.setattr(asyncio, "timeout", controlled_timeout)
        model = BlockingOpenAIModel()
        runner, agent = make_runner(model, tmp_path)
        events: list[RunFailedEvent] = []

        async def collect(event: object) -> None:
            if isinstance(event, RunFailedEvent):
                events.append(event)

        runner.event_sink = collect
        task = asyncio.create_task(Node("timed", agent=agent, runner=runner).run({}))
        await wait_for_event(model.started)
        scope.reschedule(asyncio.get_running_loop().time())
        result = await asyncio.wait_for(task, 1)
        assert result.status is NodeStatus.FAILED and result.run_status is RunStatus.FAILED
        assert result.error_code == "orchestration.graph.node_timeout"
        assert len(events) == 1 and result.node_run_id == events[0].run_id
        assert result.usage is not None

    asyncio.run(scenario())


def test_graph_child_inherits_parent_grants_deadline_identity_and_remaining_budget(tmp_path: Path) -> None:
    model = FakeOpenAIModel(
        (
            tool_batch("read_file", '{"scope":"workspace","path":"secret.txt"}', "read"),
            tool_batch("echo", '{"value":"x"}', "inspect"),
            text_batch("done", "final"),
        )
    )
    observed: list[ToolContext] = []

    async def inspect(context: ToolContext, arguments: JsonObject) -> JsonObject:
        observed.append(context)
        return arguments

    runner, agent = make_runner(model, tmp_path, inspect)
    runner.host_policy = HostPolicy(grants=CapabilityGrants(capabilities=frozenset({"file.read", "file.write"})))
    (tmp_path / "workspace" / "secret.txt").write_text("must not reach the model")
    deadline = datetime.now(UTC) + timedelta(seconds=30)
    context = ToolContext(
        call_id="execute-graph",
        run_id="parent-run",
        parent_limits=ParentRunLimits(
            deadline=deadline,
            remaining_model_steps=4,
            remaining_output_tokens=6,
            tool_concurrency=1,
        ),
    )
    graph = Graph(graph_id="parent-scope", nodes=(Node("child", agent=agent, runner=runner),))

    result = asyncio.run(GraphExecutor().execute(graph, {}, context=context))

    assert result.status is GraphStatus.SUCCEEDED
    assert [request.max_completion_tokens for request in model.requests] == [6, 5, 4]
    assert "tool.permission.denied" in model.requests[1].model_dump_json()
    assert "must not reach the model" not in model.requests[1].model_dump_json()
    assert len(observed) == 1
    assert observed[0].parent_run_id == "parent-run"
    assert observed[0].run_id == result.state.node_states[0].node_run_id
    assert observed[0].grants == context.grants
    assert observed[0].parent_limits is not None
    assert observed[0].parent_limits.deadline == deadline
    assert observed[0].parent_limits.tool_concurrency == 1
    assert result.child_usage is not None and result.child_usage.model_calls == 3


@pytest.mark.parametrize("concurrency", [1, 2])
def test_parallel_graph_children_reserve_disjoint_parent_allowances(tmp_path: Path, concurrency: int) -> None:
    async def scenario() -> None:
        observed: list[ToolContext] = []
        first_started = asyncio.Event()
        both_started = asyncio.Event()
        release = asyncio.Event()

        async def wait(context: ToolContext, arguments: JsonObject) -> JsonObject:
            observed.append(context)
            first_started.set()
            if len(observed) == 2:
                both_started.set()
            await release.wait()
            return arguments

        models = tuple(
            FakeOpenAIModel(
                (
                    tool_batch("echo", '{"value":"x"}', f"call-{index}"),
                    text_batch("done", f"final-{index}"),
                )
            )
            for index in range(2)
        )
        nodes: list[Node] = []
        for index, model in enumerate(models):
            root = tmp_path / str(index)
            root.mkdir()
            runner, agent = make_runner(model, root, wait)
            nodes.append(
                Node(str(index), agent=agent, runner=runner, limits=RunLimits(max_model_steps=2, max_output_tokens=5))
            )
        context = ToolContext(
            call_id="graph",
            run_id="parent",
            parent_limits=ParentRunLimits(
                deadline=datetime.now(UTC) + timedelta(minutes=1),
                remaining_model_steps=4,
                remaining_output_tokens=8,
                tool_concurrency=concurrency,
            ),
        )
        executor = GraphExecutor()
        task = asyncio.create_task(
            executor.execute(Graph(graph_id="parallel-budget", nodes=tuple(nodes)), {}, context=context)
        )
        await wait_for_event(first_started)
        if concurrency == 1:
            assert len(observed) == 1
        else:
            await wait_for_event(both_started)
            assert {model.requests[0].max_completion_tokens for model in models} == {3, 5}
        release.set()
        result = await asyncio.wait_for(task, 1)
        assert result.status is GraphStatus.SUCCEEDED
        assert result.child_usage is not None and result.child_usage.model_calls == 4
        saved = executor.budget_state("parent")
        assert saved is not None and saved.remaining_output_tokens == 4

    asyncio.run(scenario())


def test_ready_graph_node_waits_for_reserved_child_budget_to_be_refunded(tmp_path: Path) -> None:
    async def scenario() -> None:
        started = asyncio.Event()
        release = asyncio.Event()

        async def hold(context: ToolContext, arguments: JsonObject) -> JsonObject:
            started.set()
            await release.wait()
            return arguments

        first_root = tmp_path / "first"
        first_root.mkdir()
        first_model = FakeOpenAIModel((tool_batch("echo", '{"value":"x"}', "call"), text_batch("first", "first")))
        first_runner, first_agent = make_runner(first_model, first_root, hold)
        second_root = tmp_path / "second"
        second_root.mkdir()
        second_model = FakeOpenAIModel((text_batch("second", "second"),))
        second_runner, second_agent = make_runner(second_model, second_root)
        graph = Graph(
            graph_id="refund-ready",
            nodes=(
                Node("first", agent=first_agent, runner=first_runner),
                Node("second", agent=second_agent, runner=second_runner),
            ),
        )
        context = ToolContext(
            call_id="graph",
            run_id="parent",
            parent_limits=ParentRunLimits(
                deadline=datetime.now(UTC) + timedelta(minutes=1),
                remaining_model_steps=4,
                remaining_output_tokens=3,
                tool_concurrency=2,
            ),
        )
        executor = GraphExecutor()
        task = asyncio.create_task(executor.execute(graph, {}, context=context))
        await wait_for_event(started)
        assert second_model.requests == []
        release.set()
        result = await asyncio.wait_for(task, 1)
        assert result.status is GraphStatus.SUCCEEDED
        assert second_model.requests[0].max_completion_tokens == 1
        repeated = await executor.execute(graph, {}, context=context)
        assert repeated.status is GraphStatus.FAILED
        assert repeated.error_code == "orchestration.graph.child_budget_exhausted"
        assert len(first_model.requests) == 2 and len(second_model.requests) == 1

    asyncio.run(scenario())


def test_graph_and_subagents_can_share_one_parent_child_budget_ledger(tmp_path: Path) -> None:
    model = FakeOpenAIModel((text_batch("subagent", "subagent"), text_batch("graph", "graph")))
    runner, agent = make_runner(model, tmp_path)
    ledger = BudgetLedger()
    group = SubagentGroup((Subagent("subagent", agent, runner),), budgets=ledger)
    executor = GraphExecutor(budgets=ledger)
    context = ToolContext(
        call_id="delegate",
        run_id="parent",
        parent_limits=ParentRunLimits(
            deadline=datetime.now(UTC) + timedelta(minutes=1),
            remaining_model_steps=3,
            remaining_output_tokens=5,
            tool_concurrency=1,
        ),
    )

    async def scenario() -> None:
        await group.run_subagent("subagent", "first", context)
        result = await executor.execute(
            Graph(graph_id="shared", nodes=(Node("child", agent=agent, runner=runner),)), {}, context=context
        )
        assert result.status is GraphStatus.SUCCEEDED
        assert result.child_usage is not None and result.child_usage.model_calls == 1

    asyncio.run(scenario())

    assert [request.max_completion_tokens for request in model.requests] == [5, 4]


def test_restored_graph_budget_keeps_unknown_provider_usage_charged(tmp_path: Path) -> None:
    model = FakeOpenAIModel((text_batch("first", "first"),), report_usage=False)
    runner, agent = make_runner(model, tmp_path)
    graph = Graph(graph_id="unknown-budget", nodes=(Node("child", agent=agent, runner=runner),))
    context = ToolContext(
        call_id="graph",
        run_id="parent",
        parent_limits=ParentRunLimits(
            deadline=datetime.now(UTC) + timedelta(minutes=1),
            remaining_model_steps=3,
            remaining_output_tokens=5,
            tool_concurrency=1,
        ),
    )
    first = GraphExecutor()
    result = asyncio.run(first.execute(graph, {}, context=context))
    assert result.status is GraphStatus.SUCCEEDED
    assert result.child_usage is not None and result.child_usage.output_tokens is None
    saved = first.budget_state("parent")
    assert saved is not None and saved.remaining_output_tokens == 0
    restored = GraphExecutor()
    restored.restore_budget("parent", saved)
    restored.restore_budget("parent", saved)
    repeated = asyncio.run(restored.execute(graph, {}, context=context))
    assert repeated.status is GraphStatus.FAILED
    assert repeated.error_code == "orchestration.graph.child_budget_exhausted"
    assert len(model.requests) == 1


def test_graph_timeout_charges_unknown_inflight_usage_and_cannot_release_active_reservations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        scope = asyncio.Timeout(None)
        original_timeout = asyncio.timeout
        first_scope = True

        def controlled_timeout(delay: float | None) -> asyncio.Timeout:
            nonlocal first_scope
            if first_scope:
                first_scope = False
                return scope
            return original_timeout(delay)

        monkeypatch.setattr(asyncio, "timeout", controlled_timeout)
        model = BlockingOpenAIModel()
        runner, agent = make_runner(model, tmp_path)
        graph = Graph(graph_id="unknown-inflight", nodes=(Node("child", agent=agent, runner=runner),))
        context = ToolContext(
            call_id="graph",
            run_id="parent",
            parent_limits=ParentRunLimits(
                deadline=datetime.now(UTC) + timedelta(minutes=1),
                remaining_model_steps=2,
                remaining_output_tokens=5,
                tool_concurrency=1,
            ),
        )
        executor = GraphExecutor()
        task = asyncio.create_task(executor.execute(graph, {}, context=context))
        await wait_for_event(model.started)
        with pytest.raises(BudgetStateError, match="Active child reservations"):
            # The host must not discard a live child's reserved allowance.
            executor.release_budget("parent")
        scope.reschedule(asyncio.get_running_loop().time())
        result = await asyncio.wait_for(task, 1)
        assert result.status is GraphStatus.FAILED and result.error_code == "orchestration.graph.timed_out"
        assert result.child_usage is None
        saved = executor.budget_state("parent")
        assert saved is not None and saved.remaining_output_tokens == 0 and saved.remaining_model_steps == 0
        executor.release_budget("parent")
        executor.release_budget("parent")
        assert executor.budget_state("parent") is None

    asyncio.run(scenario())


def test_graph_starts_ready_downstream_while_an_unrelated_node_is_running() -> None:
    """An event-driven scheduler does not wait for an unrelated slow branch."""

    async def exercise() -> GraphResult:
        slow_started = asyncio.Event()
        release_slow = asyncio.Event()
        downstream_started = asyncio.Event()

        async def first(_: JsonObject) -> JsonObject:
            return {"value": 1}

        async def slow(_: JsonObject) -> JsonObject:
            slow_started.set()
            await release_slow.wait()
            return {"value": 2}

        async def downstream(input_value: JsonObject) -> JsonObject:
            assert input_value == {"value": 1}
            downstream_started.set()
            return {"value": input_value["value"]}

        graph = Graph(
            graph_id="event-driven",
            max_concurrency=2,
            nodes=(
                Node("first", handler=first),
                Node("slow", handler=slow),
                Node("downstream", handler=downstream),
            ),
            edges=(
                Edge(
                    "first-to-downstream",
                    "first",
                    "downstream",
                    mappings=(EdgeMapping("/value", "/value"),),
                ),
            ),
            outputs=(OutputBinding("downstream", "/value", "/value"),),
        )
        task = asyncio.create_task(GraphExecutor().execute(graph, {}))
        await wait_for_event(slow_started)
        await wait_for_event(downstream_started)
        release_slow.set()
        return await task

    result = asyncio.run(exercise())

    assert result.status is GraphStatus.SUCCEEDED
    assert result.outputs == {"value": 1}


def test_graph_enforces_max_concurrency_without_delaying_ready_nodes_after_capacity_frees() -> None:
    """The scheduler starts the next ready node only after a slot is released."""

    async def exercise() -> tuple[GraphResult, bool]:
        first_started = asyncio.Event()
        release_first = asyncio.Event()
        second_started = asyncio.Event()

        async def first(_: JsonObject) -> JsonObject:
            first_started.set()
            await release_first.wait()
            return {"done": True}

        async def second(_: JsonObject) -> JsonObject:
            second_started.set()
            return {"done": True}

        graph = Graph(
            graph_id="concurrency",
            max_concurrency=1,
            nodes=(Node("first", handler=first), Node("second", handler=second)),
        )
        task = asyncio.create_task(GraphExecutor().execute(graph, {}))
        await wait_for_event(first_started)
        blocked_before_release = not second_started.is_set()
        release_first.set()
        result = await task
        return result, blocked_before_release

    result, blocked_before_release = asyncio.run(exercise())

    assert blocked_before_release
    assert result.status is GraphStatus.SUCCEEDED


def test_graph_conditional_edges_skip_inactive_branch_and_join_active_branch() -> None:
    """Conditional edges produce explicit skip state and wait only for active inputs."""
    calls: list[str] = []

    async def decide(_: JsonObject) -> JsonObject:
        calls.append("decide")
        return {"choice": "yes"}

    async def yes(_: JsonObject) -> JsonObject:
        calls.append("yes")
        return {"answer": "yes"}

    async def no(_: JsonObject) -> JsonObject:
        calls.append("no")
        return {"answer": "no"}

    async def join(input_value: JsonObject) -> JsonObject:
        calls.append("join")
        return {"answer": input_value["answer"]}

    graph = Graph(
        graph_id="conditional",
        nodes=(
            Node("decide", handler=decide),
            Node("yes", handler=yes),
            Node("no", handler=no),
            Node("join", handler=join),
        ),
        edges=(
            Edge(
                "to-yes",
                "decide",
                "yes",
                condition=EdgeCondition("/choice", "equals", "yes"),
            ),
            Edge(
                "to-no",
                "decide",
                "no",
                condition=EdgeCondition("/choice", "equals", "no"),
            ),
            Edge("yes-to-join", "yes", "join", mappings=(EdgeMapping("/answer", "/answer"),)),
            Edge("no-to-join", "no", "join"),
        ),
        outputs=(OutputBinding("join", "/answer", "/answer"),),
    )

    result = asyncio.run(GraphExecutor().execute(graph, {}))

    states = {item.node_id: item for item in result.state.node_states}
    assert result.status is GraphStatus.SUCCEEDED
    assert calls == ["decide", "yes", "join"]
    assert states["no"].status is NodeStatus.SKIPPED
    assert result.outputs == {"answer": "yes"}


def test_graph_emits_lifecycle_and_join_events_without_changing_execution() -> None:
    """A real two-input join reports Graph, Node, and join lifecycle facts."""
    events: list[GraphEvent] = []

    async def source(_: JsonObject) -> JsonObject:
        return {"value": 1}

    async def join(input_value: JsonObject) -> JsonObject:
        return input_value

    async def collect(event: object) -> None:
        assert isinstance(event, GraphEvent)
        events.append(event)

    graph = Graph(
        graph_id="observed-join",
        nodes=(Node("left", handler=source), Node("right", handler=source), Node("join", handler=join)),
        edges=(
            Edge("left-join", "left", "join", mappings=(EdgeMapping("/value", "/left"),)),
            Edge("right-join", "right", "join", mappings=(EdgeMapping("/value", "/right"),)),
        ),
    )

    result = asyncio.run(GraphExecutor().execute(graph, {}, event_sink=collect))

    assert result.status is GraphStatus.SUCCEEDED
    assert events[0].kind is GraphEventKind.GRAPH_STARTED
    assert any(event.kind is GraphEventKind.JOIN_READY and event.node_id == "join" for event in events)
    assert events[-1].kind is GraphEventKind.GRAPH_COMPLETED
    assert events[-1].status == GraphStatus.SUCCEEDED.value


def test_graph_notification_failure_does_not_replace_business_result() -> None:
    """A best-effort lifecycle sink cannot turn a successful Graph into failure."""

    async def node(_: JsonObject) -> JsonObject:
        return {"ok": True}

    async def fail_notification(_: object) -> None:
        raise RuntimeError("synthetic sink failure")

    graph = Graph(graph_id="sink-failure", nodes=(Node("node", handler=node),))

    result = asyncio.run(GraphExecutor().execute(graph, {}, event_sink=fail_notification))

    assert result.status is GraphStatus.SUCCEEDED


@pytest.mark.parametrize("invalid_output", [False, True])
def test_graph_delivers_node_start_before_running_a_node_with_a_blocked_event_sink(invalid_output: bool) -> None:
    """A delayed start notification cannot arrive after the Node's result."""

    async def scenario() -> None:
        started = asyncio.Event()
        release = asyncio.Event()
        timeline: list[str] = []

        async def node(input_value: JsonObject) -> JsonObject:
            timeline.append("node.result")
            return input_value

        async def collect(event: object) -> None:
            assert isinstance(event, GraphEvent)
            if event.kind is GraphEventKind.NODE_STARTED:
                started.set()
                await release.wait()
            timeline.append(event.kind.value)

        graph = Graph(
            graph_id="ordered-node-start",
            nodes=(
                Node(
                    "node",
                    handler=node,
                    output_schema={"type": "object", "required": ["value"]} if invalid_output else {},
                ),
            ),
        )
        task = asyncio.create_task(GraphExecutor().execute(graph, {}, event_sink=collect))
        try:
            await wait_for_event(started)
            assert timeline == ["graph.started"]
        finally:
            release.set()
            result = await asyncio.wait_for(task, 1)
        assert result.status is (GraphStatus.FAILED if invalid_output else GraphStatus.SUCCEEDED)
        assert timeline == [
            "graph.started",
            "graph.node.started",
            "node.result",
            "graph.node.completed",
            "graph.failed" if invalid_output else "graph.completed",
        ]

    asyncio.run(scenario())


def test_cancelling_a_node_start_notification_refunds_its_unused_parent_budget(tmp_path: Path) -> None:
    """Cancellation before task creation cannot spend or retain child allowance."""

    async def scenario() -> None:
        started = asyncio.Event()
        release = asyncio.Event()
        model = FakeOpenAIModel((text_batch("unused", "unused"),))
        runner, agent = make_runner(model, tmp_path)
        graph = Graph(graph_id="cancel-before-node", nodes=(Node("node", agent=agent, runner=runner),))
        executor = GraphExecutor()
        context = ToolContext(
            call_id="graph",
            run_id="parent",
            parent_limits=ParentRunLimits(
                deadline=datetime.now(UTC) + timedelta(minutes=1),
                remaining_model_steps=2,
                remaining_output_tokens=8,
                tool_concurrency=1,
            ),
        )

        async def collect(event: object) -> None:
            assert isinstance(event, GraphEvent)
            if event.kind is GraphEventKind.NODE_STARTED:
                started.set()
                await release.wait()

        task = asyncio.create_task(executor.execute(graph, {}, event_sink=collect, context=context))
        try:
            await wait_for_event(started)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        saved = executor.budget_state("parent")
        assert saved is not None
        assert saved.remaining_model_steps == 2
        assert saved.remaining_output_tokens == 8
        assert model.requests == []

    asyncio.run(scenario())


def test_graph_rejects_mapping_conflict_before_running_any_node() -> None:
    """Overlapping input writes fail validation before any Node side effect occurs."""
    calls: list[str] = []

    async def root(_: JsonObject) -> JsonObject:
        calls.append("root")
        return {"value": 1}

    async def other(_: JsonObject) -> JsonObject:
        calls.append("other")
        return {"value": 2}

    async def target(_: JsonObject) -> JsonObject:
        calls.append("target")
        return {"value": 3}

    graph = Graph(
        graph_id="conflict",
        nodes=(Node("root", handler=root), Node("other", handler=other), Node("target", handler=target)),
        edges=(
            Edge("root-target", "root", "target", mappings=(EdgeMapping("/value", "/value"),)),
            Edge("other-target", "other", "target", mappings=(EdgeMapping("/value", "/value"),)),
        ),
    )

    result = asyncio.run(GraphExecutor().execute(graph, {}))

    assert result.status is GraphStatus.FAILED
    assert result.error_code == "orchestration.graph.invalid"
    assert calls == []


def test_graph_recovery_retries_failed_nodes_without_rerunning_confirmed_nodes() -> None:
    """Recovery resumes a failed node while retaining successful predecessor state."""
    calls: list[str] = []
    valid = False

    async def first(_: JsonObject) -> JsonObject:
        calls.append("first")
        return {"value": 1}

    async def second(_: JsonObject) -> JsonObject:
        calls.append("second")
        return {"ok": True} if valid else {"wrong": True}

    schema: JsonSchema = {
        "type": "object",
        "properties": {"ok": {"type": "boolean"}},
        "required": ["ok"],
        "additionalProperties": False,
    }
    graph = Graph(
        graph_id="recovery",
        nodes=(Node("first", handler=first), Node("second", handler=second, output_schema=schema)),
        edges=(Edge("first-second", "first", "second"),),
    )

    first_result = asyncio.run(GraphExecutor().execute(graph, {}))
    valid = True
    second_result = asyncio.run(GraphExecutor().execute(graph, {}, state=first_result.state))

    assert first_result.status is GraphStatus.FAILED
    assert second_result.status is GraphStatus.SUCCEEDED
    assert calls == ["first", "second", "second"]


@pytest.mark.parametrize("budgeted", [True, False], ids=["parent-budget", "host-limits-only"])
def test_graph_pauses_blocked_downstream_and_resumes_without_repeating_confirmed_sibling(
    tmp_path: Path, budgeted: bool
) -> None:
    calls: list[str] = []
    model = FakeOpenAIModel(
        (tool_batch("echo", '{"value":"approve"}', "source-call"), text_batch('{"value":42}', "final"))
    )
    runner, agent = make_runner(model, tmp_path, interrupting_tool)
    schema: JsonSchema = {"type": "object", "properties": {"value": {"type": "integer"}}, "required": ["value"]}
    source = replace(agent, output_schema=schema)

    async def sibling(input_value: JsonObject) -> JsonObject:
        calls.append("sibling")
        return {"confirmed": True}

    async def downstream(input_value: JsonObject) -> JsonObject:
        calls.append("downstream")
        return input_value

    graph = Graph(
        graph_id="paused-dependency",
        nodes=(
            Node(
                "source",
                agent=source,
                runner=runner,
                output_schema=schema,
                side_effect=True,
                limits=RunLimits(max_model_steps=3, max_output_tokens=20),
            ),
            Node("sibling", handler=sibling),
            Node("downstream", handler=downstream),
        ),
        edges=(Edge("source-downstream", "source", "downstream", mappings=(EdgeMapping("/value", "/value"),)),),
    )
    context = ToolContext(
        call_id="graph-call",
        run_id="parent",
        parent_limits=ParentRunLimits(
            deadline=datetime.now(UTC) + timedelta(minutes=1),
            remaining_model_steps=4,
            remaining_output_tokens=30,
            tool_concurrency=2,
        ),
    )

    if not budgeted:
        context = context.model_copy(update={"parent_limits": None})

    async def scenario() -> None:
        paused = await GraphExecutor().execute(graph, {}, context=context)
        assert paused.status is GraphStatus.PAUSED
        states = {item.node_id: item for item in paused.state.node_states}
        assert states["sibling"].status is NodeStatus.SUCCEEDED
        assert states["downstream"].status is NodeStatus.PENDING
        checkpoint = states["source"].checkpoint
        assert checkpoint is not None
        assert paused.child_usage is not None and paused.child_usage.model_calls == 1
        assert checkpoint.parent_run_id == context.run_id
        if budgeted:
            assert paused.state.budget is not None and len(paused.state.budget.reservations) == 1
        else:
            assert paused.state.budget is None and states["source"].reservation_id is None
        with pytest.raises(GraphSystemFault, match="input changed"):
            await GraphExecutor().execute(graph, {"changed": True}, state=paused.state, context=context)
        assert len(model.requests) == 1
        interruption = checkpoint.interruptions[0]
        now = datetime.now(UTC)
        decision = ResumeDecision(
            interruption_id=interruption.interruption_id,
            result=ToolResult(
                call_id=interruption.call_id, ok=True, output={"approved": True}, started_at=now, finished_at=now
            ),
        )
        resumed = await GraphExecutor().execute(
            graph,
            {},
            state=paused.state,
            context=context,
            resumes=(NodeResume(node_id="source", decisions=(decision,)),),
        )
        assert resumed.status is GraphStatus.SUCCEEDED
        assert resumed.outputs == {"downstream": {"value": 42}, "sibling": {"confirmed": True}}
        assert resumed.state.node_states[0].node_run_id == checkpoint.run_id
        assert resumed.child_usage is not None and resumed.child_usage.model_calls == 2
        if budgeted:
            assert resumed.state.budget is not None and resumed.state.budget.reservations == ()
            assert resumed.state.budget.state.remaining_model_steps == 2
        else:
            assert resumed.state.budget is None
        assert calls == ["sibling", "downstream"]
        assert len(model.requests) == 2

    asyncio.run(scenario())


@pytest.mark.parametrize("budgeted", [True, False], ids=["parent-budget", "host-limits-only"])
@pytest.mark.parametrize("bound", [True, False], ids=["bound-graph", "revoked-model-binding"])
def test_cancelling_a_paused_graph_preserves_siblings_and_refunds_its_child_budget_once(
    tmp_path: Path, bound: bool, budgeted: bool
) -> None:
    model = FakeOpenAIModel((tool_batch("echo", '{"value":"approve"}', "call-1"),))
    runner, agent = make_runner(model, tmp_path, interrupting_tool)

    async def sibling(input_value: JsonObject) -> JsonObject:
        return {"confirmed": True}

    graph = Graph(
        graph_id="cancel-paused",
        nodes=(Node("paused", agent=agent, runner=runner), Node("sibling", handler=sibling)),
    )
    context = ToolContext(
        call_id="graph-call",
        run_id="parent",
        parent_limits=ParentRunLimits(
            deadline=datetime.now(UTC) + timedelta(minutes=1),
            remaining_model_steps=3,
            remaining_output_tokens=20,
            tool_concurrency=2,
        ),
    )

    if not budgeted:
        context = context.model_copy(update={"parent_limits": None})

    async def scenario() -> None:
        executor = GraphExecutor()
        paused = await executor.execute(graph, {}, context=context)
        checkpoint = paused.state.node_states[0].checkpoint
        assert checkpoint is not None
        for owner in (executor, executor, GraphExecutor()):
            if bound:
                result = await owner.cancel_paused(graph, paused.state, reason="user_cancelled")
            else:
                result = await owner.cancel_paused(
                    None,
                    paused.state,
                    reason="user_cancelled",
                    runner=runner,
                    session_store=graph.nodes[0].session_store,
                )
            assert result.status is GraphStatus.CANCELLED
            states = {item.node_id: item for item in result.state.node_states}
            assert states["paused"].status is NodeStatus.CANCELLED
            assert states["paused"].node_run_id == checkpoint.run_id
            assert states["paused"].checkpoint is None
            assert states["sibling"].status is NodeStatus.SUCCEEDED
            assert states["sibling"].output == {"confirmed": True}
            assert result.child_usage is not None and result.child_usage.model_calls == 1
            if budgeted:
                assert result.state.budget is not None and result.state.budget.reservations == ()
                assert result.state.budget.state.remaining_model_steps == 2
            else:
                assert result.state.budget is None
                assert owner.budgets.pools == {} and owner.budgets.reservations == {}
        assert len(model.requests) == 1
        snapshot = await graph.nodes[0].session_store.run(checkpoint.session_id, checkpoint.run_id)
        assert snapshot.state is RunStatus.CANCELLED

    asyncio.run(scenario())


def test_graph_node_can_run_a_subagent_handoff_through_real_sdk_components(tmp_path: Path) -> None:
    """Graph, SubagentGroup, and Handoff keep the child Run on one shared control path."""
    source_model = FakeOpenAIModel((tool_batch("transfer_to_reviewer", '{"task":"review"}', "handoff-call"),))
    target_model = FakeAnthropicModel((anthropic_text_batch("reviewed", "target-final"),))
    source_root = tmp_path / "source"
    source_root.mkdir()
    runner, source = make_runner(source_model, source_root)
    target = Agent(name="reviewer", model=target_model, prompt="Review the task.")
    binding = Handoff(
        source=source,
        target=target,
        name="transfer_to_reviewer",
        description="Transfer the current task to the reviewer.",
    ).bind(runner)
    subagent = Subagent("reviewer-subagent", source, runner, handoffs=(binding,))
    group = SubagentGroup((subagent,))

    async def subagent_node(input_value: JsonObject) -> JsonObject:
        result = await group.run_subagent(
            subagent,
            "review this graph input",
            ToolContext(call_id="graph-call", run_id="graph-run"),
        )
        assert isinstance(result, RunResult)
        return {"text": result.final_output}

    graph = Graph(
        graph_id="composition",
        nodes=(Node("subagent", handler=subagent_node),),
        outputs=(OutputBinding("subagent", "/text", "/text"),),
    )

    result = asyncio.run(GraphExecutor().execute(graph, {}))

    assert result.status is GraphStatus.SUCCEEDED
    assert result.outputs == {"text": "reviewed"}
    assert len(source_model.requests) == 1
    assert len(target_model.requests) == 1
    assert len(group.records) == 1
    assert next(iter(group.records.values())).child_run_id is not None


def test_invalid_node_input_cancels_an_already_running_sibling_before_returning() -> None:
    async def exercise() -> None:
        started = asyncio.Event()
        cleaned = asyncio.Event()
        blocked = asyncio.Event()

        async def sibling(input_value: JsonObject) -> JsonObject:
            started.set()
            try:
                await blocked.wait()
                return input_value
            finally:
                cleaned.set()

        async def source(input_value: JsonObject) -> JsonObject:
            await started.wait()
            return input_value

        graph = Graph(
            graph_id="invalid-input-cleanup",
            nodes=(
                Node("sibling", handler=sibling),
                Node("source", handler=source),
                Node("invalid", handler=source, input_schema={"type": "object", "required": ["missing"]}),
            ),
            edges=(Edge("source-invalid", "source", "invalid"),),
        )
        result = await asyncio.wait_for(GraphExecutor().execute(graph, {}), 1)
        assert result.status is GraphStatus.FAILED
        assert result.error_code == "orchestration.graph.node_failed"
        assert cleaned.is_set()

    asyncio.run(exercise())


@pytest.mark.parametrize("pending_side_effect", [False, True])
def test_timeout_preserves_confirmed_side_effects_and_blocks_unknown_side_effect_replay(
    monkeypatch: pytest.MonkeyPatch, pending_side_effect: bool
) -> None:
    async def exercise() -> None:
        timeout_scope = asyncio.Timeout(None)

        def controlled_timeout(delay: float | None) -> asyncio.Timeout:
            assert delay is not None
            return timeout_scope

        monkeypatch.setattr(asyncio, "timeout", controlled_timeout)
        calls: list[str] = []
        cleaned = asyncio.Event()
        should_timeout = True

        async def first(input_value: JsonObject) -> JsonObject:
            calls.append("confirmed")
            return {"value": 42}

        async def second(input_value: JsonObject) -> JsonObject:
            calls.append("pending")
            if should_timeout:
                timeout_scope.reschedule(asyncio.get_running_loop().time())
                try:
                    await asyncio.Event().wait()
                finally:
                    cleaned.set()
            return input_value

        graph = Graph(
            graph_id="timeout-state",
            timeout_seconds=1,
            nodes=(
                Node("confirmed", handler=first, side_effect=True),
                Node("pending", handler=second, side_effect=pending_side_effect),
            ),
            edges=(Edge("confirmed-pending", "confirmed", "pending"),),
        )
        executor = GraphExecutor()
        result = await asyncio.wait_for(executor.execute(graph, {}), 1)
        states = {item.node_id: item for item in result.state.node_states}
        assert result.error_code == "orchestration.graph.timed_out"
        assert states["confirmed"].status is NodeStatus.SUCCEEDED
        assert states["confirmed"].output == {"value": 42}
        assert states["confirmed"].attempt == 1
        assert states["pending"].status is NodeStatus.RUNNING
        assert cleaned.is_set()
        should_timeout = False
        timeout_scope = asyncio.Timeout(None)
        if pending_side_effect:
            with pytest.raises(GraphSystemFault, match="cannot be retried"):
                await executor.execute(graph, {}, state=result.state)
            assert calls == ["confirmed", "pending"]
        else:
            recovered = await executor.execute(graph, {}, state=result.state)
            assert recovered.status is GraphStatus.SUCCEEDED
            assert calls == ["confirmed", "pending", "pending"]

    asyncio.run(exercise())


def test_node_fault_preserves_original_exception_and_cleans_up_running_siblings() -> None:
    async def exercise() -> None:
        started = asyncio.Event()
        cleaned = asyncio.Event()

        async def sibling(input_value: JsonObject) -> JsonObject:
            started.set()
            try:
                await asyncio.Event().wait()
                return input_value
            finally:
                cleaned.set()

        async def fail(input_value: JsonObject) -> JsonObject:
            await started.wait()
            raise RuntimeError("node transport disconnected")

        graph = Graph(
            graph_id="node-fault-cleanup",
            nodes=(Node("sibling", handler=sibling), Node("failed", handler=fail)),
        )
        with pytest.raises(RuntimeError, match="node transport disconnected"):
            await asyncio.wait_for(GraphExecutor().execute(graph, {}), 1)
        assert cleaned.is_set()

    asyncio.run(exercise())


def test_unknown_external_node_result_is_persisted_and_cannot_be_replayed() -> None:
    observed: list[str] = []

    async def process_input(input_value: JsonObject) -> JsonObject:
        return input_value

    class ExternalProcess:
        async def execute(
            self,
            node: Node,
            input_value: JsonObject,
            *,
            checkpoint: RunCheckpoint | None = None,
            decisions: tuple[ResumeDecision, ...] = (),
        ) -> NodeResult:
            observed.append(node.node_id)
            return NodeResult(node_id=node.node_id, node_run_id="external-action", status=NodeStatus.UNKNOWN)

    graph = Graph(
        graph_id="unknown-result",
        nodes=(Node("source", handler=process_input), Node("dependent", handler=process_input)),
        edges=(Edge("source-dependent", "source", "dependent"),),
    )

    async def scenario() -> None:
        result = await GraphExecutor().execute(graph, {}, ExternalProcess())
        assert result.status is GraphStatus.FAILED
        assert result.error_code == "orchestration.graph.recovery_unknown"
        assert result.state.node_states[0].status is NodeStatus.UNKNOWN
        assert result.state.node_states[0].node_run_id == "external-action"
        assert result.state.node_states[1].status is NodeStatus.PENDING
        with pytest.raises(GraphSystemFault, match="requires host reconciliation"):
            await GraphExecutor().execute(graph, {}, ExternalProcess(), state=result.state)
        assert observed == ["source"]

    asyncio.run(scenario())


@pytest.mark.parametrize("duplicate", [False, True])
def test_graph_recovery_rejects_incomplete_or_duplicate_node_facts(duplicate: bool) -> None:
    calls: list[str] = []

    async def perform(input_value: JsonObject) -> JsonObject:
        calls.append("performed")
        return input_value

    graph = Graph(graph_id="complete-facts", nodes=(Node("source", handler=perform),))

    async def scenario() -> None:
        result = await GraphExecutor().execute(graph, {})
        states = (*result.state.node_states, *result.state.node_states) if duplicate else ()
        invalid = result.state.model_copy(update={"node_states": states})
        with pytest.raises(GraphSystemFault, match="missing or duplicate Node"):
            await GraphExecutor().execute(graph, {}, state=invalid)
        assert calls == ["performed"]

    asyncio.run(scenario())


def test_timeout_retains_a_side_effect_result_returned_during_cancellation(monkeypatch: pytest.MonkeyPatch) -> None:
    async def exercise() -> None:
        scope = asyncio.Timeout(None)
        calls: list[str] = []

        def controlled_timeout(delay: float | None) -> asyncio.Timeout:
            assert delay is not None
            return scope

        monkeypatch.setattr(asyncio, "timeout", controlled_timeout)

        async def commit(input_value: JsonObject) -> JsonObject:
            calls.append("committed")
            scope.reschedule(asyncio.get_running_loop().time())
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                # The external operation supplies its confirmed receipt during cleanup.
                return {"receipt": "accepted"}
            return input_value

        graph = Graph(graph_id="commit-on-cancel", nodes=(Node("commit", handler=commit, side_effect=True),))
        executor = GraphExecutor()
        result = await asyncio.wait_for(executor.execute(graph, {}), 1)
        assert result.error_code == "orchestration.graph.timed_out"
        assert result.state.node_states[0].status is NodeStatus.SUCCEEDED
        assert result.state.node_states[0].output == {"receipt": "accepted"}
        scope = asyncio.Timeout(None)
        recovered = await executor.execute(graph, {}, state=result.state)
        assert recovered.outputs == {"commit": {"receipt": "accepted"}}
        assert calls == ["committed"]

    asyncio.run(exercise())


@pytest.mark.parametrize("index", ["-1", "+0", "01", "\u0661"])
def test_graph_output_rejects_non_rfc_array_indices(index: str) -> None:
    async def source(input_value: JsonObject) -> JsonObject:
        return {"items": ["first", "last"]}

    graph = Graph(
        graph_id="invalid-array-index",
        nodes=(Node("source", handler=source),),
        outputs=(OutputBinding("source", f"/items/{index}", "/selected"),),
    )
    result = asyncio.run(GraphExecutor().execute(graph, {}))
    assert result.status is GraphStatus.FAILED
    assert result.outputs == {}
    assert result.error_code == "orchestration.graph.node_failed"


def test_graph_recovery_rejects_changed_graph_output_contract() -> None:
    async def source(input_value: JsonObject) -> JsonObject:
        return {"value": 1}

    graph = Graph(graph_id="schema-recovery", nodes=(Node("source", handler=source),))
    result = asyncio.run(GraphExecutor().execute(graph, {}))
    changed = replace(graph, output_schema={"type": "object", "required": ["different"]})
    with pytest.raises(GraphSystemFault, match="fingerprint"):
        asyncio.run(GraphExecutor().execute(changed, {}, state=result.state))


def test_graph_records_inactive_branch_after_source_output_fails_validation() -> None:
    async def invalid(input_value: JsonObject) -> JsonObject:
        return {"wrong": True}

    async def dependent(input_value: JsonObject) -> JsonObject:
        raise AssertionError("Invalid source output must not activate its dependent")

    graph = Graph(
        graph_id="invalid-source",
        nodes=(
            Node("source", handler=invalid, output_schema={"type": "object", "required": ["value"]}),
            Node("dependent", handler=dependent),
        ),
        edges=(Edge("dependency", "source", "dependent"),),
    )
    result = asyncio.run(GraphExecutor().execute(graph, {}))
    assert result.status is GraphStatus.FAILED
    assert result.state is not None
    assert not result.state.branch_decisions[0].activated


@pytest.mark.parametrize("reference", ["https://example.invalid/schema", "file:///tmp/schema.json"])
def test_graph_rejects_external_schema_references_before_execution(reference: str) -> None:
    async def source(input_value: JsonObject) -> JsonObject:
        return input_value

    with pytest.raises(ValueError, match="external or remote"):
        Graph(
            graph_id="external-schema",
            nodes=(Node("source", handler=source),),
            input_schema={"$ref": reference},
        )


@pytest.mark.parametrize("timeout", [float("nan"), float("inf"), float("-inf")])
def test_graph_rejects_nonfinite_execution_timeout(timeout: float) -> None:
    async def source(input_value: JsonObject) -> JsonObject:
        return input_value

    with pytest.raises(ValueError, match="finite and positive"):
        Graph(graph_id="invalid-timeout", nodes=(Node("source", handler=source),), timeout_seconds=timeout)
