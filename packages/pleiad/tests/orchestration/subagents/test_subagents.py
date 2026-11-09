import asyncio
import json
from collections.abc import AsyncIterator, Mapping
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import TypeAdapter, ValidationError

import pleiad.runtime.runner as runner_module
from pleiad.agent.agent import Agent
from pleiad.budgets import BudgetLedger, BudgetState, BudgetStateError
from pleiad.contracts import CapabilityGrants, JsonObject, ToolRef
from pleiad.events.contracts import Event
from pleiad.events.runtime import ModelStepCompletedEvent
from pleiad.events.subagents import DelegationStatus, SubagentCompletedEvent, SubagentConsumedEvent
from pleiad.messages.message import AssistantMessage, ToolMessage
from pleiad.models.openai.request import OpenAIRequest
from pleiad.models.openai.response import (
    OpenAIStreamChoice,
    OpenAIStreamChunk,
    OpenAIStreamDelta,
    OpenAIStreamFunction,
    OpenAIStreamToolCall,
)
from pleiad.orchestration.errors import (
    OrchestrationConfigurationError,
    OrchestrationStateError,
    SubagentBudgetError,
    SubagentPolicyError,
)
from pleiad.orchestration.graph.executor import GraphExecutor
from pleiad.orchestration.graph.graph import Graph
from pleiad.orchestration.graph.node import Node
from pleiad.orchestration.graph.state import GraphStatus
from pleiad.orchestration.subagents.group import SubagentGroup
from pleiad.orchestration.subagents.subagent import Subagent
from pleiad.orchestration.subagents.tools import (
    DelegationTool,
    SubagentTaskArguments,
    as_tool,
    submit_subagent,
    wait_subagents,
)
from pleiad.runtime.checkpoint import ResumeDecision
from pleiad.runtime.limits import RunLimits
from pleiad.runtime.result import RunOutcome, RunResult, RunSuspension
from pleiad.sessions.backends.in_memory import InMemorySessionStore
from pleiad.sessions.checkpoint import RunCheckpoint
from pleiad.sessions.records import RunStatus
from pleiad.sessions.session import Session
from pleiad.sessions.usage import RunUsage
from pleiad.tools.base import FunctionTool
from pleiad.tools.contracts import ParentRunLimits, ToolCall, ToolContext, ToolDescriptor, ToolPolicy, ToolResult
from pleiad.tools.interruption import InterruptionResponse, ToolInterruption
from pleiad.tools.runtime import ToolRuntime
from tests.fakes.runtime import (
    BlockingOpenAIModel,
    FakeOpenAIModel,
    interrupting_tool,
    make_runner,
    text_batch,
)
from tests.runtime_support import resume_agent, run_agent, start_agent


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
    subagent = Subagent("reviewer", child, child_runner)
    group = SubagentGroup((subagent,))
    delegation = as_tool(subagent, name="review_code", description="Review code.", group=group)
    parent_runner.tools.register(delegation.descriptor, lambda selected: delegation)
    parent = Agent(
        name=parent.name, model=parent.model, prompt=parent.prompt, tools=(*parent.tools, ToolRef("review_code"))
    )

    memory = Session("parent-memory", InMemorySessionStore())
    result = asyncio.run(run_agent(parent_runner, parent, "start", session=memory, limits=RunLimits()))

    assert result.state is RunStatus.SUCCEEDED

    assert isinstance(result, RunResult)
    assert result.final_output == "parent complete"
    assert len(group.records) == 1
    record = next(iter(group.records.values()))
    assert record.child_run_id is not None
    assert record.result == {"text": "private child notes"}
    parent_steps = asyncio.run(memory.history())
    assert not any(
        isinstance(message, AssistantMessage) and message.content == "private child notes"
        for step in parent_steps
        for message in step.messages
    )
    second_request = parent_model.requests[1]
    assert second_request.messages[-1].role == "tool"
    assert record.child_run_id in (second_request.messages[-1].content or "")


def test_submit_subagent_returns_acceptance_before_one_completion_event(tmp_path: Path) -> None:
    """Asynchronous submission has an accepted Tool result and a separate event."""
    child_model = FakeOpenAIModel((text_batch("async child", "async-child"),))
    child_root = tmp_path / "child"
    child_root.mkdir()
    child_runner, child = make_runner(child_model, child_root)
    subagent = Subagent("subagent-a", child, child_runner)
    group = SubagentGroup((subagent,))
    submit_tool = submit_subagent(group)
    wait_tool = wait_subagents(group)

    async def exercise() -> tuple[ToolResult, tuple[SubagentCompletedEvent, ToolResult]]:
        async with group:
            accepted = await ToolRuntime().execute(
                submit_tool,
                ToolCall(
                    call_id="submit",
                    tool_code="submit_subagent",
                    arguments={"subagent_id": "subagent-a", "task": "run"},
                ),
                ToolContext(call_id="submit", run_id="parent-run"),
            )
            assert isinstance(accepted, ToolResult)
            assert accepted.output is not None
            delegation_id = accepted.output["delegation_id"]
            assert isinstance(delegation_id, str)
            event = await group.wait_subagent(delegation_id)
            completed = await ToolRuntime().execute(
                wait_tool,
                ToolCall(call_id="wait", tool_code="wait_subagents", arguments={"delegation_id": delegation_id}),
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
    lifecycle = group.events_since()
    assert tuple(item.kind for item in lifecycle) == (
        "subagent.submitted",
        "subagent.started",
        "subagent.completed",
        "subagent.consumed",
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
    subagent = Subagent("reviewer", child, child_runner)
    group = SubagentGroup((subagent,))
    delegation = as_tool(subagent, name="review_code", description="Review code.", group=group)
    parent_runner.tools.register(delegation.descriptor, lambda selected: delegation)
    parent = Agent(
        name=parent.name,
        model=parent.model,
        prompt=parent.prompt,
        tools=(*parent.tools, ToolRef("review_code")),
    )
    memory = Session("parent-resume", InMemorySessionStore())

    interrupted = asyncio.run(run_agent(parent_runner, parent, "start", session=memory, limits=RunLimits()))
    assert isinstance(interrupted, RunSuspension)
    assert isinstance(interrupted, RunSuspension)
    delegation_id = next(iter(group.records))
    child_run_id = group.records[delegation_id].child_run_id
    assert child_run_id is not None
    assert interrupted.usage.model_calls == 1 and interrupted.usage.child_model_calls == 1
    assert interrupted.usage.budget_output_tokens == 2
    now = datetime.now(UTC)
    child_checkpoint = RunCheckpoint.model_validate(interrupted.interruptions[0].details["checkpoint"])
    response = InterruptionResponse(
        interruption_id=interrupted.interruptions[0].interruption_id,
        result=ToolResult(
            call_id="parent-call",
            ok=True,
            output={
                "subagent_decisions": [
                    ResumeDecision(
                        interruption_id=child_checkpoint.interruptions[0].interruption_id,
                        result=ToolResult(
                            call_id=child_checkpoint.interruptions[0].call_id,
                            ok=True,
                            output={"approved": True},
                            started_at=now,
                            finished_at=now,
                        ),
                    ).model_dump(mode="json")
                ]
            },
            started_at=now,
            finished_at=now,
        ),
    )

    resumed = asyncio.run(
        resume_agent(
            parent_runner,
            parent,
            interrupted.checkpoint,
            session=memory,
            decisions=(ResumeDecision(interruption_id=response.interruption_id, result=response.result),),
            limits=RunLimits(),
        )
    )

    assert resumed.state is RunStatus.SUCCEEDED

    assert isinstance(resumed, RunResult)
    assert resumed.final_output == "parent resumed"
    assert resumed.usage.model_calls == 2 and resumed.usage.child_model_calls == 2
    assert resumed.usage.child_budget_output_tokens == 2 and resumed.usage.budget_output_tokens == 4
    assert len(child_model.requests) == 2
    assert group.records[delegation_id].child_run_id == child_run_id
    assert group.records[delegation_id].result == {"text": "child resumed"}

    with pytest.raises(OrchestrationStateError, match="only resume from the interrupted state"):
        asyncio.run(
            delegation.resume_interruption(
                ToolContext(call_id="parent-call", run_id=interrupted.run_id),
                interrupted.interruptions[0],
                response,
            )
        )


def test_cancelled_synchronous_subagent_cleans_child_memory_step(tmp_path: Path) -> None:
    """Cancelling the owner task propagates and leaves no active child ModelStep."""
    model = BlockingOpenAIModel()
    child_root = tmp_path / "child"
    child_root.mkdir()
    child_runner, child = make_runner(model, child_root)
    subagent = Subagent("blocking", child, child_runner)
    group = SubagentGroup((subagent,))
    context = ToolContext(call_id="parent-call", run_id="parent-run")

    async def exercise() -> Session:
        task = asyncio.create_task(group.run_subagent("blocking", "wait", context))
        await model.started.wait()
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        memory = next(iter(group.sessions.values()))
        return memory

    memory = asyncio.run(exercise())

    assert asyncio.run(memory.store.snapshot(memory.session_id)).current_run_id is None


def test_subagent_child_limits_intersects_parent_and_subagent_grants(tmp_path: Path) -> None:
    """A Subagent cannot regain a capability removed by its own Run configuration."""
    child_root = tmp_path / "child"
    child_root.mkdir()
    child_runner, child = make_runner(FakeOpenAIModel((text_batch("done", "child"),)), child_root)
    subagent = Subagent(
        "restricted",
        child,
        child_runner,
        limits=RunLimits(grants=CapabilityGrants(capabilities=frozenset({"read", "command"}))),
    )
    context = ToolContext(
        call_id="parent-call",
        grants=CapabilityGrants(capabilities=frozenset({"read", "write"})),
    )

    config = subagent.child_limits(context)

    assert config.grants == CapabilityGrants(capabilities=frozenset({"read"}))


def test_subagent_child_limits_preserves_explicit_empty_grants(tmp_path: Path) -> None:
    """An explicit empty Subagent grant remains empty under a permissive parent."""
    child_root = tmp_path / "child"
    child_root.mkdir()
    child_runner, child = make_runner(FakeOpenAIModel((text_batch("done", "child"),)), child_root)
    subagent = Subagent("restricted", child, child_runner, limits=RunLimits(grants=CapabilityGrants()))
    context = ToolContext(
        call_id="parent-call",
        grants=CapabilityGrants(capabilities=frozenset({"read"})),
    )

    config = subagent.child_limits(context)

    assert config.grants == CapabilityGrants()


def test_subagent_rejects_expired_parent_deadline_before_start(tmp_path: Path) -> None:
    """An expired parent deadline prevents the child model request from starting."""
    child_model = FakeOpenAIModel((text_batch("must not run", "child"),))
    child_root = tmp_path / "child"
    child_root.mkdir()
    child_runner, child = make_runner(child_model, child_root)
    group = SubagentGroup((Subagent("subagent", child, child_runner),))
    context = ToolContext(
        call_id="parent-call",
        run_id="parent-run",
        parent_limits=ParentRunLimits(
            deadline=datetime.now(UTC) - timedelta(seconds=1),
            remaining_model_steps=1,
            remaining_output_tokens=1,
            tool_concurrency=1,
        ),
    )

    with pytest.raises(SubagentPolicyError, match="deadline expired"):
        asyncio.run(group.run_subagent("subagent", "task", context))

    assert child_model.requests == []


def test_submit_subagent_requires_structured_group_scope(tmp_path: Path) -> None:
    """Asynchronous delegation cannot escape without a task owner."""
    child_root = tmp_path / "child"
    child_root.mkdir()
    child_runner, child = make_runner(FakeOpenAIModel((text_batch("done", "child"),)), child_root)
    group = SubagentGroup((Subagent("subagent", child, child_runner),))

    with pytest.raises(OrchestrationConfigurationError, match="open SubagentGroup scope"):
        asyncio.run(
            group.submit_subagent(
                "subagent",
                "task",
                ToolContext(call_id="parent-call", run_id="parent-run"),
            )
        )


def test_subagent_group_accepts_work_only_from_its_explicit_parent_run(tmp_path: Path) -> None:
    model = FakeOpenAIModel((text_batch("done", "child"),))
    runner, agent = make_runner(model, tmp_path)
    group = SubagentGroup((Subagent("child", agent, runner),))

    async def scenario() -> None:
        with pytest.raises(OrchestrationConfigurationError, match="explicit parent Run"):
            await group.run_subagent("child", "missing owner", ToolContext(call_id="missing"))
        result = await group.run_subagent("child", "owned work", ToolContext(call_id="owned", run_id="parent"))
        assert isinstance(result, RunResult) and result.state is RunStatus.SUCCEEDED
        with pytest.raises(OrchestrationStateError, match="another parent Run"):
            await group.run_subagent("child", "wrong owner", ToolContext(call_id="wrong", run_id="other"))
        assert len(model.requests) == 1

    asyncio.run(scenario())


def test_parallel_subagents_share_and_settle_parent_budget(tmp_path: Path) -> None:
    """Concurrent submissions reserve disjoint shares and return confirmed unused budget."""
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    first_runner, first_agent = make_runner(FakeOpenAIModel((text_batch("first", "first"),)), first_root)
    second_runner, second_agent = make_runner(FakeOpenAIModel((text_batch("second", "second"),)), second_root)
    group = SubagentGroup(
        (
            Subagent("first", first_agent, first_runner, limits=RunLimits(max_model_steps=2, max_output_tokens=10)),
            Subagent("second", second_agent, second_runner, limits=RunLimits(max_model_steps=2, max_output_tokens=10)),
        )
    )
    context = ToolContext(
        call_id="parent-call",
        run_id="parent-run",
        parent_limits=ParentRunLimits(
            deadline=datetime.now(UTC) + timedelta(minutes=1),
            remaining_model_steps=3,
            remaining_output_tokens=15,
            tool_concurrency=2,
        ),
    )

    async def exercise() -> tuple[tuple[int, ...], tuple[str, str]]:
        async with group:
            first = await group.submit_subagent("first", "task", context)
            second = await group.submit_subagent("second", "task", context)
            reserved = tuple(sorted(item.model_steps for item in group.budgets.reservations.values()))
            first_id = first["delegation_id"]
            second_id = second["delegation_id"]
            assert isinstance(first_id, str)
            assert isinstance(second_id, str)
            await group.wait_subagent(first_id)
            await group.wait_subagent(second_id)
            return reserved, (first_id, second_id)

    reserved, delegation_ids = asyncio.run(exercise())

    assert reserved == (1, 2)
    assert all(delegation_id not in group.budgets.reservations for delegation_id in delegation_ids)
    assert group.budgets.pools["parent-run"].remaining_model_steps == 1


@pytest.mark.parametrize("stale_context", [False, True])
def test_later_subagents_share_only_output_budget_left_after_new_parent_consumption(
    tmp_path: Path, stale_context: bool
) -> None:
    model = FakeOpenAIModel(tuple(text_batch("done", f"child-{index}") for index in range(3)))
    runner, agent = make_runner(model, tmp_path)
    subagent = Subagent("subagent", agent, runner, limits=RunLimits(max_model_steps=2, max_output_tokens=10))
    group = SubagentGroup((subagent,))
    limits = ParentRunLimits(
        deadline=datetime.now(UTC) + timedelta(minutes=1),
        remaining_model_steps=10,
        remaining_output_tokens=20,
        tool_concurrency=1,
    )
    initial = ToolContext(call_id="first-call", run_id="parent", parent_limits=limits)
    later = initial.model_copy(
        update={"parent_limits": limits.model_copy(update={"remaining_model_steps": 8, "remaining_output_tokens": 6})}
    )

    async def scenario() -> None:
        for context in (initial, later, initial if stale_context else later):
            result = await group.run_subagent(subagent, "work", context)
            assert result.state is RunStatus.SUCCEEDED
            assert isinstance(result, RunResult)

    asyncio.run(scenario())

    assert [request.max_completion_tokens for request in model.requests] == [10, 5, 4]


def test_later_subagent_cannot_spend_step_budget_consumed_by_parent_and_previous_child(tmp_path: Path) -> None:
    model = FakeOpenAIModel((text_batch("first", "first"), text_batch("must not run", "second")))
    runner, agent = make_runner(model, tmp_path)
    subagent = Subagent("subagent", agent, runner, limits=RunLimits(max_model_steps=2, max_output_tokens=10))
    group = SubagentGroup((subagent,))
    limits = ParentRunLimits(
        deadline=datetime.now(UTC) + timedelta(minutes=1),
        remaining_model_steps=4,
        remaining_output_tokens=20,
        tool_concurrency=1,
    )
    initial = ToolContext(call_id="first-call", run_id="parent", parent_limits=limits)
    later = initial.model_copy(update={"parent_limits": limits.model_copy(update={"remaining_model_steps": 1})})

    async def scenario() -> None:
        first = await group.run_subagent(subagent, "first", initial)
        assert first.state is RunStatus.SUCCEEDED
        with pytest.raises(SubagentBudgetError):
            await group.run_subagent(subagent, "must not run", later)

    asyncio.run(scenario())

    assert len(model.requests) == 1


def test_parent_consumption_during_reserved_subagent_is_not_refunded_as_unused_child_budget(tmp_path: Path) -> None:
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
    first_subagent = Subagent(
        "first", first_agent, first_runner, limits=RunLimits(max_model_steps=3, max_output_tokens=15)
    )
    second_model = FakeOpenAIModel((text_batch("second", "second"),))
    second_root = tmp_path / "second"
    second_root.mkdir()
    second_runner, second_agent = make_runner(second_model, second_root)
    second_subagent = Subagent(
        "second", second_agent, second_runner, limits=RunLimits(max_model_steps=2, max_output_tokens=10)
    )
    group = SubagentGroup((first_subagent, second_subagent))
    limits = ParentRunLimits(
        deadline=datetime.now(UTC) + timedelta(minutes=1),
        remaining_model_steps=4,
        remaining_output_tokens=20,
        tool_concurrency=2,
    )
    initial = ToolContext(call_id="first-call", run_id="parent", parent_limits=limits)
    later = initial.model_copy(
        update={"parent_limits": limits.model_copy(update={"remaining_model_steps": 3, "remaining_output_tokens": 6})}
    )

    async def scenario() -> None:
        async with group:
            accepted = await group.submit_subagent("first", "hold reservation", initial)
            await asyncio.wait_for(started.wait(), 1)
            with pytest.raises(SubagentBudgetError):
                await group.run_subagent(second_subagent, "competing request", later)
            release.set()
            delegation_id = accepted["delegation_id"]
            assert isinstance(delegation_id, str)
            await group.wait_subagent(delegation_id)
            completed = await group.run_subagent(second_subagent, "after settlement", later)
            assert completed.state is RunStatus.SUCCEEDED

    asyncio.run(scenario())

    assert len(second_model.requests) == 1
    assert second_model.requests[0].max_completion_tokens == 5


def test_subagent_resume_rejects_missing_checkpoint(tmp_path: Path) -> None:
    """Damaged recovery state cannot be disguised as a successful parent response."""
    child_root = tmp_path / "child"
    child_root.mkdir()
    child_runner, child = make_runner(FakeOpenAIModel((text_batch("done", "child"),)), child_root)
    subagent = Subagent("subagent", child, child_runner)
    group = SubagentGroup((subagent,))
    tool = DelegationTool(ToolDescriptor(name="delegate", code="delegate", description="Delegate."), subagent, group)
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


def test_paused_subagent_retains_budget_until_resumed_child_finishes(tmp_path: Path) -> None:
    model = FakeOpenAIModel((tool_batch("echo", '{"value":"x"}', "call-1"), text_batch("done", "child-final")))
    runner, agent = make_runner(model, tmp_path, interrupting_tool)
    subagent = Subagent("subagent", agent, runner, limits=RunLimits(max_model_steps=3, max_output_tokens=30))
    group = SubagentGroup((subagent,))
    tool = as_tool(subagent, name="delegate", description="Delegate.", group=group)
    context = ToolContext(
        call_id="parent-call",
        run_id="parent-run",
        parent_limits=ParentRunLimits(
            deadline=datetime.now(UTC) + timedelta(minutes=1),
            remaining_model_steps=3,
            remaining_output_tokens=30,
            tool_concurrency=1,
        ),
    )

    async def exercise() -> ToolResult:
        paused = await tool.invoke(context, {"task": "work"})
        assert isinstance(paused, ToolInterruption)
        with pytest.raises(SubagentBudgetError):
            await group.run_subagent(subagent, "competing task", context)
        now = datetime.now(UTC)
        child_checkpoint = RunCheckpoint.model_validate(paused.details["checkpoint"])
        response = InterruptionResponse(
            interruption_id=paused.interruption_id,
            result=ToolResult(
                call_id=context.call_id,
                ok=True,
                output={
                    "subagent_decisions": [
                        ResumeDecision(
                            interruption_id=child_checkpoint.interruptions[0].interruption_id,
                            result=ToolResult(
                                call_id=child_checkpoint.interruptions[0].call_id,
                                ok=True,
                                output={"approved": True},
                                started_at=now,
                                finished_at=now,
                            ),
                        ).model_dump(mode="json")
                    ]
                },
                started_at=now,
                finished_at=now,
            ),
        )
        restored_group = SubagentGroup((subagent,))
        restored_tool = as_tool(subagent, name="delegate", description="Delegate.", group=restored_group)
        outcome = await restored_tool.resume_interruption(context, paused, response)
        assert isinstance(outcome, ToolResult)
        assert restored_group.budgets.reservations == {}
        assert restored_group.budgets.pools["parent-run"].remaining_model_steps == 1
        return outcome

    result = asyncio.run(exercise())

    assert result.ok
    assert len(model.requests) == 2


def test_subagent_does_not_refund_output_budget_when_provider_omits_usage(tmp_path: Path) -> None:
    model = FakeOpenAIModel((text_batch("done", "unknown"),), report_usage=False)
    runner, agent = make_runner(model, tmp_path)
    subagent = Subagent("subagent", agent, runner, limits=RunLimits(max_output_tokens=5))
    group = SubagentGroup((subagent,))
    context = ToolContext(
        call_id="parent-call",
        run_id="parent",
        parent_limits=ParentRunLimits(
            deadline=datetime.now(UTC) + timedelta(minutes=1),
            remaining_model_steps=3,
            remaining_output_tokens=5,
            tool_concurrency=1,
        ),
    )

    async def scenario() -> None:
        result = await group.run_subagent(subagent, "work", context)
        assert result.state is RunStatus.SUCCEEDED
        assert isinstance(result, RunResult)
        assert result.usage.output_tokens is None
        assert result.usage.budget_output_tokens == 5
        assert group.budgets.pools["parent"].remaining_output_tokens == 0
        with pytest.raises(SubagentBudgetError):
            await group.run_subagent(subagent, "more", context)

    asyncio.run(scenario())


@pytest.mark.parametrize(("steps", "tokens"), [(2, 1), (1, 2)])
def test_saved_child_budget_rejects_allowance_larger_than_its_parent(steps: int, tokens: int) -> None:
    with pytest.raises(ValidationError, match="cannot exceed the parent allowance"):
        BudgetState(
            remaining_model_steps=steps,
            remaining_output_tokens=tokens,
            parent_remaining_model_steps=1,
            parent_remaining_output_tokens=1,
        )


def test_restored_child_budget_does_not_replenish_a_previously_consumed_allowance(tmp_path: Path) -> None:
    model = FakeOpenAIModel((text_batch("first", "first"), text_batch("second", "second")))
    runner, agent = make_runner(model, tmp_path)
    subagent = Subagent("subagent", agent, runner, limits=RunLimits(max_model_steps=3, max_output_tokens=10))
    first = SubagentGroup((subagent,))
    context = ToolContext(
        call_id="delegate",
        run_id="parent",
        parent_limits=ParentRunLimits(
            deadline=datetime.now(UTC) + timedelta(minutes=1),
            remaining_model_steps=3,
            remaining_output_tokens=10,
            tool_concurrency=1,
        ),
    )

    async def scenario() -> None:
        await first.run_subagent(subagent, "first", context)
        saved = first.budgets.snapshot("parent")
        assert saved is not None
        restored = BudgetLedger()
        restored.restore("parent", BudgetState.model_validate_json(saved.model_dump_json()))
        second = SubagentGroup((subagent,), budgets=restored)
        await second.run_subagent(subagent, "second", context)
        latest = restored.snapshot("parent")
        assert latest is not None
        restored.restore("parent", latest)
        with pytest.raises(BudgetStateError, match="differs from the live"):
            restored.restore("parent", saved)

    asyncio.run(scenario())

    assert [request.max_completion_tokens for request in model.requests] == [10, 9]


def test_child_budget_charges_reported_provider_overage_instead_of_forgiving_it() -> None:
    ledger = BudgetLedger()
    limits = ParentRunLimits(
        deadline=datetime.now(UTC) + timedelta(minutes=1),
        remaining_model_steps=2,
        remaining_output_tokens=100,
        tool_concurrency=1,
    )
    assert ledger.reserve("child", "parent", limits, 1, 50) is not None
    ledger.settle(
        "child",
        RunUsage(input_tokens=1, output_tokens=75, total_tokens=76, budget_output_tokens=75, model_calls=1).charge(
            "parent", "child"
        ),
    )
    saved = ledger.snapshot("parent")
    assert saved is not None and saved.remaining_output_tokens == 25


@pytest.mark.parametrize("kind", ["graph", "subagent"])
@pytest.mark.parametrize("exhausted", ["steps", "output"])
def test_parent_final_allowance_does_not_fabricate_budget_for_graph_or_subagent_children(
    tmp_path: Path,
    kind: str,
    exhausted: str,
) -> None:
    child_root = tmp_path / "child"
    child_root.mkdir()
    child_model = FakeOpenAIModel((text_batch("must not run", "child"),))
    child_runner, child_agent = make_runner(child_model, child_root)
    group = SubagentGroup((Subagent("child", child_agent, child_runner),))
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
            await group.run_subagent("child", "delegate", context)
        except SubagentBudgetError as error:
            accepted.append(False)
            return {"accepted": False, "error_code": error.code}
        accepted.append(True)
        return {"accepted": True}

    parent_root = tmp_path / "parent"
    parent_root.mkdir()
    parent_model = FakeOpenAIModel((tool_batch("echo", '{"value":"delegate"}', "delegate"),))
    parent_runner, parent_agent = make_runner(parent_model, parent_root, delegate)
    config = RunLimits(
        max_model_steps=1 if exhausted == "steps" else 3, max_output_tokens=1 if exhausted == "output" else 10
    )
    result = asyncio.run(
        run_agent(
            parent_runner,
            parent_agent,
            "Delegate at the boundary",
            session=Session("parent", InMemorySessionStore()),
            limits=config,
        )
    )

    assert result.state is RunStatus.FAILED
    assert len(contexts) == 1 and contexts[0].parent_limits is not None
    if exhausted == "steps":
        assert contexts[0].parent_limits.remaining_model_steps == 0
    else:
        assert contexts[0].parent_limits.remaining_output_tokens == 0
    assert accepted == [False]
    assert child_model.requests == []


@pytest.mark.parametrize("budgeted", [True, False], ids=["parent-budget", "host-limits-only"])
def test_subagent_resume_binds_multiple_explicit_decisions_to_their_original_child_calls(
    tmp_path: Path, budgeted: bool
) -> None:
    async def ask(context: ToolContext, arguments: JsonObject) -> ToolInterruption:
        return ToolInterruption(
            interruption_id=f"question-{context.call_id}",
            call_id=context.call_id,
            tool_code="echo",
            tool_name="echo",
            arguments=arguments,
            reason="Host answer required",
        )

    calls = tuple(
        OpenAIStreamToolCall(
            index=index, id=identity, function=OpenAIStreamFunction(name="echo", arguments='{"value":"x"}')
        )
        for index, identity in enumerate(("first", "second"))
    )
    batch = (
        OpenAIStreamChunk(
            id="two-calls",
            model="fake-model",
            choices=(OpenAIStreamChoice(delta=OpenAIStreamDelta(tool_calls=calls), finish_reason="tool_calls"),),
        ),
    )
    model = FakeOpenAIModel((batch, text_batch("Both decisions accepted", "final")))
    runner, agent = make_runner(model, tmp_path, ask)
    subagent = Subagent("child", agent, runner)
    group = SubagentGroup((subagent,))
    tool = as_tool(subagent, name="delegate", description="Delegate.", group=group)
    context = ToolContext(
        call_id="parent-call",
        run_id="parent",
        parent_limits=ParentRunLimits(
            deadline=datetime.now(UTC) + timedelta(minutes=1),
            remaining_model_steps=4,
            remaining_output_tokens=40,
            tool_concurrency=2,
        ),
    )

    if not budgeted:
        context = context.model_copy(update={"parent_limits": None})

    async def scenario() -> None:
        paused = await tool.invoke(context, {"task": "Ask both questions"})
        assert isinstance(paused, ToolInterruption) and paused.details["kind"] == "pleiad.subagent"
        checkpoint = RunCheckpoint.model_validate(paused.details["checkpoint"])
        assert len(checkpoint.interruptions) == 2
        now = datetime.now(UTC)
        ambiguous = InterruptionResponse(
            interruption_id=paused.interruption_id,
            result=ToolResult(
                call_id="parent-call", ok=True, output={"approved": True}, started_at=now, finished_at=now
            ),
        )
        with pytest.raises(OrchestrationStateError, match="explicit child decisions"):
            await tool.resume_interruption(context, paused, ambiguous)
        decisions = [
            ResumeDecision(
                interruption_id=value.interruption_id,
                result=ToolResult(
                    call_id=value.call_id, ok=True, output={"answer": value.call_id}, started_at=now, finished_at=now
                ),
            ).model_dump(mode="json")
            for value in reversed(checkpoint.interruptions)
        ]
        partial = ambiguous.model_copy(
            update={"result": ambiguous.result.model_copy(update={"output": {"subagent_decisions": decisions[:1]}})}
        )
        remaining = await tool.resume_interruption(context, paused, partial)
        assert isinstance(remaining, ToolInterruption) and remaining.details["kind"] == "pleiad.subagent"
        replacement = RunCheckpoint.model_validate(remaining.details["checkpoint"])
        assert len(replacement.interruptions) == 1 and replacement.run_id == checkpoint.run_id
        assert len(model.requests) == 1
        explicit = InterruptionResponse(
            interruption_id=remaining.interruption_id,
            result=ambiguous.result.model_copy(update={"output": {"subagent_decisions": decisions[1:]}}),
        )
        outcome = await tool.resume_interruption(context, remaining, explicit)
        assert isinstance(outcome, ToolResult) and outcome.ok
        record = next(iter(group.records.values()))
        assert record.child_run_id == checkpoint.run_id
        session = group.sessions[record.delegation_id]
        messages = [
            message for step in await session.history() for message in step.messages if isinstance(message, ToolMessage)
        ]
        assert [message.tool_call_id for message in messages] == ["first", "second"]
        for message in messages:
            result = TypeAdapter[JsonObject](JsonObject).validate_json(message.content)
            output = result["output"]
            assert isinstance(output, dict) and output["answer"] == message.tool_call_id
        assert len(model.requests) == 2
        if not budgeted:
            assert group.budgets.pools == {} and group.budgets.reservations == {}

    asyncio.run(scenario())


@pytest.mark.parametrize("budgeted", [True, False], ids=["parent-budget", "host-limits-only"])
def test_subagent_cancellation_reopens_its_original_child_session_and_refunds_once(
    tmp_path: Path, budgeted: bool
) -> None:
    model = FakeOpenAIModel((tool_batch("echo", '{"value":"x"}', "child-call"),))
    runner, agent = make_runner(model, tmp_path, interrupting_tool)
    subagent = Subagent("child", agent, runner, limits=RunLimits(max_model_steps=3, max_output_tokens=30))
    group = SubagentGroup((subagent,))
    tool = as_tool(subagent, name="delegate", description="Delegate.", group=group)
    context = ToolContext(
        call_id="parent-call",
        run_id="parent",
        parent_limits=ParentRunLimits(
            deadline=datetime.now(UTC) + timedelta(minutes=1),
            remaining_model_steps=3,
            remaining_output_tokens=30,
            tool_concurrency=1,
        ),
    )

    if not budgeted:
        context = context.model_copy(update={"parent_limits": None})

    async def scenario() -> None:
        paused = await tool.invoke(context, {"task": "Wait for the host"})
        assert isinstance(paused, ToolInterruption)
        original = group.snapshot()
        for same_owner in (False, True, False):
            owner = group if same_owner else SubagentGroup((subagent,))
            if not same_owner:
                owner.restore(original)
            result = await owner.cancel_paused(
                reason="host_cancelled", context=ToolContext(call_id="cancel", run_id="parent")
            )
            if budgeted:
                assert result.budget is not None and result.budget.reservations == ()
                assert result.budget.state.remaining_model_steps == 2
            else:
                assert result.budget is None and owner.budgets.pools == {} and owner.budgets.reservations == {}
            assert result.delegations[0].status is DelegationStatus.CANCELLED
            child_id = result.delegations[0].child_run_id
            assert child_id is not None
            checkpoint = RunCheckpoint.model_validate(paused.details["checkpoint"])
            snapshot = await subagent.session_store.run(checkpoint.session_id, child_id)
            assert snapshot.state is RunStatus.CANCELLED
            repeated = await owner.cancel_paused(reason="host_cancelled")
            assert repeated == result
        assert len(model.requests) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("step_limit", [1, 2, 3])
def test_parent_model_step_limit_includes_its_completed_scoped_child(tmp_path: Path, step_limit: int) -> None:
    async def scenario() -> None:
        parent_root, child_root = tmp_path / "parent", tmp_path / "child"
        parent_root.mkdir()
        child_root.mkdir()
        parent_model = FakeOpenAIModel(
            (tool_batch("consult", '{"task":"review"}', "consult-call"), text_batch("Done", "final"))
        )
        child_model = FakeOpenAIModel((text_batch("Evidence", "child"),))
        parent_runner, parent = make_runner(parent_model, parent_root)
        child_runner, child = make_runner(child_model, child_root)
        group = SubagentGroup(
            (Subagent("child", child, child_runner, limits=RunLimits(max_model_steps=1, max_output_tokens=2)),)
        )
        tool = as_tool(group.subagents["child"], name="consult", description="Consult.", group=group)
        parent_runner.tools.register(tool.descriptor, lambda selected: tool)
        parent = Agent(
            name=parent.name, model=parent.model, prompt=parent.prompt, tools=(*parent.tools, ToolRef("consult"))
        )
        result = await run_agent(
            parent_runner, parent, "Review", limits=RunLimits(max_model_steps=step_limit, max_output_tokens=8)
        )
        assert isinstance(result, RunResult)
        assert result.state is (RunStatus.FAILED if step_limit <= 2 else RunStatus.SUCCEEDED)
        assert result.usage.model_calls == (1 if step_limit <= 2 else 2)
        assert result.usage.child_model_calls == (0 if step_limit == 1 else 1)
        assert result.usage.budget_output_tokens == step_limit
        assert result.usage.output_tokens == result.usage.model_calls
        assert len(parent_model.requests) == max(1, step_limit - 1)
        assert len(child_model.requests) == (0 if step_limit == 1 else 1)

    asyncio.run(scenario())


@pytest.mark.parametrize("reported", [True, False], ids=["reported-child-cost", "unknown-child-cost"])
def test_parent_output_admission_charges_scoped_children_without_merging_their_measurements(
    tmp_path: Path, reported: bool
) -> None:
    async def scenario() -> None:
        parent_root, child_root = tmp_path / "parent", tmp_path / "child"
        parent_root.mkdir()
        child_root.mkdir()
        parent_model = FakeOpenAIModel(
            (tool_batch("consult", '{"task":"review"}', "consult-call"), text_batch("Done", "final"))
        )
        child_model = FakeOpenAIModel((text_batch("Evidence", "child"),), report_usage=reported)
        parent_runner, parent = make_runner(parent_model, parent_root)
        child_runner, child = make_runner(child_model, child_root)
        subagent = Subagent("child", child, child_runner, limits=RunLimits(max_model_steps=1, max_output_tokens=2))
        group = SubagentGroup((subagent,))
        tool = as_tool(subagent, name="consult", description="Consult.", group=group)
        parent_runner.tools.register(tool.descriptor, lambda selected: tool)
        parent = Agent(
            name=parent.name, model=parent.model, prompt=parent.prompt, tools=(*parent.tools, ToolRef("consult"))
        )
        result = await run_agent(
            parent_runner, parent, "Review", limits=RunLimits(max_model_steps=3, max_output_tokens=4)
        )
        assert isinstance(result, RunResult) and result.state is RunStatus.SUCCEEDED
        child_charge = 1 if reported else 2
        assert result.usage.model_calls == 2 and result.usage.output_tokens == 2
        assert result.usage.child_model_calls == 1 and result.usage.child_budget_output_tokens == child_charge
        assert result.usage.budget_output_tokens == 2 + child_charge
        assert [value.max_completion_tokens for value in parent_model.requests] == [4, 3 - child_charge]
        assert group.budgets.pools[result.run_id].remaining_output_tokens == 2 - child_charge

    asyncio.run(scenario())


def test_parent_without_wait_keeps_async_child_reservations_until_actual_settlement(tmp_path: Path) -> None:
    async def scenario() -> None:
        started, release, parent_ready = asyncio.Event(), asyncio.Event(), asyncio.Event()

        class GatedModel(FakeOpenAIModel):
            async def stream(self, request: OpenAIRequest | Mapping[str, object]) -> AsyncIterator[OpenAIStreamChunk]:
                started.set()
                await release.wait()
                async for chunk in super().stream(request):
                    yield chunk

        parent_root, child_root = tmp_path / "parent", tmp_path / "child"
        parent_root.mkdir()
        child_root.mkdir()
        parent_model = FakeOpenAIModel(
            (
                tool_batch("submit_subagent", '{"subagent_id":"child","task":"review"}', "submit"),
                text_batch("Parent ready", "final"),
            )
        )
        child_model = GatedModel((text_batch("Child settled", "child"),))
        parent_runner, parent = make_runner(parent_model, parent_root)
        child_runner, child = make_runner(child_model, child_root)
        group = SubagentGroup(
            (Subagent("child", child, child_runner, limits=RunLimits(max_model_steps=1, max_output_tokens=2)),)
        )
        submission = submit_subagent(group)
        parent_runner.tools.register(submission.descriptor, lambda selected: submission)
        parent = Agent(
            name=parent.name,
            model=parent.model,
            prompt=parent.prompt,
            tools=(*parent.tools, ToolRef(submission.descriptor.code)),
        )
        completed = 0

        async def observe(event: Event) -> None:
            nonlocal completed
            if isinstance(event, ModelStepCompletedEvent):
                completed += 1
                if completed == 2:
                    parent_ready.set()

        parent_runner.event_sink = observe
        handle = await start_agent(
            parent_runner, parent, "Review", limits=RunLimits(max_model_steps=4, max_output_tokens=6)
        )
        async with handle:
            await asyncio.wait_for(started.wait(), 3)
            await asyncio.wait_for(parent_ready.wait(), 3)
            assert parent_model.requests[1].max_completion_tokens == 3
            waiting = asyncio.create_task(handle.result())
            persisted = await handle.session.store.run(handle.session.session_id, handle.run_id)
            assert persisted.receipt is None and persisted.state is RunStatus.RUNNING
            assert not waiting.done()
            assert len(group.budgets.reservations) == 1
            release.set()
            result = await waiting
        assert isinstance(result, RunResult) and result.state is RunStatus.SUCCEEDED
        assert result.usage.model_calls == 2 and result.usage.child_model_calls == 1
        assert result.usage.budget_output_tokens == 3 and result.usage.child_budget_output_tokens == 1
        assert group.budgets.reservations == {} and group.task_group is None
        assert len(child_model.requests) == 1

    asyncio.run(scenario())


def test_repeated_async_wait_consumes_the_same_child_charge_once(tmp_path: Path) -> None:
    class WaitingParentModel(FakeOpenAIModel):
        async def stream(self, request: OpenAIRequest | Mapping[str, object]) -> AsyncIterator[OpenAIStreamChunk]:
            if not isinstance(request, OpenAIRequest):
                raise TypeError("The fake provider requires the parsed public request")
            if len(self.requests) in {1, 2}:
                tool_message = next(message for message in request.messages if message.role == "tool")
                assert isinstance(tool_message.content, str)
                accepted = TypeAdapter[JsonObject](JsonObject).validate_json(tool_message.content)
                output = accepted["output"]
                assert isinstance(output, dict) and isinstance(output["delegation_id"], str)
                self.batches = iter(
                    (
                        tool_batch(
                            "wait_subagents",
                            json.dumps({"delegation_id": output["delegation_id"]}),
                            f"wait-{len(self.requests)}",
                        ),
                    )
                )
            elif len(self.requests) == 3:
                self.batches = iter((text_batch("Done", "final"),))
            async for chunk in super().stream(request):
                yield chunk

    async def scenario() -> None:
        parent_root, child_root = tmp_path / "parent", tmp_path / "child"
        parent_root.mkdir()
        child_root.mkdir()
        parent_model = WaitingParentModel(
            (tool_batch("submit_subagent", '{"subagent_id":"child","task":"review"}', "submit"),)
        )
        child_model = FakeOpenAIModel((text_batch("Evidence", "child"),))
        parent_runner, parent = make_runner(parent_model, parent_root)
        child_runner, child = make_runner(child_model, child_root)
        group = SubagentGroup(
            (Subagent("child", child, child_runner, limits=RunLimits(max_model_steps=1, max_output_tokens=2)),)
        )
        submission, waiting = submit_subagent(group), wait_subagents(group)
        parent_runner.tools.register(submission.descriptor, lambda selected: submission)
        parent_runner.tools.register(waiting.descriptor, lambda selected: waiting)
        parent = Agent(
            name=parent.name,
            model=parent.model,
            prompt=parent.prompt,
            tools=(*parent.tools, ToolRef(submission.descriptor.code), ToolRef(waiting.descriptor.code)),
        )
        result = await run_agent(
            parent_runner, parent, "Review", limits=RunLimits(max_model_steps=5, max_output_tokens=10)
        )
        assert isinstance(result, RunResult) and result.state is RunStatus.SUCCEEDED
        assert result.usage.model_calls == 4 and result.usage.child_model_calls == 1
        assert result.usage.budget_output_tokens == 5 and result.usage.child_budget_output_tokens == 1
        assert len(group.budgets.usage_charges(result.run_id)) == 1 and len(child_model.requests) == 1
        assert sum(isinstance(value, SubagentConsumedEvent) for value in group.events) == 1

    asyncio.run(scenario())


def test_parallel_graph_and_subagent_share_admission_and_charge_each_actual_child_once(tmp_path: Path) -> None:
    async def scenario() -> None:
        graph_started, subagent_started, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

        class GatedModel(FakeOpenAIModel):
            def __init__(self, signal: asyncio.Event) -> None:
                super().__init__((text_batch("Evidence", "child"),))
                self.signal = signal

            async def stream(self, request: OpenAIRequest | Mapping[str, object]) -> AsyncIterator[OpenAIStreamChunk]:
                self.signal.set()
                await release.wait()
                async for chunk in super().stream(request):
                    yield chunk

        parent_root, graph_root, child_root = tmp_path / "parent", tmp_path / "graph", tmp_path / "child"
        for root in (parent_root, graph_root, child_root):
            root.mkdir()
        calls = tuple(
            OpenAIStreamToolCall(
                index=index, id=f"call-{name}", function=OpenAIStreamFunction(name=name, arguments='{"task":"review"}')
            )
            for index, name in enumerate(("execute_graph", "consult"))
        )
        batch = (
            OpenAIStreamChunk(
                id="parallel",
                model="fake-model",
                choices=(OpenAIStreamChoice(delta=OpenAIStreamDelta(tool_calls=calls), finish_reason="tool_calls"),),
            ),
        )
        parent_model = FakeOpenAIModel((batch, text_batch("Done", "final")))
        parent_runner, parent = make_runner(parent_model, parent_root)
        graph_runner, graph_agent = make_runner(GatedModel(graph_started), graph_root)
        child_runner, child_agent = make_runner(GatedModel(subagent_started), child_root)
        limits = RunLimits(max_model_steps=1, max_output_tokens=2)
        graph = Graph(
            graph_id="shared-budget", nodes=(Node("node", agent=graph_agent, runner=graph_runner, limits=limits),)
        )
        ledger = BudgetLedger()
        executor = GraphExecutor(budgets=ledger)
        group = SubagentGroup((Subagent("child", child_agent, child_runner, limits=limits),), budgets=ledger)
        delegation = as_tool(group.subagents["child"], name="consult", description="Consult.", group=group)

        async def execute_graph(context: ToolContext, arguments: JsonObject) -> JsonObject:
            result = await executor.execute(graph, {}, context=context)
            assert result.status is GraphStatus.SUCCEEDED
            return result.model_dump(mode="json")

        graph_tool = FunctionTool(
            ToolDescriptor(name="execute_graph", code="execute_graph", description="Execute a graph."),
            execute_graph,
            SubagentTaskArguments,
            policy=ToolPolicy(parallel_safe=True),
            budget_ledger=ledger,
        )
        parent_runner.tools.register(graph_tool.descriptor, lambda selected: graph_tool)
        parent_runner.tools.register(delegation.descriptor, lambda selected: delegation)
        parent = Agent(
            name=parent.name,
            model=parent.model,
            prompt=parent.prompt,
            tools=(*parent.tools, ToolRef(graph_tool.descriptor.code), ToolRef(delegation.descriptor.code)),
        )
        handle = await start_agent(
            parent_runner, parent, "Review", limits=RunLimits(max_model_steps=5, max_output_tokens=10)
        )
        async with handle:
            await asyncio.wait_for(asyncio.gather(graph_started.wait(), subagent_started.wait()), 3)
            assert len(ledger.reservations) == 2
            assert ledger.pools[handle.run_id].remaining_output_tokens == 5
            assert sum(value.output_tokens for value in ledger.reservations.values()) == 4
            release.set()
            result = await handle.result()
        assert isinstance(result, RunResult) and result.state is RunStatus.SUCCEEDED
        assert result.usage.model_calls == 2 and result.usage.output_tokens == 2
        assert result.usage.child_model_calls == 2 and result.usage.child_budget_output_tokens == 2
        assert result.usage.budget_output_tokens == 4
        assert parent_model.requests[1].max_completion_tokens == 7
        assert len(ledger.usage_charges(handle.run_id)) == 2 and ledger.reservations == {}
        assert ledger.pools[handle.run_id].remaining_output_tokens == 6

    asyncio.run(scenario())


def test_cancelling_a_parent_settles_async_child_resources_and_preserves_unknown_reserved_cost(tmp_path: Path) -> None:
    async def scenario() -> None:
        parent_root, child_root = tmp_path / "parent", tmp_path / "child"
        parent_root.mkdir()
        child_root.mkdir()
        parent_model = FakeOpenAIModel(
            (
                tool_batch("submit_subagent", '{"subagent_id":"child","task":"review"}', "submit"),
                text_batch("Waiting", "final"),
            )
        )
        child_model = BlockingOpenAIModel()
        parent_runner, parent = make_runner(parent_model, parent_root)
        child_runner, child = make_runner(child_model, child_root)
        group = SubagentGroup(
            (Subagent("child", child, child_runner, limits=RunLimits(max_model_steps=1, max_output_tokens=2)),)
        )
        submission = submit_subagent(group)
        parent_runner.tools.register(submission.descriptor, lambda selected: submission)
        parent = Agent(
            name=parent.name,
            model=parent.model,
            prompt=parent.prompt,
            tools=(*parent.tools, ToolRef(submission.descriptor.code)),
        )
        parent_ready = asyncio.Event()
        completed = 0

        async def observe(event: Event) -> None:
            nonlocal completed
            if isinstance(event, ModelStepCompletedEvent):
                completed += 1
                if completed == 2:
                    parent_ready.set()

        parent_runner.event_sink = observe
        handle = await start_agent(
            parent_runner, parent, "Review", limits=RunLimits(max_model_steps=4, max_output_tokens=6)
        )
        async with handle:
            await asyncio.wait_for(child_model.started.wait(), 3)
            await asyncio.wait_for(parent_ready.wait(), 3)
            result = await handle.cancel(reason="host_cancelled")
        assert result.state is RunStatus.CANCELLED
        assert result.usage.model_calls == 2 and result.usage.child_model_calls == 1
        assert result.usage.child_budget_output_tokens == 2 and result.usage.budget_output_tokens == 4
        assert group.budgets.reservations == {} and group.task_group is None
        record = next(iter(group.records.values()))
        assert record.status is DelegationStatus.CANCELLED
        child_state = await group.sessions[record.delegation_id].store.snapshot(
            group.sessions[record.delegation_id].session_id
        )
        assert child_state.current_run_id is None and child_state.runs[0].state is RunStatus.CANCELLED

    asyncio.run(scenario())


@pytest.mark.parametrize("extra_group", [False, True], ids=["single-scope", "independent-sibling-scope"])
def test_async_child_system_failure_closes_its_scope_and_commits_the_parent_failure(
    tmp_path: Path, extra_group: bool
) -> None:
    async def scenario() -> None:
        started, release = asyncio.Event(), asyncio.Event()

        class FailingChild(FakeOpenAIModel):
            async def stream(self, request: OpenAIRequest | Mapping[str, object]) -> AsyncIterator[OpenAIStreamChunk]:
                started.set()
                await release.wait()
                yield OpenAIStreamChunk(id="failed", model="fake-model", choices=())
                raise RuntimeError("external child provider unavailable")

        parent_root, child_root = tmp_path / "parent", tmp_path / "child"
        parent_root.mkdir()
        child_root.mkdir()
        parent_model = FakeOpenAIModel(
            (
                tool_batch("submit_subagent", '{"subagent_id":"child","task":"review"}', "submit"),
                text_batch("Waiting", "final"),
            )
        )
        parent_runner, parent = make_runner(parent_model, parent_root)
        child_runner, child = make_runner(FailingChild(()), child_root)
        ledger = BudgetLedger()
        group = SubagentGroup(
            (Subagent("child", child, child_runner, limits=RunLimits(max_model_steps=1, max_output_tokens=2)),),
            budgets=ledger,
        )
        submission = submit_subagent(group)
        parent_runner.tools.register(submission.descriptor, lambda selected: submission)
        parent = Agent(
            name=parent.name,
            model=parent.model,
            prompt=parent.prompt,
            tools=(*parent.tools, ToolRef(submission.descriptor.code)),
        )
        other_group: SubagentGroup | None = None
        other_model: BlockingOpenAIModel | None = None
        if extra_group:
            other_root = tmp_path / "other"
            other_root.mkdir()
            other_model = BlockingOpenAIModel()
            other_runner, other_agent = make_runner(other_model, other_root)
            other_group = SubagentGroup(
                (
                    Subagent(
                        "other", other_agent, other_runner, limits=RunLimits(max_model_steps=1, max_output_tokens=2)
                    ),
                ),
                budgets=ledger,
            )
            other_submission = submit_subagent(other_group, name="submit_other")
            parent_runner.tools.register(other_submission.descriptor, lambda selected: other_submission)
            parent = Agent(
                name=parent.name,
                model=parent.model,
                prompt=parent.prompt,
                tools=(*parent.tools[:-1], ToolRef(other_submission.descriptor.code), parent.tools[-1]),
            )
            calls = tuple(
                OpenAIStreamToolCall(
                    index=index,
                    id=f"submit-{index}",
                    function=OpenAIStreamFunction(
                        name=name, arguments=json.dumps({"subagent_id": child_id, "task": "review"})
                    ),
                )
                for index, (name, child_id) in enumerate((("submit_subagent", "child"), ("submit_other", "other")))
            )
            parent_model.batches = iter(
                (
                    (
                        OpenAIStreamChunk(
                            id="two-submissions",
                            model="fake-model",
                            choices=(
                                OpenAIStreamChoice(
                                    delta=OpenAIStreamDelta(tool_calls=calls), finish_reason="tool_calls"
                                ),
                            ),
                        ),
                    ),
                    text_batch("Waiting", "final"),
                )
            )
        session = Session("parent-failure", InMemorySessionStore())
        handle = await start_agent(
            parent_runner, parent, "Review", session=session, limits=RunLimits(max_model_steps=4, max_output_tokens=6)
        )
        async with handle:
            await asyncio.wait_for(started.wait(), 3)
            if other_model is not None:
                await asyncio.wait_for(other_model.started.wait(), 3)
            release.set()
            outcomes = await asyncio.gather(handle.result(), return_exceptions=True)
            assert isinstance(outcomes[0], RunResult | ExceptionGroup)
        state = await session.store.snapshot(session.session_id)
        assert state.current_run_id is None
        receipt = state.runs[0].receipt
        assert receipt is not None and receipt.state == "failed"
        assert receipt.usage.child_budget_output_tokens == (4 if extra_group else 2)
        assert group.task_group is None and group.budgets.reservations == {}
        if other_group is not None:
            assert other_group.task_group is None
            other_record = next(iter(other_group.records.values()))
            other_session = other_group.sessions[other_record.delegation_id]
            other_state = await other_session.store.snapshot(other_session.session_id)
            assert other_state.current_run_id is None and other_state.runs[0].state is RunStatus.CANCELLED
        record = next(iter(group.records.values()))
        child_session = group.sessions[record.delegation_id]
        child_receipt = (await child_session.store.snapshot(child_session.session_id)).runs[0].receipt
        assert child_receipt is not None and child_receipt.error_message == "external child provider unavailable"

    asyncio.run(scenario())


def test_cancelling_a_suspended_parent_cancels_its_child_checkpoint_and_settles_the_original_charge(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        parent_root, child_root = tmp_path / "parent", tmp_path / "child"
        parent_root.mkdir()
        child_root.mkdir()
        parent_model = FakeOpenAIModel((tool_batch("consult", '{"task":"review"}', "consult-call"),))
        child_model = FakeOpenAIModel((tool_batch("echo", '{"value":"x"}', "child-call"),))
        parent_runner, parent = make_runner(parent_model, parent_root)
        child_runner, child = make_runner(child_model, child_root, interrupting_tool)
        group = SubagentGroup(
            (Subagent("child", child, child_runner, limits=RunLimits(max_model_steps=3, max_output_tokens=3)),)
        )
        delegation = as_tool(group.subagents["child"], name="consult", description="Consult.", group=group)
        parent_runner.tools.register(delegation.descriptor, lambda selected: delegation)
        parent = Agent(
            name=parent.name,
            model=parent.model,
            prompt=parent.prompt,
            tools=(*parent.tools, ToolRef(delegation.descriptor.code)),
        )
        handle = await start_agent(
            parent_runner, parent, "Review", limits=RunLimits(max_model_steps=5, max_output_tokens=6)
        )
        async with handle:
            paused = await handle.result()
            assert isinstance(paused, RunSuspension)
            assert paused.usage.child_model_calls == 1 and paused.usage.budget_output_tokens == 2
            assert paused.checkpoint.budget is not None and len(paused.checkpoint.budget.reservations) == 1
            result = await handle.cancel(reason="host_cancelled")
            assert await handle.result() == result
            assert await handle.cancel(reason="repeated_cancel") == result
        assert result.state is RunStatus.CANCELLED and result.usage == paused.usage
        assert group.budgets.reservations == {} and group.task_group is None
        record = next(iter(group.records.values()))
        assert record.status is DelegationStatus.CANCELLED and record.child_run_id is not None
        child_session = group.sessions[record.delegation_id]
        assert (
            await child_session.store.run(child_session.session_id, record.child_run_id)
        ).state is RunStatus.CANCELLED
        assert len(child_model.requests) == 1 and len(parent_model.requests) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("seconds", [10.0, 2.0], ids=["safe-pause", "scope-drain-exhausts-timeout"])
def test_parent_execution_time_includes_async_child_drain_before_a_safe_pause(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, seconds: float
) -> None:
    async def scenario() -> None:
        execution_time = 0.0
        child_started, release, first_completed, continue_parent, draining = tuple(
            asyncio.Event() for number in range(5)
        )

        def runtime_time() -> float:
            return execution_time

        monkeypatch.setattr(runner_module, "monotonic", runtime_time)

        class GatedChild(FakeOpenAIModel):
            async def stream(self, request: OpenAIRequest | Mapping[str, object]) -> AsyncIterator[OpenAIStreamChunk]:
                child_started.set()
                await release.wait()
                async for chunk in super().stream(request):
                    yield chunk

        class ObservedGroup(SubagentGroup):
            async def wait_scope(self, run_id: str) -> None:
                draining.set()
                await super().wait_scope(run_id)

        parent_root, child_root = tmp_path / "parent", tmp_path / "child"
        parent_root.mkdir()
        child_root.mkdir()
        parent_model = FakeOpenAIModel(
            (tool_batch("submit_subagent", '{"subagent_id":"child","task":"review"}', "submit"),)
        )
        parent_runner, parent = make_runner(parent_model, parent_root)
        child_runner, child = make_runner(GatedChild((text_batch("Evidence", "child"),)), child_root)
        group = ObservedGroup(
            (Subagent("child", child, child_runner, limits=RunLimits(max_model_steps=1, max_output_tokens=2)),)
        )
        submission = submit_subagent(group)
        parent_runner.tools.register(submission.descriptor, lambda selected: submission)
        parent = Agent(
            name=parent.name,
            model=parent.model,
            prompt=parent.prompt,
            tools=(*parent.tools, ToolRef(submission.descriptor.code)),
        )

        async def observe(event: Event) -> None:
            if isinstance(event, ModelStepCompletedEvent):
                first_completed.set()
                await continue_parent.wait()

        parent_runner.event_sink = observe
        handle = await start_agent(
            parent_runner,
            parent,
            "Review",
            limits=RunLimits(execution_timeout_seconds=seconds, max_model_steps=4, max_output_tokens=6),
        )
        pause_started = asyncio.Event()

        async def pause() -> RunOutcome:
            pause_started.set()
            return await handle.pause(reason="host_requested")

        async with handle:
            await asyncio.wait_for(asyncio.gather(child_started.wait(), first_completed.wait()), 3)
            paused_task = asyncio.create_task(pause())
            await pause_started.wait()
            continue_parent.set()
            await asyncio.wait_for(draining.wait(), 3)
            execution_time = 5.0
            release.set()
            result = await paused_task
        if seconds > 5:
            assert isinstance(result, RunSuspension)
            assert result.checkpoint.execution_seconds_used == 5
        else:
            assert isinstance(result, RunResult) and result.state is RunStatus.FAILED and result.reason == "timeout"
        assert result.usage.model_calls == 1 and result.usage.child_model_calls == 1
        assert result.usage.budget_output_tokens == 2
        assert group.task_group is None and group.budgets.reservations == {}
        assert len(parent_model.requests) == 1

    asyncio.run(scenario())
