import asyncio
from dataclasses import replace
from pathlib import Path

import pytest

from pleiad.agent.agent import Agent
from pleiad.contracts import JsonObject, JsonSchema
from pleiad.events.graph import GraphEvent, GraphEventKind
from pleiad.orchestration.graph.edge import Edge, EdgeCondition, EdgeMapping
from pleiad.orchestration.graph.errors import GraphSystemFault
from pleiad.orchestration.graph.executor import GraphExecutor
from pleiad.orchestration.graph.graph import Graph, OutputBinding
from pleiad.orchestration.graph.node import Node
from pleiad.orchestration.graph.state import GraphResult, GraphStatus, NodeStatus
from pleiad.orchestration.handoff.handoff import Handoff
from pleiad.orchestration.supervisor.supervisor import Supervisor
from pleiad.orchestration.supervisor.worker import Worker
from pleiad.tools.contracts import ToolContext
from tests.orchestration.supervisor.test_supervisor import tool_batch
from tests.runtime.execution.test_runner import (
    FakeAnthropicModel,
    FakeOpenAIModel,
    anthropic_text_batch,
    make_runner,
)


async def wait_for_event(event: asyncio.Event) -> None:
    """Wait for a test barrier without masking scheduler deadlocks."""
    await asyncio.wait_for(event.wait(), timeout=1)


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


def test_graph_node_can_run_a_worker_handoff_through_real_sdk_components(tmp_path: Path) -> None:
    """Graph, Supervisor, and Handoff keep the child Run on one shared control path."""
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
    worker = Worker("reviewer-worker", source, runner, handoffs=(binding,))
    supervisor = Supervisor((worker,))

    async def worker_node(_: JsonObject) -> JsonObject:
        result = await supervisor.run_worker(
            worker,
            "review this graph input",
            ToolContext(call_id="graph-call", run_id="graph-run"),
        )
        return {"text": result.final_output}

    graph = Graph(
        graph_id="composition",
        nodes=(Node("worker", handler=worker_node),),
        outputs=(OutputBinding("worker", "/text", "/text"),),
    )

    result = asyncio.run(GraphExecutor().execute(graph, {}))

    assert result.status is GraphStatus.SUCCEEDED
    assert result.outputs == {"text": "reviewed"}
    assert len(source_model.requests) == 1
    assert len(target_model.requests) == 1
    assert len(supervisor.records) == 1
    assert next(iter(supervisor.records.values())).child_run_id is not None


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
