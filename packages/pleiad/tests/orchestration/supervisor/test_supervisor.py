import asyncio
from collections.abc import AsyncIterator, Mapping
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from pleiad.agent.agent import Agent
from pleiad.contracts import CapabilityGrants, JsonObject, ToolRef
from pleiad.events.supervisor import WorkerCompletedEvent
from pleiad.memory.short_memory import ShortMemory
from pleiad.messages.message import AssistantMessage
from pleiad.models.openai.request import OpenAIRequest
from pleiad.models.openai.response import (
    OpenAIStreamChoice,
    OpenAIStreamChunk,
    OpenAIStreamDelta,
    OpenAIStreamFunction,
    OpenAIStreamToolCall,
)
from pleiad.orchestration.budget import BudgetLedger, BudgetState
from pleiad.orchestration.errors import (
    OrchestrationConfigurationError,
    OrchestrationStateError,
    WorkerBudgetError,
    WorkerPolicyError,
)
from pleiad.orchestration.graph.executor import GraphExecutor
from pleiad.orchestration.graph.graph import Graph
from pleiad.orchestration.graph.node import Node
from pleiad.orchestration.graph.state import GraphStatus
from pleiad.orchestration.supervisor.supervisor import Supervisor
from pleiad.orchestration.supervisor.tools import DelegationTool, as_tool, submit_worker, wait_workers
from pleiad.orchestration.supervisor.worker import Worker
from pleiad.runtime.config import RunConfig
from pleiad.runtime.lifecycle.state import RunStatus, RunUsage
from pleiad.tools.contracts import ParentRunLimits, ToolCall, ToolContext, ToolDescriptor, ToolResult
from pleiad.tools.interruption import InterruptionResponse, ToolInterruption
from pleiad.tools.runtime import ToolRuntime
from tests.runtime.execution.test_runner import (
    BlockingOpenAIModel,
    FakeOpenAIModel,
    interrupting_tool,
    make_runner,
    text_batch,
)


def tool_batch(name: str, arguments: str, call_id: str) -> tuple[OpenAIStreamChunk, ...]:
    """Create one deterministic OpenAI Tool-call stream at the model boundary."""
    return (
        OpenAIStreamChunk(
            id="response-tool",
            model="fake-model",
            choices=(
                OpenAIStreamChoice(
                    delta=OpenAIStreamDelta(
                        tool_calls=(
                            OpenAIStreamToolCall(
                                index=0,
                                id=call_id,
                                function=OpenAIStreamFunction(name=name, arguments=arguments),
                            ),
                        ),
                    ),
                ),
            ),
        ),
        OpenAIStreamChunk(
            id="response-tool",
            model="fake-model",
            choices=(OpenAIStreamChoice(delta=OpenAIStreamDelta(), finish_reason="tool_calls"),),
        ),
    )


def test_as_tool_runs_a_real_isolated_child_and_returns_one_controlled_result(tmp_path: Path) -> None:
    """The parent sees one child result while the child history remains isolated."""
    parent_model = FakeOpenAIModel(
        (
            tool_batch("review_code", '{"task":"inspect the patch"}', "parent-call"),
            text_batch("parent complete", "parent-final"),
        )
    )
    child_model = FakeOpenAIModel((text_batch("private child notes", "child-final"),))
    parent_root = tmp_path / "parent"
    child_root = tmp_path / "child"
    parent_root.mkdir()
    child_root.mkdir()
    parent_runner, parent = make_runner(parent_model, parent_root)
    child_runner, child = make_runner(child_model, child_root)
    child = Agent(name="reviewer", model=child.model, prompt=child.prompt, tools=child.tools)
    worker = Worker("reviewer", child, child_runner)
    supervisor = Supervisor((worker,))
    delegation = as_tool(worker, name="review_code", description="Review code.", supervisor=supervisor)
    parent_runner.tools.register(delegation.descriptor, lambda selected: delegation)
    parent = Agent(
        name=parent.name, model=parent.model, prompt=parent.prompt, tools=(*parent.tools, ToolRef("review_code"))
    )

    memory = ShortMemory.in_memory("parent-memory")
    result = asyncio.run(parent_runner.run(parent, "start", memory=memory, config=RunConfig()))

    assert result.status is RunStatus.SUCCEEDED
    assert result.final_output == "parent complete"
    assert len(supervisor.records) == 1
    record = next(iter(supervisor.records.values()))
    assert record.child_run_id is not None
    assert record.result == {"text": "private child notes"}
    parent_turns = asyncio.run(memory.read())
    assert not any(
        isinstance(message, AssistantMessage) and message.content == "private child notes"
        for turn in parent_turns
        for message in turn.messages
    )
    second_request = parent_model.requests[1]
    assert second_request.messages[-1].role == "tool"
    assert record.child_run_id in (second_request.messages[-1].content or "")


def test_submit_worker_returns_acceptance_before_one_completion_event(tmp_path: Path) -> None:
    """Asynchronous submission has an accepted Tool result and a separate event."""
    child_model = FakeOpenAIModel((text_batch("async child", "async-child"),))
    child_root = tmp_path / "child"
    child_root.mkdir()
    child_runner, child = make_runner(child_model, child_root)
    worker = Worker("worker-a", child, child_runner)
    supervisor = Supervisor((worker,))
    submit_tool = submit_worker(supervisor)
    wait_tool = wait_workers(supervisor)

    async def exercise() -> tuple[ToolResult, tuple[WorkerCompletedEvent, ToolResult]]:
        async with supervisor:
            accepted = await ToolRuntime().execute(
                submit_tool,
                ToolCall(
                    call_id="submit", tool_code="submit_worker", arguments={"worker_id": "worker-a", "task": "run"}
                ),
                ToolContext(call_id="submit", run_id="parent-run"),
            )
            assert isinstance(accepted, ToolResult)
            assert accepted.output is not None
            delegation_id = accepted.output["delegation_id"]
            assert isinstance(delegation_id, str)
            event = await supervisor.wait_worker(delegation_id)
            completed = await ToolRuntime().execute(
                wait_tool,
                ToolCall(call_id="wait", tool_code="wait_workers", arguments={"delegation_id": delegation_id}),
                ToolContext(call_id="wait", run_id="parent-run"),
            )
            assert isinstance(completed, ToolResult)
            return accepted, (event, completed)

    accepted, (event, completed) = asyncio.run(exercise())

    assert accepted.ok is True
    assert accepted.output is not None
    assert accepted.output["status"] == "accepted"
    assert event.status.value == "succeeded"
    assert event.child_run_id is not None
    assert completed.output is not None
    assert completed.output["event_id"] == event.event_id
    lifecycle = supervisor.events_since()
    assert tuple(item.kind for item in lifecycle) == (
        "worker.submitted",
        "worker.started",
        "worker.completed",
        "worker.consumed",
    )
    assert all(item.parent_run_id == "parent-run" for item in lifecycle)
    assert lifecycle[-1].child_run_id == event.child_run_id


def test_as_tool_resume_reuses_the_interrupted_child_run_and_child_memory(tmp_path: Path) -> None:
    """Resuming a parent delegation continues the original child checkpoint once."""
    parent_model = FakeOpenAIModel(
        (tool_batch("review_code", '{"task":"approve"}', "parent-call"), text_batch("parent resumed", "parent-final"))
    )
    child_model = FakeOpenAIModel(
        (tool_batch("echo", '{"value":"x"}', "call-1"), text_batch("child resumed", "child-final"))
    )
    parent_root = tmp_path / "parent"
    child_root = tmp_path / "child"
    parent_root.mkdir()
    child_root.mkdir()
    parent_runner, parent = make_runner(parent_model, parent_root)
    child_runner, child = make_runner(child_model, child_root, interrupting_tool)
    child = Agent(name="reviewer", model=child.model, prompt=child.prompt, tools=child.tools)
    worker = Worker("reviewer", child, child_runner)
    supervisor = Supervisor((worker,))
    delegation = as_tool(worker, name="review_code", description="Review code.", supervisor=supervisor)
    parent_runner.tools.register(delegation.descriptor, lambda selected: delegation)
    parent = Agent(
        name=parent.name,
        model=parent.model,
        prompt=parent.prompt,
        tools=(*parent.tools, ToolRef("review_code")),
    )
    memory = ShortMemory.in_memory("parent-resume")

    interrupted = asyncio.run(parent_runner.run(parent, "start", memory=memory, config=RunConfig()))
    assert interrupted.status is RunStatus.INTERRUPTED
    assert interrupted.checkpoint is not None
    delegation_id = next(iter(supervisor.records))
    child_run_id = supervisor.records[delegation_id].child_run_id
    assert child_run_id is not None
    now = datetime.now(UTC)
    response = InterruptionResponse(
        interruption_id=interrupted.interruptions[0].interruption_id,
        result=ToolResult(call_id="parent-call", ok=True, output={"approved": True}, started_at=now, finished_at=now),
    )

    resumed = asyncio.run(
        parent_runner.resume(
            parent,
            interrupted.checkpoint,
            memory=memory,
            resolutions=(response,),
            config=RunConfig(),
        )
    )

    assert resumed.status is RunStatus.SUCCEEDED
    assert resumed.final_output == "parent resumed"
    assert len(child_model.requests) == 2
    assert supervisor.records[delegation_id].child_run_id == child_run_id
    assert supervisor.records[delegation_id].result == {"text": "child resumed"}

    with pytest.raises(OrchestrationStateError, match="only resume from the interrupted state"):
        asyncio.run(
            delegation.resume_interruption(
                ToolContext(call_id="parent-call", run_id=interrupted.run_id),
                interrupted.interruptions[0],
                response,
            )
        )


def test_cancelled_synchronous_worker_cleans_child_memory_turn(tmp_path: Path) -> None:
    """Cancelling the owner task propagates and leaves no active child Turn."""
    model = BlockingOpenAIModel()
    child_root = tmp_path / "child"
    child_root.mkdir()
    child_runner, child = make_runner(model, child_root)
    worker = Worker("blocking", child, child_runner)
    supervisor = Supervisor((worker,))
    context = ToolContext(call_id="parent-call", run_id="parent-run")

    async def exercise() -> ShortMemory:
        task = asyncio.create_task(supervisor.run_worker("blocking", "wait", context))
        await model.started.wait()
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        memory = next(iter(supervisor.memories.values()))
        return memory

    memory = asyncio.run(exercise())

    assert asyncio.run(memory.active()) is None


def test_worker_child_config_intersects_parent_and_worker_grants(tmp_path: Path) -> None:
    """A Worker cannot regain a capability removed by its own Run configuration."""
    child_root = tmp_path / "child"
    child_root.mkdir()
    child_runner, child = make_runner(FakeOpenAIModel((text_batch("done", "child"),)), child_root)
    worker = Worker(
        "restricted",
        child,
        child_runner,
        config=RunConfig(grants=CapabilityGrants(capabilities=frozenset({"read", "command"}))),
    )
    context = ToolContext(
        call_id="parent-call",
        grants=CapabilityGrants(capabilities=frozenset({"read", "write"})),
    )

    config = worker.child_config(context)

    assert config.grants == CapabilityGrants(capabilities=frozenset({"read"}))


def test_worker_child_config_preserves_explicit_empty_grants(tmp_path: Path) -> None:
    """An explicit empty Worker grant remains empty under a permissive parent."""
    child_root = tmp_path / "child"
    child_root.mkdir()
    child_runner, child = make_runner(FakeOpenAIModel((text_batch("done", "child"),)), child_root)
    worker = Worker("restricted", child, child_runner, config=RunConfig(grants=CapabilityGrants()))
    context = ToolContext(
        call_id="parent-call",
        grants=CapabilityGrants(capabilities=frozenset({"read"})),
    )

    config = worker.child_config(context)

    assert config.grants == CapabilityGrants()


def test_worker_rejects_expired_parent_deadline_before_start(tmp_path: Path) -> None:
    """An expired parent deadline prevents the child model request from starting."""
    child_model = FakeOpenAIModel((text_batch("must not run", "child"),))
    child_root = tmp_path / "child"
    child_root.mkdir()
    child_runner, child = make_runner(child_model, child_root)
    supervisor = Supervisor((Worker("worker", child, child_runner),))
    context = ToolContext(
        call_id="parent-call",
        run_id="parent-run",
        parent_limits=ParentRunLimits(
            deadline=datetime.now(UTC) - timedelta(seconds=1),
            remaining_turns=1,
            remaining_output_tokens=1,
            tool_concurrency=1,
        ),
    )

    with pytest.raises(WorkerPolicyError, match="deadline expired"):
        asyncio.run(supervisor.run_worker("worker", "task", context))

    assert child_model.requests == []


def test_submit_worker_requires_structured_supervisor_scope(tmp_path: Path) -> None:
    """Asynchronous delegation cannot escape without a task owner."""
    child_root = tmp_path / "child"
    child_root.mkdir()
    child_runner, child = make_runner(FakeOpenAIModel((text_batch("done", "child"),)), child_root)
    supervisor = Supervisor((Worker("worker", child, child_runner),))

    with pytest.raises(OrchestrationConfigurationError, match="open Supervisor scope"):
        asyncio.run(
            supervisor.submit_worker(
                "worker",
                "task",
                ToolContext(call_id="parent-call", run_id="parent-run"),
            )
        )


def test_parallel_workers_share_and_settle_parent_budget(tmp_path: Path) -> None:
    """Concurrent submissions reserve disjoint shares and return confirmed unused budget."""
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    first_runner, first_agent = make_runner(FakeOpenAIModel((text_batch("first", "first"),)), first_root)
    second_runner, second_agent = make_runner(FakeOpenAIModel((text_batch("second", "second"),)), second_root)
    supervisor = Supervisor(
        (
            Worker("first", first_agent, first_runner, config=RunConfig(max_turns=2, max_output_tokens=10)),
            Worker("second", second_agent, second_runner, config=RunConfig(max_turns=2, max_output_tokens=10)),
        )
    )
    context = ToolContext(
        call_id="parent-call",
        run_id="parent-run",
        parent_limits=ParentRunLimits(
            deadline=datetime.now(UTC) + timedelta(minutes=1),
            remaining_turns=3,
            remaining_output_tokens=15,
            tool_concurrency=2,
        ),
    )

    async def exercise() -> tuple[tuple[int, ...], tuple[str, str]]:
        async with supervisor:
            first = await supervisor.submit_worker("first", "task", context)
            second = await supervisor.submit_worker("second", "task", context)
            reserved = tuple(sorted(item.turns for item in supervisor.budgets.reservations.values()))
            first_id = first["delegation_id"]
            second_id = second["delegation_id"]
            assert isinstance(first_id, str)
            assert isinstance(second_id, str)
            await supervisor.wait_worker(first_id)
            await supervisor.wait_worker(second_id)
            return reserved, (first_id, second_id)

    reserved, delegation_ids = asyncio.run(exercise())

    assert reserved == (1, 2)
    assert all(delegation_id not in supervisor.budgets.reservations for delegation_id in delegation_ids)
    assert supervisor.budgets.pools["parent-run"].remaining_turns == 1


@pytest.mark.parametrize("stale_context", [False, True])
def test_later_workers_share_only_output_budget_left_after_new_parent_consumption(
    tmp_path: Path, stale_context: bool
) -> None:
    model = FakeOpenAIModel(tuple(text_batch("done", f"child-{index}") for index in range(3)))
    runner, agent = make_runner(model, tmp_path)
    worker = Worker("worker", agent, runner, config=RunConfig(max_turns=2, max_output_tokens=10))
    supervisor = Supervisor((worker,))
    limits = ParentRunLimits(
        deadline=datetime.now(UTC) + timedelta(minutes=1),
        remaining_turns=10,
        remaining_output_tokens=20,
        tool_concurrency=1,
    )
    initial = ToolContext(call_id="first-call", run_id="parent", parent_limits=limits)
    later = initial.model_copy(
        update={"parent_limits": limits.model_copy(update={"remaining_turns": 8, "remaining_output_tokens": 6})}
    )

    async def scenario() -> None:
        for context in (initial, later, initial if stale_context else later):
            result = await supervisor.run_worker(worker, "work", context)
            assert result.status is RunStatus.SUCCEEDED

    asyncio.run(scenario())

    assert [request.max_completion_tokens for request in model.requests] == [10, 5, 4]


def test_later_worker_cannot_spend_turn_budget_consumed_by_parent_and_previous_child(tmp_path: Path) -> None:
    model = FakeOpenAIModel((text_batch("first", "first"), text_batch("must not run", "second")))
    runner, agent = make_runner(model, tmp_path)
    worker = Worker("worker", agent, runner, config=RunConfig(max_turns=2, max_output_tokens=10))
    supervisor = Supervisor((worker,))
    limits = ParentRunLimits(
        deadline=datetime.now(UTC) + timedelta(minutes=1),
        remaining_turns=4,
        remaining_output_tokens=20,
        tool_concurrency=1,
    )
    initial = ToolContext(call_id="first-call", run_id="parent", parent_limits=limits)
    later = initial.model_copy(update={"parent_limits": limits.model_copy(update={"remaining_turns": 1})})

    async def scenario() -> None:
        first = await supervisor.run_worker(worker, "first", initial)
        assert first.status is RunStatus.SUCCEEDED
        with pytest.raises(WorkerBudgetError):
            await supervisor.run_worker(worker, "must not run", later)

    asyncio.run(scenario())

    assert len(model.requests) == 1


def test_parent_consumption_during_reserved_worker_is_not_refunded_as_unused_child_budget(tmp_path: Path) -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    class GatedModel(FakeOpenAIModel):
        async def stream(self, request: OpenAIRequest | Mapping[str, object]) -> AsyncIterator[OpenAIStreamChunk]:
            started.set()
            await release.wait()
            async for chunk in super().stream(request):
                yield chunk

    first_model = GatedModel((text_batch("first", "first"),))
    first_root = tmp_path / "first"
    first_root.mkdir()
    first_runner, first_agent = make_runner(first_model, first_root)
    first_worker = Worker("first", first_agent, first_runner, config=RunConfig(max_turns=3, max_output_tokens=15))
    second_model = FakeOpenAIModel((text_batch("second", "second"),))
    second_root = tmp_path / "second"
    second_root.mkdir()
    second_runner, second_agent = make_runner(second_model, second_root)
    second_worker = Worker("second", second_agent, second_runner, config=RunConfig(max_turns=2, max_output_tokens=10))
    supervisor = Supervisor((first_worker, second_worker))
    limits = ParentRunLimits(
        deadline=datetime.now(UTC) + timedelta(minutes=1),
        remaining_turns=4,
        remaining_output_tokens=20,
        tool_concurrency=2,
    )
    initial = ToolContext(call_id="first-call", run_id="parent", parent_limits=limits)
    later = initial.model_copy(
        update={"parent_limits": limits.model_copy(update={"remaining_turns": 3, "remaining_output_tokens": 6})}
    )

    async def scenario() -> None:
        async with supervisor:
            accepted = await supervisor.submit_worker("first", "hold reservation", initial)
            await asyncio.wait_for(started.wait(), 1)
            with pytest.raises(WorkerBudgetError):
                await supervisor.run_worker(second_worker, "competing request", later)
            release.set()
            delegation_id = accepted["delegation_id"]
            assert isinstance(delegation_id, str)
            await supervisor.wait_worker(delegation_id)
            completed = await supervisor.run_worker(second_worker, "after settlement", later)
            assert completed.status is RunStatus.SUCCEEDED

    asyncio.run(scenario())

    assert len(second_model.requests) == 1
    assert second_model.requests[0].max_completion_tokens == 5


def test_worker_resume_rejects_missing_checkpoint(tmp_path: Path) -> None:
    """Damaged recovery state cannot be disguised as a successful parent response."""
    child_root = tmp_path / "child"
    child_root.mkdir()
    child_runner, child = make_runner(FakeOpenAIModel((text_batch("done", "child"),)), child_root)
    worker = Worker("worker", child, child_runner)
    supervisor = Supervisor((worker,))
    tool = DelegationTool(ToolDescriptor(name="delegate", code="delegate", description="Delegate."), worker, supervisor)
    interruption = ToolInterruption(
        call_id="parent-call",
        tool_code="delegate",
        tool_name="delegate",
        arguments={"task": "work"},
        reason="paused",
        details={"delegation_id": "delegation"},
    )
    now = datetime.now(UTC)
    response = InterruptionResponse(
        interruption_id=interruption.interruption_id,
        result=ToolResult(call_id="parent-call", ok=True, output={"approved": True}, started_at=now, finished_at=now),
    )

    with pytest.raises(OrchestrationStateError, match="missing its checkpoint"):
        asyncio.run(tool.resume_interruption(ToolContext(call_id="parent-call"), interruption, response))


def test_paused_worker_retains_budget_until_resumed_child_finishes(tmp_path: Path) -> None:
    model = FakeOpenAIModel((tool_batch("echo", '{"value":"x"}', "call-1"), text_batch("done", "child-final")))
    runner, agent = make_runner(model, tmp_path, interrupting_tool)
    worker = Worker("worker", agent, runner, config=RunConfig(max_turns=3, max_output_tokens=30))
    supervisor = Supervisor((worker,))
    tool = as_tool(worker, name="delegate", description="Delegate.", supervisor=supervisor)
    context = ToolContext(
        call_id="parent-call",
        run_id="parent-run",
        parent_limits=ParentRunLimits(
            deadline=datetime.now(UTC) + timedelta(minutes=1),
            remaining_turns=3,
            remaining_output_tokens=30,
            tool_concurrency=1,
        ),
    )

    async def exercise() -> ToolResult:
        paused = await tool.invoke(context, {"task": "work"})
        assert isinstance(paused, ToolInterruption)
        with pytest.raises(WorkerBudgetError):
            await supervisor.run_worker(worker, "competing task", context)
        now = datetime.now(UTC)
        response = InterruptionResponse(
            interruption_id=paused.interruption_id,
            result=ToolResult(
                call_id=context.call_id, ok=True, output={"approved": True}, started_at=now, finished_at=now
            ),
        )
        outcome = await tool.resume_interruption(context, paused, response)
        assert isinstance(outcome, ToolResult)
        return outcome

    result = asyncio.run(exercise())

    assert result.ok
    assert len(model.requests) == 2
    assert supervisor.budgets.reservations == {}
    assert supervisor.budgets.pools["parent-run"].remaining_turns == 1


def test_worker_does_not_refund_output_budget_when_provider_omits_usage(tmp_path: Path) -> None:
    model = FakeOpenAIModel((text_batch("done", "unknown"),), report_usage=False)
    runner, agent = make_runner(model, tmp_path)
    worker = Worker("worker", agent, runner, config=RunConfig(max_output_tokens=5))
    supervisor = Supervisor((worker,))
    context = ToolContext(
        call_id="parent-call",
        run_id="parent",
        parent_limits=ParentRunLimits(
            deadline=datetime.now(UTC) + timedelta(minutes=1),
            remaining_turns=3,
            remaining_output_tokens=5,
            tool_concurrency=1,
        ),
    )

    async def scenario() -> None:
        result = await supervisor.run_worker(worker, "work", context)
        assert result.status is RunStatus.SUCCEEDED
        assert result.usage.output_tokens is None
        assert result.usage.budget_output_tokens == 5
        assert supervisor.budgets.pools["parent"].remaining_output_tokens == 0
        with pytest.raises(WorkerBudgetError):
            await supervisor.run_worker(worker, "more", context)

    asyncio.run(scenario())


@pytest.mark.parametrize(("turns", "tokens"), [(2, 1), (1, 2)])
def test_saved_child_budget_rejects_allowance_larger_than_its_parent(turns: int, tokens: int) -> None:
    with pytest.raises(ValidationError, match="cannot exceed the parent allowance"):
        BudgetState(
            remaining_turns=turns,
            remaining_output_tokens=tokens,
            parent_remaining_turns=1,
            parent_remaining_output_tokens=1,
        )


def test_restored_child_budget_does_not_replenish_a_previously_consumed_allowance(tmp_path: Path) -> None:
    model = FakeOpenAIModel((text_batch("first", "first"), text_batch("second", "second")))
    runner, agent = make_runner(model, tmp_path)
    worker = Worker("worker", agent, runner, config=RunConfig(max_turns=3, max_output_tokens=10))
    first = Supervisor((worker,))
    context = ToolContext(
        call_id="delegate",
        run_id="parent",
        parent_limits=ParentRunLimits(
            deadline=datetime.now(UTC) + timedelta(minutes=1),
            remaining_turns=3,
            remaining_output_tokens=10,
            tool_concurrency=1,
        ),
    )

    async def scenario() -> None:
        await first.run_worker(worker, "first", context)
        saved = first.budgets.snapshot("parent")
        assert saved is not None
        restored = BudgetLedger()
        restored.restore("parent", BudgetState.model_validate_json(saved.model_dump_json()))
        second = Supervisor((worker,), budgets=restored)
        await second.run_worker(worker, "second", context)
        latest = restored.snapshot("parent")
        assert latest is not None
        restored.restore("parent", latest)
        with pytest.raises(OrchestrationStateError, match="differs from the live"):
            restored.restore("parent", saved)

    asyncio.run(scenario())

    assert [request.max_completion_tokens for request in model.requests] == [10, 9]


def test_child_budget_charges_reported_provider_overage_instead_of_forgiving_it() -> None:
    ledger = BudgetLedger()
    limits = ParentRunLimits(
        deadline=datetime.now(UTC) + timedelta(minutes=1),
        remaining_turns=2,
        remaining_output_tokens=100,
        tool_concurrency=1,
    )
    assert ledger.reserve("child", "parent", limits, 1, 50) is not None
    ledger.settle(
        "child", RunUsage(input_tokens=1, output_tokens=75, total_tokens=76, budget_output_tokens=75, model_calls=1)
    )
    saved = ledger.snapshot("parent")
    assert saved is not None and saved.remaining_output_tokens == 25


@pytest.mark.parametrize("kind", ["graph", "worker"])
@pytest.mark.parametrize("exhausted", ["turns", "output"])
def test_parent_final_allowance_does_not_fabricate_budget_for_graph_or_worker_children(
    tmp_path: Path,
    kind: str,
    exhausted: str,
) -> None:
    child_root = tmp_path / "child"
    child_root.mkdir()
    child_model = FakeOpenAIModel((text_batch("must not run", "child"),))
    child_runner, child_agent = make_runner(child_model, child_root)
    supervisor = Supervisor((Worker("child", child_agent, child_runner),))
    graph = Graph(graph_id="final-allowance", nodes=(Node("child", agent=child_agent, runner=child_runner),))
    contexts: list[ToolContext] = []
    accepted: list[bool] = []

    async def delegate(context: ToolContext, arguments: JsonObject) -> JsonObject:
        contexts.append(context)
        if kind == "graph":
            child = await GraphExecutor().execute(graph, {}, context=context)
            allowed = child.status is GraphStatus.SUCCEEDED
            accepted.append(allowed)
            return {"accepted": allowed, "error_code": child.error_code}
        try:
            await supervisor.run_worker("child", "delegate", context)
        except WorkerBudgetError as error:
            accepted.append(False)
            return {"accepted": False, "error_code": error.code}
        accepted.append(True)
        return {"accepted": True}

    parent_root = tmp_path / "parent"
    parent_root.mkdir()
    parent_model = FakeOpenAIModel((tool_batch("echo", '{"value":"delegate"}', "delegate"),))
    parent_runner, parent_agent = make_runner(parent_model, parent_root, delegate)
    config = RunConfig(max_turns=1 if exhausted == "turns" else 3, max_output_tokens=1 if exhausted == "output" else 10)
    result = asyncio.run(
        parent_runner.run(
            parent_agent, "Delegate at the boundary", memory=ShortMemory.in_memory("parent"), config=config
        )
    )

    assert result.status is RunStatus.LIMIT_REACHED
    assert len(contexts) == 1 and contexts[0].parent_limits is not None
    if exhausted == "turns":
        assert contexts[0].parent_limits.remaining_turns == 0
    else:
        assert contexts[0].parent_limits.remaining_output_tokens == 0
    assert accepted == [False]
    assert child_model.requests == []
