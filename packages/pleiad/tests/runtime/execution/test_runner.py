import asyncio
import hashlib
import json
import sqlite3
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event as ThreadEvent

import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import Histogram, InMemoryMetricReader, Sum
from pydantic import TypeAdapter, ValidationError

from pleiad.agent.agent import Agent
from pleiad.agent.errors import AgentConfigurationError
from pleiad.agent.policy import AgentPolicyHints
from pleiad.context.budget import ContextBudget
from pleiad.context.selection.models import ContextBlock
from pleiad.contracts import CapabilityGrants, JsonObject, SkillRef, ToolRef
from pleiad.events.contracts import Event
from pleiad.events.message import MessageAbortedEvent
from pleiad.events.runtime import ModelCompletedEvent, ModelStepCompletedEvent
from pleiad.messages.message import AssistantMessage, ToolMessage, UserMessage
from pleiad.messages.results import OperationResultReference, ResultReference
from pleiad.models.openai.request import OpenAIRequest
from pleiad.models.openai.response import (
    OpenAIStreamChoice,
    OpenAIStreamChunk,
    OpenAIStreamDelta,
    OpenAIStreamFunction,
    OpenAIStreamToolCall,
)
from pleiad.observability.tracing import Observability
from pleiad.orchestration.handoff.handoff import Handoff
from pleiad.runtime.checkpoint import ResumeDecision
from pleiad.runtime.errors import RuntimeStateError
from pleiad.runtime.execution.read_tool_result import ReadToolResultTool
from pleiad.runtime.limits import RunLimits
from pleiad.runtime.policy import HostPolicy, effective_policy
from pleiad.runtime.result import RunOutcome, RunResult, RunSuspension
from pleiad.runtime.runner import Runner
from pleiad.sandbox.policy import SandboxPolicy
from pleiad.sessions.backends.in_memory import InMemorySessionStore
from pleiad.sessions.backends.sqlite import SqliteSessionStore
from pleiad.sessions.checkpoint import RunCheckpoint
from pleiad.sessions.errors import SessionConflictError, SessionStorageError
from pleiad.sessions.input import InputEnvelope
from pleiad.sessions.model_step import ModelStep
from pleiad.sessions.records import RunStatus
from pleiad.sessions.session import Session
from pleiad.sessions.state import SessionState
from pleiad.sessions.writer import SessionWriter
from pleiad.skills.contracts import SkillDescriptor
from pleiad.skills.loader import SkillLoader
from pleiad.skills.package import SkillPackage
from pleiad.tools.base import BackgroundAdmission, FunctionTool
from pleiad.tools.builtins.defaults import install_default_tools
from pleiad.tools.contracts import ToolAcceptance, ToolContext, ToolDescriptor, ToolPolicy, ToolResult, ToolSubmission
from pleiad.tools.contracts import ToolCall as ExecutionToolCall
from pleiad.tools.errors import BackgroundNotAcceptedError, ToolCancelledError
from pleiad.tools.interruption import InterruptionResponse, ToolInterruption
from pleiad.tools.registry import ToolRegistry
from pleiad.tools.runtime import ToolRuntime
from tests.fakes.runtime import (
    BlockingOpenAIModel,
    EchoArguments,
    FailingOpenAIModel,
    FakeAnthropicModel,
    FakeOpenAIModel,
    anthropic_text_batch,
    anthropic_tool_batch,
    create_sandbox,
    interrupting_tool,
    make_runner,
    successful_tool,
    text_batch,
    tool_batch,
)
from tests.runtime_support import active_step, input_text, resume_agent, run_agent, start_agent


def test_runner_persists_a_direct_model_answer_as_one_completed_step(tmp_path: Path) -> None:
    """A direct response creates one Session ModelStep and returns its visible text."""
    model = FakeOpenAIModel((text_batch("done", "response-1"),))
    runner, agent = make_runner(model, tmp_path)
    memory = Session("memory-direct", InMemorySessionStore())

    result = asyncio.run(run_agent(runner, agent, "hello", session=memory, limits=RunLimits()))

    assert result.state is RunStatus.SUCCEEDED

    assert isinstance(result, RunResult)
    assert result.final_output == "done"
    steps = asyncio.run(memory.history())
    assert len(steps) == 1
    assert [message.role for message in steps[0].messages] == ["user", "assistant"]
    projected = model.requests[0].model_dump_json()
    assert "hello" in projected and "input_scope" in projected
    assert input_text(steps[0].messages[0].content) == "hello"


def test_runner_completes_the_model_tool_model_loop_at_step_boundaries(tmp_path: Path) -> None:
    """A Tool response closes one ModelStep before the next model call starts."""
    model = FakeOpenAIModel((tool_batch("call-1"), text_batch("finished", "response-2")))
    runner, agent = make_runner(model, tmp_path, successful_tool)
    memory = Session("memory-tool", InMemorySessionStore())

    result = asyncio.run(run_agent(runner, agent, "use the Tool", session=memory, limits=RunLimits()))

    assert result.state is RunStatus.SUCCEEDED

    assert isinstance(result, RunResult)
    steps = asyncio.run(memory.history())
    assert len(steps) == 2
    assert [message.role for message in steps[0].messages] == [
        "user",
        "assistant",
        "tool",
    ]
    assert steps[0].completed is True
    assert steps[1].messages[-1].content == "finished"
    assert len(model.requests) == 2
    assert model.requests[1].messages[-1].role == "tool"


def test_tool_continuation_retains_dynamic_references_without_rewriting_memory(tmp_path: Path) -> None:
    """The second model request keeps references and the original Tool pairing."""
    model = FakeOpenAIModel((tool_batch("call-1"), text_batch("finished", "response-2")))
    runner, agent = make_runner(model, tmp_path, successful_tool)
    memory = Session("context-tool-continuation", InMemorySessionStore())
    original = UserMessage(content="Use the Tool.")
    references = (ContextBlock(kind="task_state", origin="request", content="customer=Alpha"),)

    result = asyncio.run(run_agent(runner, agent, original, session=memory, context=references, limits=RunLimits()))

    assert result.state is RunStatus.SUCCEEDED

    assert isinstance(result, RunResult)
    assert all("customer=Alpha" in request.model_dump_json() for request in model.requests)
    assert [message.role for message in model.requests[1].messages] == ["system", "user", "assistant", "tool"]
    assert model.requests[1].messages[-1].tool_call_id == "call-1"
    assert asyncio.run(memory.history())[0].messages[0].message_id == original.message_id
    assert input_text(asyncio.run(memory.history())[0].messages[0].content) == original.content


def test_resumed_run_reuses_the_saved_reference_snapshot(tmp_path: Path) -> None:
    """Resume reconstructs the exact saved context beside the accepted input."""
    model = FakeOpenAIModel((tool_batch("call-1"), text_batch("resumed", "response-2")))
    runner, agent = make_runner(model, tmp_path, interrupting_tool)
    memory = Session("context-resume", InMemorySessionStore())
    original = UserMessage(content="Wait for approval.")

    async def exercise() -> RunOutcome:
        interrupted = await run_agent(
            runner,
            agent,
            original,
            session=memory,
            context=(ContextBlock(kind="task_state", origin="request", content="old-reference"),),
            limits=RunLimits(),
        )
        assert isinstance(interrupted, RunSuspension)
        return await resume_agent(
            runner,
            agent,
            interrupted.checkpoint,
            session=memory,
            decisions=(approved_resolution(),),
            limits=RunLimits(),
        )

    result = asyncio.run(exercise())

    assert result.state is RunStatus.SUCCEEDED

    assert isinstance(result, RunResult)
    assert "old-reference" in model.requests[1].model_dump_json()
    assert "current-reference" not in model.requests[1].model_dump_json()
    assert [message.role for message in model.requests[1].messages] == ["system", "user", "assistant", "tool"]
    assert model.requests[1].messages[-1].tool_call_id == "call-1"
    assert asyncio.run(memory.history())[0].messages[0].message_id == original.message_id
    assert input_text(asyncio.run(memory.history())[0].messages[0].content) == original.content


def test_agent_completes_anthropic_tool_roundtrip(tmp_path: Path) -> None:
    """The Anthropic protocol completes the same Tool roundtrip as OpenAI."""
    model = FakeAnthropicModel((anthropic_tool_batch("call-1"), anthropic_text_batch("finished", "response-2")))
    registry = ToolRegistry()
    workspace = tmp_path / "workspace"
    scratch = tmp_path / "scratch"
    workspace.mkdir()
    scratch.mkdir()
    install_default_tools(
        registry,
        workspace=workspace,
        scratch=scratch,
        sandbox=create_sandbox(),
        sandbox_policy=SandboxPolicy(image=f"python@sha256:{'a' * 64}", allow_shell_command=True),
    )
    descriptor = ToolDescriptor(name="echo", code="echo", description="Echo a value")
    registry.register(
        descriptor,
        lambda selected: FunctionTool(
            selected,
            successful_tool,
            EchoArguments,
            policy=ToolPolicy(parallel_safe=True),
        ),
    )
    runner = Runner(tools=registry, skills=SkillLoader(), tool_runtime=ToolRuntime())
    agent = Agent(name="assistant", model=model, prompt="Answer the user.", tools=(ToolRef("echo"),))
    memory = Session("memory-anthropic-tool", InMemorySessionStore())

    result = asyncio.run(run_agent(runner, agent, "use the Tool", session=memory, limits=RunLimits()))

    assert result.state is RunStatus.SUCCEEDED

    assert isinstance(result, RunResult)
    assert result.final_output == "finished"
    assert len(model.requests) == 2
    assert model.requests[1].messages[-1].role == "user"


def test_runner_resume_submits_a_resolved_interruption_once(tmp_path: Path) -> None:
    """Resume completes the paused Tool result and continues without repeating the Tool."""
    model = FakeOpenAIModel((tool_batch("call-1"), text_batch("resumed", "response-2")))
    runner, agent = make_runner(model, tmp_path, interrupting_tool)
    memory = Session("memory-resume", InMemorySessionStore())

    interrupted = asyncio.run(run_agent(runner, agent, "wait", session=memory, limits=RunLimits()))

    assert interrupted.state is RunStatus.PAUSED
    assert isinstance(interrupted, RunSuspension)
    now = datetime.now(UTC)
    resolution = ResumeDecision(
        interruption_id="interrupt-1",
        result=ToolResult(call_id="call-1", ok=True, output={"approved": True}, started_at=now, finished_at=now),
    )
    resumed = asyncio.run(
        resume_agent(
            runner,
            agent,
            interrupted.checkpoint,
            session=memory,
            decisions=(resolution,),
            limits=RunLimits(),
        ),
    )

    assert resumed.state is RunStatus.SUCCEEDED

    assert isinstance(resumed, RunResult)
    steps = asyncio.run(memory.history())
    assert len(steps) == 2
    assert (
        sum(
            len(message.tool_calls)
            for step in steps
            for message in step.messages
            if isinstance(message, AssistantMessage)
        )
        == 1
    )
    assert model.requests[1].messages[-1].role == "tool"


def test_streamed_resume_publishes_recovered_tool_messages_after_memory_accepts_them(tmp_path: Path) -> None:
    """Resume correlates the recovered Tool result and the next ModelStep with the original Run."""
    model = FakeOpenAIModel((tool_batch("call-1"), text_batch("resumed", "response-2")))
    runner, agent = make_runner(model, tmp_path, interrupting_tool)
    memory = Session("memory-streamed-resume", InMemorySessionStore())
    interrupted = asyncio.run(run_agent(runner, agent, "wait", session=memory, limits=RunLimits()))
    assert isinstance(interrupted, RunSuspension)
    checkpoint = interrupted.checkpoint
    previous_events = asyncio.run(memory.store.snapshot(memory.session_id)).events
    cursor = previous_events[-1].sequence
    now = datetime.now(UTC)
    resolution = ResumeDecision(
        interruption_id="interrupt-1",
        result=ToolResult(call_id="call-1", ok=True, output={"approved": True}, started_at=now, finished_at=now),
    )

    async def collect() -> tuple[list[JsonObject], RunOutcome]:
        stream = await runner.resume(
            agent,
            checkpoint=checkpoint,
            session=memory,
            decisions=(resolution,),
            limits=RunLimits(),
        )
        events = [record.event async for record in stream.events(after_sequence=cursor)]
        return events, await stream.result()

    events, result = asyncio.run(collect())

    kinds = [event["kind"] for event in events]
    assert kinds[:5] == [
        "run.resumed",
        "tool.completed",
        "message.completed",
        "step.completed",
        "step.started",
    ]
    assert result.run_id == interrupted.run_id
    assert all(event["run_id"] == interrupted.run_id for event in events)


def test_model_stream_failure_aborts_the_message_and_reports_the_attempt(tmp_path: Path) -> None:
    """A Provider failure closes the streamed message and reports the failed model attempt."""
    model = FailingOpenAIModel(())
    runner, agent = make_runner(model, tmp_path)
    memory = Session("memory-model-failure", InMemorySessionStore())

    async def collect() -> list[JsonObject]:
        stream = await start_agent(runner, agent, "hello", session=memory, limits=RunLimits())
        events = [record.event async for record in stream.events()]
        with pytest.raises(RuntimeError, match="provider unavailable"):
            await stream.result()
        return events

    events = asyncio.run(collect())

    assert [event["kind"] for event in events][-3:] == ["model.failed", "message.aborted", "run.failed"]
    failed = asyncio.run(memory.store.snapshot(memory.session_id)).runs[0]
    assert failed.usage.model_calls == 1 and failed.usage.output_tokens is None
    assert failed.usage.budget_output_tokens == model.requests[0].max_completion_tokens
    assert len(failed.model_attempts) == 1 and failed.model_attempts[0].completed_at is None


def test_runner_streams_safe_lifecycle_events_through_the_shared_execution_path(tmp_path: Path) -> None:
    """The streamed API exposes model deltas and its terminal event from one Run."""
    model = FakeOpenAIModel((text_batch("streamed", "response-1"),))
    runner, agent = make_runner(model, tmp_path)
    memory = Session("memory-stream", InMemorySessionStore())

    async def collect() -> tuple[list[JsonObject], RunOutcome]:
        stream = await start_agent(runner, agent, "hello", session=memory, limits=RunLimits())
        events = [record.event async for record in stream.events()]
        return events, await stream.result()

    events, result = asyncio.run(collect())

    assert result.state is RunStatus.SUCCEEDED

    assert isinstance(result, RunResult)
    assert [event["kind"] for event in events] == [
        "run.started",
        "step.started",
        "message.completed",
        "model.started",
        "message.started",
        "message.text.delta",
        "model.completed",
        "message.completed",
        "step.completed",
        "run.completed",
    ]
    assert events[5]["delta"] == "streamed"
    completed_message = events[7]["message"]
    assert isinstance(completed_message, dict)
    assert events[4]["message_id"] == events[5]["message_id"] == completed_message["message_id"]
    assert all(event["event_id"] and event["schema_version"] == 1 for event in events)
    assert all(event["run_id"] == result.run_id for event in events)


def test_runner_timeout_aborts_the_active_step_and_returns_timed_out(tmp_path: Path) -> None:
    """The Run timeout cleans up an active Session ModelStep."""
    model = BlockingOpenAIModel()
    runner, agent = make_runner(model, tmp_path)
    memory = Session("memory-timeout", InMemorySessionStore())
    events: list[Event] = []

    async def sink(event: Event) -> None:
        events.append(event)

    runner.event_sink = sink

    async def run() -> RunOutcome:
        return await run_agent(
            runner,
            agent,
            "wait",
            session=memory,
            limits=RunLimits(execution_timeout_seconds=0.05),
        )

    async def exercise() -> tuple[RunOutcome, ModelStep | None]:
        task = asyncio.create_task(run())
        await model.started.wait()
        result = await task
        return result, await active_step(memory)

    result, active = asyncio.run(exercise())

    assert result.state is RunStatus.FAILED

    assert isinstance(result, RunResult)
    assert active is None
    aborted = next(event for event in events if isinstance(event, MessageAbortedEvent))
    assert aborted.reason == "timed_out"
    assert result.usage.model_calls == 1 and result.usage.output_tokens is None
    assert result.usage.budget_output_tokens == RunLimits().max_output_tokens


def test_runner_propagates_cancellation_after_aborting_the_active_step(tmp_path: Path) -> None:
    """Caller cancellation remains CancelledError after Runtime cleanup."""
    model = BlockingOpenAIModel()
    runner, agent = make_runner(model, tmp_path)
    memory = Session("memory-cancel", InMemorySessionStore())
    events: list[Event] = []

    async def sink(event: Event) -> None:
        events.append(event)

    runner.event_sink = sink

    async def exercise() -> ModelStep | None:
        task = asyncio.create_task(run_agent(runner, agent, "cancel", session=memory, limits=RunLimits()))
        await model.started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return await active_step(memory)

    active = asyncio.run(exercise())

    assert active is None
    assert [event.kind for event in events][-2:] == ["message.aborted", "run.cancelled"]
    cancelled = asyncio.run(memory.store.snapshot(memory.session_id)).runs[0]
    assert cancelled.usage.model_calls == 1 and cancelled.usage.output_tokens is None
    assert cancelled.usage.budget_output_tokens == RunLimits().max_output_tokens


@pytest.mark.parametrize("event_kind", ["run.started", "step.started", "message.completed"])
def test_cancellation_during_startup_leaves_memory_available_for_next_run(tmp_path: Path, event_kind: str) -> None:
    """Cancellation before the model loop releases the newly owned ModelStep."""
    model = FakeOpenAIModel((text_batch("next answer", "next-response"),))
    runner, agent = make_runner(model, tmp_path)
    memory = Session("startup-cancel", InMemorySessionStore())

    async def exercise() -> RunOutcome:
        blocked = asyncio.Event()

        async def sink(event: Event) -> None:
            if event.kind == event_kind and not blocked.is_set():
                blocked.set()
                await asyncio.Event().wait()

        runner.event_sink = sink
        task = asyncio.create_task(run_agent(runner, agent, "cancel", session=memory, limits=RunLimits()))
        await asyncio.wait_for(blocked.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await active_step(memory) is None
        assert model.requests == []
        return await run_agent(runner, agent, "next", session=memory, limits=RunLimits())

    result = asyncio.run(exercise())

    assert result.state is RunStatus.SUCCEEDED

    assert isinstance(result, RunResult)
    assert result.final_output == "next answer"
    assert input_text(asyncio.run(memory.history())[0].messages[0].content) == "next"


def test_handoff_enforces_the_target_agents_shorter_deadline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A tighter target policy cancels its model and closes its active ModelStep."""
    source_model = FakeOpenAIModel(
        (
            (
                OpenAIStreamChunk(
                    id="handoff-response",
                    model="fake-model",
                    choices=(
                        OpenAIStreamChoice(
                            delta=OpenAIStreamDelta(
                                tool_calls=(
                                    OpenAIStreamToolCall(
                                        index=0,
                                        id="handoff-call",
                                        function=OpenAIStreamFunction(name="transfer", arguments='{"task":"review"}'),
                                    ),
                                ),
                            ),
                            finish_reason="tool_calls",
                        ),
                    ),
                ),
            ),
        ),
    )
    target_model = BlockingOpenAIModel()
    runner, source = make_runner(source_model, tmp_path)
    target = Agent(
        name="reviewer",
        model=target_model,
        prompt="Review the task.",
        policy_hints=AgentPolicyHints(timeout_seconds=1),
    )
    binding = Handoff(source=source, target=target, name="transfer", description="Transfer to reviewer").bind(runner)
    memory = Session("handoff-deadline", InMemorySessionStore())

    async def exercise() -> RunOutcome:
        timer = asyncio.Timeout(None)
        reschedule = timer.reschedule
        target_deadlines: list[float] = []

        def expire_target_deadline(when: float | None) -> None:
            if when is None:
                reschedule(None)
            else:
                assert when - asyncio.get_running_loop().time() <= 1
                target_deadlines.append(when)
                reschedule(when)

        monkeypatch.setattr(timer, "reschedule", expire_target_deadline)
        monkeypatch.setattr(asyncio, "timeout", lambda delay: timer)
        async with asyncio.TaskGroup() as group:
            task = group.create_task(
                run_agent(
                    runner,
                    source,
                    "review",
                    session=memory,
                    limits=RunLimits(execution_timeout_seconds=60.0),
                    handoffs=(binding,),
                )
            )
            await asyncio.wait_for(target_model.started.wait(), timeout=3)
            assert target_deadlines
            reschedule(asyncio.get_running_loop().time())
        return task.result()

    result = asyncio.run(exercise())

    assert result.state is RunStatus.FAILED

    assert isinstance(result, RunResult)
    assert result.reason == "timeout"
    assert target_model.started.is_set()
    assert asyncio.run(active_step(memory)) is None
    assert len(asyncio.run(memory.history())) == 1


def test_run_stream_context_closes_an_unconsumed_run(tmp_path: Path) -> None:
    """Leaving the explicit stream scope cancels and cleans its owned Run."""
    model = BlockingOpenAIModel()
    runner, agent = make_runner(model, tmp_path)
    memory = Session("memory-stream-close", InMemorySessionStore())

    async def exercise() -> tuple[bool, ModelStep | None]:
        stream = await start_agent(runner, agent, "close", session=memory, limits=RunLimits())
        async with stream:
            await model.started.wait()
        assert stream.task is not None
        return stream.task.done(), await active_step(memory)

    done, active = asyncio.run(exercise())

    assert done is True
    assert active is None


def test_missing_tool_binding_fails_before_step_starts(tmp_path: Path) -> None:
    """An unbound default Tool fails before Session or the model is touched."""
    model = FakeOpenAIModel((text_batch("unexpected", "response-1"),))
    registry = ToolRegistry()
    runner = Runner(tools=registry, skills=SkillLoader(), tool_runtime=ToolRuntime())
    agent = Agent(name="assistant", model=model, prompt="Answer the user.")
    memory = Session("memory-missing-tool", InMemorySessionStore())

    with pytest.raises(AgentConfigurationError, match="capability binding"):
        asyncio.run(run_agent(runner, agent, "hello", session=memory, limits=RunLimits()))

    assert asyncio.run(memory.history()) == ()
    assert model.requests == []


@pytest.mark.parametrize(("name", "code"), [("read_tool_result", "host.reader"), ("host_reader", "read_tool_result")])
def test_host_tool_cannot_shadow_the_memory_bound_result_reader(tmp_path: Path, name: str, code: str) -> None:
    """Reserved result-reader identity fails before a ModelStep or model request starts."""
    model = FakeOpenAIModel((text_batch("unexpected", "response-1"),))
    runner, base_agent = make_runner(model, tmp_path)
    descriptor = ToolDescriptor(name=name, code=code, description="Host result reader")
    runner.tools.register(descriptor, lambda selected: FunctionTool(selected, successful_tool, EchoArguments))
    agent = Agent(name=base_agent.name, model=model, prompt=base_agent.prompt, tools=(ToolRef(code),))
    memory = Session("reader-shadow", InMemorySessionStore())

    with pytest.raises(AgentConfigurationError, match="reserved"):
        asyncio.run(run_agent(runner, agent, "run", session=memory, limits=RunLimits()))

    assert asyncio.run(active_step(memory)) is None
    assert model.requests == []


def test_handoff_cannot_shadow_the_memory_bound_result_reader(tmp_path: Path) -> None:
    """A reserved Handoff name fails before Runtime starts a ModelStep."""
    model = FakeOpenAIModel(())
    runner, source = make_runner(model, tmp_path)
    target = Agent(name="reviewer", model=model, prompt="Review the task.")
    binding = Handoff(source=source, target=target, name="read_tool_result", description="Transfer to reviewer").bind(
        runner
    )
    memory = Session("handoff-reader-shadow", InMemorySessionStore())

    with pytest.raises(AgentConfigurationError, match="reserved"):
        asyncio.run(run_agent(runner, source, "run", session=memory, limits=RunLimits(), handoffs=(binding,)))

    assert asyncio.run(active_step(memory)) is None
    assert model.requests == []


def test_skill_resolution_does_not_load_instructions_body(tmp_path: Path) -> None:
    """Runner capability resolution exposes Skill metadata without loading its body."""
    skill_root = tmp_path / "demo"
    skill_root.mkdir()
    (skill_root / "SKILL.md").write_bytes(b"---\nname: demo\ndescription: Demo skill\n---\n\xff")
    descriptor = SkillDescriptor(name="demo", code="demo", description="Demo skill")
    loader = SkillLoader()
    loader.register(descriptor, SkillPackage(skill_root))
    registry = ToolRegistry()
    (tmp_path / "workspace").mkdir()
    (tmp_path / "scratch").mkdir()
    install_default_tools(
        registry,
        workspace=tmp_path / "workspace",
        scratch=tmp_path / "scratch",
        skill_roots={"demo": skill_root},
        sandbox=create_sandbox(),
        sandbox_policy=SandboxPolicy(image=f"python@sha256:{'a' * 64}", allow_shell_command=True),
    )
    runner = Runner(tools=registry, skills=loader, tool_runtime=ToolRuntime())
    agent = Agent(name="assistant", model=FakeOpenAIModel(()), prompt="Answer the user.", skills=(SkillRef("demo"),))

    _, skills = runner.resolve(agent)

    assert skills[0].descriptor == descriptor
    assert skills[0].instructions is None


def test_skill_requires_read_access_before_model_call(tmp_path: Path) -> None:
    """A selected Skill without a host-bound read root fails before a ModelStep starts."""
    skill_root = tmp_path / "demo"
    skill_root.mkdir()
    (skill_root / "SKILL.md").write_text(
        "---\nname: demo\ndescription: Demo skill\n---\nUse the demo skill.\n",
        encoding="utf-8",
    )
    loader = SkillLoader()
    loader.register(
        SkillDescriptor(name="demo", code="demo", description="Demo skill"),
        SkillPackage(skill_root),
    )
    registry = ToolRegistry()
    workspace = tmp_path / "workspace"
    scratch = tmp_path / "scratch"
    workspace.mkdir()
    scratch.mkdir()
    install_default_tools(
        registry,
        workspace=workspace,
        scratch=scratch,
        sandbox=create_sandbox(),
        sandbox_policy=SandboxPolicy(image=f"python@sha256:{'a' * 64}", allow_shell_command=True),
    )
    runner = Runner(tools=registry, skills=loader, tool_runtime=ToolRuntime())
    model = FakeOpenAIModel((text_batch("unexpected", "response-1"),))
    agent = Agent(name="assistant", model=model, prompt="Answer the user.", skills=(SkillRef("demo"),))
    memory = Session("memory-missing-skill-read", InMemorySessionStore())

    with pytest.raises(AgentConfigurationError, match="not readable"):
        asyncio.run(run_agent(runner, agent, "hello", session=memory, limits=RunLimits()))

    assert asyncio.run(memory.history()) == ()
    assert model.requests == []


@pytest.mark.parametrize("content", ["not json", '{"answer": 7}'], ids=["malformed-json", "schema-mismatch"])
def test_invalid_final_output_is_not_success(tmp_path: Path, content: str) -> None:
    """A final response that violates the Agent schema returns a failed Run."""
    model = FakeOpenAIModel((text_batch(content, "response-1"),))
    runner, _ = make_runner(model, tmp_path)
    agent = Agent(
        name="structured",
        model=model,
        prompt="Return JSON.",
        output_schema={"type": "object", "required": ["answer"], "properties": {"answer": {"type": "string"}}},
    )
    memory = Session("memory-invalid-output", InMemorySessionStore())

    result = asyncio.run(run_agent(runner, agent, "hello", session=memory, limits=RunLimits()))

    assert result.state is RunStatus.FAILED

    assert isinstance(result, RunResult)
    assert result.error is not None
    assert result.error.code == "agent.output.invalid"
    steps = asyncio.run(memory.history())
    assert len(steps) == 1 and steps[0].completed
    assert steps[0].messages[-1].content == content


def test_reused_agent_keeps_runs_isolated(tmp_path: Path) -> None:
    """Reusing one immutable Agent does not share messages between Runs."""
    model = FakeOpenAIModel((text_batch("first", "response-1"), text_batch("second", "response-2")))
    runner, agent = make_runner(model, tmp_path)
    first_memory = Session("memory-first", InMemorySessionStore())
    second_memory = Session("memory-second", InMemorySessionStore())

    first = asyncio.run(run_agent(runner, agent, "one", session=first_memory, limits=RunLimits()))
    second = asyncio.run(run_agent(runner, agent, "two", session=second_memory, limits=RunLimits()))

    assert first.state is RunStatus.SUCCEEDED

    assert isinstance(first, RunResult)
    assert second.state is RunStatus.SUCCEEDED
    assert isinstance(second, RunResult)
    assert first.final_output == "first"
    assert second.final_output == "second"
    first_steps = asyncio.run(first_memory.history())
    second_steps = asyncio.run(second_memory.history())
    assert input_text(first_steps[0].messages[0].content) == "one"
    assert input_text(second_steps[0].messages[0].content) == "two"


class RecordingResumeTool(FunctionTool):
    """Expose recovery permissions and cancellation through a real Tool binding."""

    def __init__(self) -> None:
        super().__init__(
            ToolDescriptor(name="echo", code="echo", description="Echo a value"),
            interrupting_tool,
            EchoArguments,
            required_permissions=frozenset({"write"}),
        )
        self.contexts: list[ToolContext] = []
        self.on_resume: Callable[[], Awaitable[None]] | None = None

    async def resume_interruption(
        self, context: ToolContext, interruption: ToolInterruption, response: InterruptionResponse
    ) -> ToolResult:
        self.contexts.append(context)
        if self.on_resume is not None:
            await self.on_resume()
        return response.result


def make_resume_runner(tmp_path: Path) -> tuple[Runner, Agent, RecordingResumeTool]:
    model = FakeOpenAIModel((tool_batch("call-1"), text_batch("resumed", "response-2")))
    runner, base_agent = make_runner(model, tmp_path)
    tool = RecordingResumeTool()
    runner.tools.register(tool.descriptor, lambda descriptor: tool)
    runner.host_policy = HostPolicy(grants=CapabilityGrants(capabilities=frozenset({"write"})))
    agent = Agent(name=base_agent.name, model=model, prompt=base_agent.prompt, tools=(ToolRef("echo"),))
    return runner, agent, tool


def approved_resolution() -> ResumeDecision:
    now = datetime.now(UTC)
    return ResumeDecision(
        interruption_id="interrupt-1",
        result=ToolResult(call_id="call-1", ok=True, output={"approved": True}, started_at=now, finished_at=now),
    )


def make_sequential_tool_runner(
    tmp_path: Path, *, wait_second: bool = False
) -> tuple[Runner, Agent, list[ToolContext]]:
    """Bind a serial batch whose external actions expose their real admission order."""
    response = OpenAIStreamChunk(
        id="serial-response",
        model="fake-model",
        choices=(
            OpenAIStreamChoice(
                delta=OpenAIStreamDelta(
                    tool_calls=tuple(
                        OpenAIStreamToolCall(
                            index=index,
                            id=f"call-{index + 1}",
                            function=OpenAIStreamFunction(name="serial", arguments='{"value":"original"}'),
                        )
                        for index in range(3)
                    )
                )
            ),
        ),
    )
    model = FakeOpenAIModel(((response, tool_batch("unused")[1]), text_batch("resumed", "response-2")))
    runner, base_agent = make_runner(model, tmp_path)
    contexts: list[ToolContext] = []

    async def action(context: ToolContext, arguments: JsonObject) -> JsonObject | ToolInterruption:
        contexts.append(context)
        if context.call_id == "call-1" or (wait_second and context.call_id == "call-2"):
            return ToolInterruption(
                interruption_id=f"{context.call_id}-pending",
                call_id=context.call_id,
                tool_code="serial",
                tool_name="serial",
                arguments=arguments,
                reason="Wait for the external action's result",
            )
        return {"received": arguments["value"]}

    descriptor = ToolDescriptor(name="serial", code="serial", description="Perform serial external actions")
    runner.tools.register(descriptor, lambda selected: FunctionTool(selected, action, EchoArguments))
    agent = Agent(name=base_agent.name, model=model, prompt=base_agent.prompt, tools=(ToolRef("serial"),))
    return runner, agent, contexts


def resolve_serial_call(call_id: str) -> ResumeDecision:
    """Supply the actual completion fact for one admitted external action."""
    now = datetime.now(UTC)
    return ResumeDecision(
        interruption_id=f"{call_id}-pending",
        result=ToolResult(call_id=call_id, ok=True, output={"completed": True}, started_at=now, finished_at=now),
    )


def test_interrupted_serial_batch_defers_later_actions_until_resume_with_current_limits(tmp_path: Path) -> None:
    """A pending action blocks later admissions, which resume under the reduced grant."""
    runner, agent, contexts = make_sequential_tool_runner(tmp_path)
    session = Session("serial-deferred", InMemorySessionStore())

    async def scenario() -> None:
        paused = await run_agent(runner, agent, "perform actions", session=session, limits=RunLimits())
        assert isinstance(paused, RunSuspension)
        assert [value.call_id for value in contexts] == ["call-1"]
        assert [value.call_id for value in await session.store.tool_calls(session.session_id, paused.run_id)] == [
            "call-1"
        ]
        assert paused.checkpoint.tool_results == ()

        result = await resume_agent(
            runner,
            agent,
            paused.checkpoint,
            session=session,
            decisions=(resolve_serial_call("call-1"),),
            limits=RunLimits(max_model_steps=2, max_output_tokens=500, tool_concurrency=1),
        )
        assert isinstance(result, RunResult) and result.final_output == "resumed"
        assert [value.call_id for value in contexts] == ["call-1", "call-2", "call-3"]
        assert contexts[1].parent_limits is not None
        assert contexts[1].parent_limits.remaining_model_steps == 1
        assert contexts[1].parent_limits.remaining_output_tokens == 500 - paused.usage.budget_output_tokens
        assert contexts[1].parent_limits.tool_concurrency == 1
        step = (await session.history())[0]
        assert [value.tool_call_id for value in step.messages if isinstance(value, ToolMessage)] == [
            "call-1",
            "call-2",
            "call-3",
        ]
        assert result.usage.tool_calls == 3

    asyncio.run(scenario())


def test_second_deferred_interruption_retains_prior_results_without_replaying_actions(tmp_path: Path) -> None:
    """Each actual pending action creates its own barrier within the original batch."""
    runner, agent, contexts = make_sequential_tool_runner(tmp_path, wait_second=True)
    session = Session("serial-second-pause", InMemorySessionStore())

    async def scenario() -> None:
        first = await run_agent(runner, agent, "perform actions", session=session, limits=RunLimits())
        assert isinstance(first, RunSuspension)
        second = await resume_agent(
            runner, agent, first.checkpoint, session=session, decisions=(resolve_serial_call("call-1"),)
        )
        assert isinstance(second, RunSuspension)
        assert [value.call_id for value in contexts] == ["call-1", "call-2"]
        assert [value.call_id for value in second.checkpoint.tool_results] == ["call-1"]
        assert [value.call_id for value in second.interruptions] == ["call-2"]
        assert second.usage.tool_calls == 1
        result = await resume_agent(
            runner, agent, second.checkpoint, session=session, decisions=(resolve_serial_call("call-2"),)
        )
        assert isinstance(result, RunResult) and result.final_output == "resumed"
        assert [value.call_id for value in contexts] == ["call-1", "call-2", "call-3"]
        assert result.usage.tool_calls == 3

    asyncio.run(scenario())


def test_cancelling_interrupted_serial_batch_keeps_later_declarations_unadmitted(tmp_path: Path) -> None:
    """Cancelling a paused batch never starts the trailing external actions."""
    runner, agent, contexts = make_sequential_tool_runner(tmp_path)
    session = Session("serial-cancel", InMemorySessionStore())

    async def scenario() -> None:
        paused = await run_agent(runner, agent, "perform actions", session=session, limits=RunLimits())
        assert isinstance(paused, RunSuspension)
        result = await runner.cancel_paused(session=session, checkpoint=paused.checkpoint, reason="user_requested")
        assert result.state is RunStatus.CANCELLED
        assert [value.call_id for value in contexts] == ["call-1"]
        assert [value.call_id for value in await session.store.tool_calls(session.session_id, paused.run_id)] == [
            "call-1"
        ]

    asyncio.run(scenario())


def test_recovery_finishes_original_serial_pairing_without_replaying_confirmed_deferred_actions(tmp_path: Path) -> None:
    """A crash before checkpoint replacement preserves all confirmed external results."""
    runner, agent, contexts = make_sequential_tool_runner(tmp_path)
    session = Session("serial-recovery", InMemorySessionStore())

    async def scenario() -> None:
        paused = await run_agent(runner, agent, "perform actions", session=session, limits=RunLimits())
        assert isinstance(paused, RunSuspension)
        writer = SessionWriter(session, await session.store.resume(session.session_id, paused.checkpoint))
        first_intent = (await session.store.tool_calls(session.session_id, paused.run_id))[0]
        now = datetime.now(UTC)
        for call_id in ("call-1", "call-2", "call-3"):
            observed = ToolResult(
                call_id=call_id, ok=True, output={"observed": call_id}, started_at=now, finished_at=now
            )
            await writer.save_tool_call(
                first_intent.model_copy(update={"call_id": call_id, "state": "succeeded", "result": observed})
            )
        recovered = await runner.reconcile(
            session=session,
            run_id=paused.run_id,
            request_id="recover-confirmed-batch",
            expected_epoch=writer.writer_epoch,
            expected_revision=writer.revision,
        )
        checkpoint = recovered.snapshot.checkpoint
        assert checkpoint is not None and checkpoint.interruptions == ()
        result = await resume_agent(runner, agent, checkpoint, session=session)
        assert isinstance(result, RunResult) and result.final_output == "resumed"
        assert result.usage.tool_calls == 3
        assert [value.call_id for value in contexts] == ["call-1"]
        messages = (await session.history())[0].messages
        assert [value.tool_call_id for value in messages if isinstance(value, ToolMessage)] == [
            "call-1",
            "call-2",
            "call-3",
        ]

    asyncio.run(scenario())


@pytest.mark.parametrize("restriction", ["request", "parent"])
def test_resume_rejects_revoked_permissions_before_external_action(tmp_path: Path, restriction: str) -> None:
    runner, agent, tool = make_resume_runner(tmp_path)
    memory = Session("revoked", InMemorySessionStore())

    async def exercise() -> None:
        parent_policy = effective_policy(RunLimits(), runner.host_policy) if restriction == "parent" else None
        interrupted = await run_agent(
            runner,
            agent,
            "approve",
            session=memory,
            limits=RunLimits(),
            parent_run_id="parent-run" if parent_policy is not None else None,
            parent_policy=parent_policy,
        )
        assert isinstance(interrupted, RunSuspension)
        restricted = RunLimits(grants=CapabilityGrants())
        with pytest.raises(RuntimeStateError, match="permissions"):
            await resume_agent(
                runner,
                agent,
                interrupted.checkpoint,
                session=memory,
                decisions=(approved_resolution(),),
                limits=restricted if restriction == "request" else RunLimits(),
                parent_policy=effective_policy(restricted, runner.host_policy) if restriction == "parent" else None,
            )
        assert tool.contexts == []
        assert await active_step(memory) is not None

    asyncio.run(exercise())


def test_resume_expired_checkpoint_never_reenters_tool(tmp_path: Path) -> None:
    runner, agent, tool = make_resume_runner(tmp_path)
    memory = Session("expired", InMemorySessionStore())

    async def exercise() -> None:
        interrupted = await run_agent(runner, agent, "approve", session=memory, limits=RunLimits())
        assert isinstance(interrupted, RunSuspension)
        checkpoint = interrupted.checkpoint
        result = await resume_agent(
            runner,
            agent,
            checkpoint,
            session=memory,
            decisions=(approved_resolution(),),
            limits=RunLimits(deadline=datetime.now(UTC) - timedelta(seconds=1)),
        )
        assert result.state is RunStatus.FAILED
        assert isinstance(result, RunResult)
        assert tool.contexts == []
        assert result.reason == "timeout"
        snapshot = await memory.store.snapshot(memory.session_id)
        assert snapshot.steps[-1].state == "aborted"
        assert snapshot.steps[-1].messages[1].message_id
        assert await active_step(memory) is None

    asyncio.run(exercise())


def test_child_run_resume_requires_current_parent_policy(tmp_path: Path) -> None:
    runner, agent, tool = make_resume_runner(tmp_path)
    memory = Session("child-resume", InMemorySessionStore())

    async def exercise() -> None:
        interrupted = await run_agent(
            runner,
            agent,
            "approve",
            session=memory,
            limits=RunLimits(),
            parent_run_id="parent-run",
            parent_policy=effective_policy(RunLimits(), runner.host_policy),
        )
        assert isinstance(interrupted, RunSuspension)
        with pytest.raises(RuntimeStateError, match="current parent policy"):
            await resume_agent(
                runner,
                agent,
                interrupted.checkpoint,
                session=memory,
                decisions=(approved_resolution(),),
                limits=RunLimits(),
            )
        assert tool.contexts == []
        assert await active_step(memory) is not None

    asyncio.run(exercise())


def test_resume_deadline_cancels_waiting_tool_and_preserves_pending_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, agent, tool = make_resume_runner(tmp_path)
    memory = Session("resume-deadline", InMemorySessionStore())

    async def exercise() -> None:
        interrupted = await run_agent(runner, agent, "approve", session=memory, limits=RunLimits())
        assert isinstance(interrupted, RunSuspension)
        timer = asyncio.Timeout(None)
        cleaned = asyncio.Event()

        async def block() -> None:
            timer.reschedule(asyncio.get_running_loop().time())
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()

        tool.on_resume = block
        monkeypatch.setattr(asyncio, "timeout", lambda delay: timer)
        result = await resume_agent(
            runner,
            agent,
            interrupted.checkpoint,
            session=memory,
            decisions=(approved_resolution(),),
            limits=RunLimits(),
        )
        assert result.state is RunStatus.FAILED
        assert isinstance(result, RunResult)
        assert cleaned.is_set()
        assert result.reason == "timeout"
        snapshot = await memory.store.snapshot(memory.session_id)
        assistant = snapshot.steps[-1].messages[1]
        assert isinstance(assistant, AssistantMessage)
        assert assistant.tool_calls[0].id == "call-1"
        assert snapshot.steps[-1].state == "aborted"

    asyncio.run(exercise())


def test_resume_at_step_limit_leaves_memory_available_for_next_run(tmp_path: Path) -> None:
    runner, agent, tool = make_resume_runner(tmp_path)
    memory = Session("resume-limit", InMemorySessionStore())

    async def exercise() -> None:
        interrupted = await run_agent(runner, agent, "approve", session=memory, limits=RunLimits(max_model_steps=1))
        assert isinstance(interrupted, RunSuspension)
        result = await resume_agent(
            runner,
            agent,
            interrupted.checkpoint,
            session=memory,
            decisions=(approved_resolution(),),
            limits=RunLimits(),
        )
        assert result.state is RunStatus.FAILED
        assert isinstance(result, RunResult)
        assert await active_step(memory) is None
        assert len(await memory.history()) == 1
        assert tool.contexts[0].parent_limits is not None
        assert tool.contexts[0].parent_limits.remaining_model_steps == 0
        next_result = await run_agent(runner, agent, "next", session=memory, limits=RunLimits())
        assert next_result.state is RunStatus.SUCCEEDED
        assert isinstance(next_result, RunResult)
        assert next_result.final_output == "resumed"
        assert len(tool.contexts) == 1

    asyncio.run(exercise())


def test_detached_event_subscriber_does_not_cancel_the_owned_run(tmp_path: Path) -> None:
    model = BlockingOpenAIModel()
    model.batches = iter((text_batch("completed", "after-detach"),))
    runner, agent = make_runner(model, tmp_path)

    async def scenario() -> None:
        handle = await start_agent(runner, agent, "keep working")
        async with handle:
            await model.started.wait()
            async for first in handle.events():
                assert first.event["kind"] == "run.started"
                break
            assert handle.task is not None and not handle.task.done()
            model.release.set()
            result = await handle.result()
            assert isinstance(result, RunResult) and result.state is RunStatus.SUCCEEDED
            assert result.final_output == "completed"

    asyncio.run(scenario())


def test_event_journal_replays_more_than_one_page_without_blocking_execution(tmp_path: Path) -> None:
    chunks = tuple(
        OpenAIStreamChunk(
            id="many", model="fake-model", choices=(OpenAIStreamChoice(delta=OpenAIStreamDelta(content="x")),)
        )
        for index in range(300)
    )
    model = FakeOpenAIModel((chunks,))
    runner, agent = make_runner(model, tmp_path)

    async def scenario() -> None:
        handle = await start_agent(runner, agent, "generate")
        async with handle:
            result = await handle.result()
            assert isinstance(result, RunResult) and result.final_output == "x" * 300
            records = [record async for record in handle.events()]
            assert sum(record.event["kind"] == "message.text.delta" for record in records) == 300
            assert [record.sequence for record in records] == list(range(1, len(records) + 1))
            replay = [record async for record in handle.events(after_sequence=records[255].sequence)]
            assert replay == records[256:]

    asyncio.run(scenario())


def test_runner_omits_budget_pruned_references_from_wire_request(tmp_path: Path) -> None:
    model = FakeOpenAIModel((text_batch("answer", "response-1"),))
    runner, agent = make_runner(model, tmp_path)
    memory = Session("context-pruning", InMemorySessionStore())
    config = RunLimits(
        context_budget=ContextBudget(window_tokens=3_000, output_reserve=100, compact_at=2_800, recent_tokens=100)
    )
    result = asyncio.run(
        run_agent(
            runner,
            agent,
            "question",
            session=memory,
            limits=config,
            context=(ContextBlock(kind="document", origin="review", content="PRUNED_REFERENCE " + "x" * 20_000),),
        )
    )
    assert result.state is RunStatus.SUCCEEDED
    assert isinstance(result, RunResult)
    assert "PRUNED_REFERENCE" not in model.requests[0].model_dump_json()
    assert input_text(asyncio.run(memory.history())[0].messages[0].content) == "question"


def test_run_missing_usage_remains_unknown_and_charges_request_budget(tmp_path: Path) -> None:
    model = FakeOpenAIModel((text_batch("answer", "unknown"),), report_usage=False)
    runner, agent = make_runner(model, tmp_path)
    events: list[Event] = []

    async def collect(event: Event) -> None:
        events.append(event)

    runner.event_sink = collect
    result = asyncio.run(
        run_agent(
            runner,
            agent,
            "answer",
            session=Session("unknown-usage", InMemorySessionStore()),
            limits=RunLimits(max_output_tokens=5),
        )
    )
    assert result.state is RunStatus.SUCCEEDED
    assert isinstance(result, RunResult)
    assert result.usage.input_tokens is None
    assert result.usage.output_tokens is None
    assert result.usage.total_tokens is None
    assert result.usage.budget_output_tokens == 5
    event = next(item for item in events if isinstance(item, ModelCompletedEvent))
    assert event.input_tokens is None and event.output_tokens is None
    assert model.requests[0].stream_options == {"include_usage": True}


def test_run_stops_before_a_second_model_request_when_output_usage_is_unknown(tmp_path: Path) -> None:
    model = FakeOpenAIModel((tool_batch("call-1"), text_batch("must not run", "unexpected")), report_usage=False)
    runner, agent = make_runner(model, tmp_path)
    result = asyncio.run(
        run_agent(
            runner,
            agent,
            "use a tool",
            session=Session("unknown-budget", InMemorySessionStore()),
            limits=RunLimits(max_output_tokens=5),
        )
    )
    assert result.state is RunStatus.FAILED
    assert isinstance(result, RunResult)
    assert result.usage.output_tokens is None
    assert result.usage.budget_output_tokens == 5
    assert len(model.requests) == 1


def test_model_requests_use_only_remaining_run_output_budget(tmp_path: Path) -> None:
    model = FakeOpenAIModel((tool_batch("call-1"), text_batch("done", "last")))
    runner, agent = make_runner(model, tmp_path)
    result = asyncio.run(
        run_agent(
            runner,
            agent,
            "use a tool",
            session=Session("remaining-budget", InMemorySessionStore()),
            limits=RunLimits(max_output_tokens=3),
        )
    )
    assert result.state is RunStatus.SUCCEEDED
    assert isinstance(result, RunResult)
    assert result.usage.output_tokens == 2
    assert result.usage.budget_output_tokens == 2
    assert [request.max_completion_tokens for request in model.requests] == [3, 2]


def test_resume_retains_unknown_usage_budget_and_closes_step_at_output_limit(tmp_path: Path) -> None:
    model = FakeOpenAIModel((tool_batch("call-1"), text_batch("must not run", "unexpected")), report_usage=False)
    runner, agent = make_runner(model, tmp_path, interrupting_tool)
    memory = Session("unknown-resume", InMemorySessionStore())

    async def scenario() -> None:
        paused = await run_agent(runner, agent, "work", session=memory, limits=RunLimits(max_output_tokens=5))
        assert paused.state is RunStatus.PAUSED
        assert isinstance(paused, RunSuspension)
        assert paused.checkpoint.version == 1
        assert paused.checkpoint.usage.budget_output_tokens == 5
        now = datetime.now(UTC)
        response = ResumeDecision(
            interruption_id=paused.interruptions[0].interruption_id,
            result=ToolResult(call_id="call-1", ok=True, output={"approved": True}, started_at=now, finished_at=now),
        )
        resumed = await resume_agent(
            runner,
            agent,
            paused.checkpoint,
            session=memory,
            decisions=(response,),
            limits=RunLimits(max_output_tokens=5),
        )
        assert resumed.state is RunStatus.FAILED
        assert isinstance(resumed, RunResult)
        assert resumed.usage.output_tokens is None
        assert resumed.usage.budget_output_tokens == 5
        assert len(model.requests) == 1
        assert await active_step(memory) is None

    asyncio.run(scenario())


@pytest.mark.parametrize("version", [2, 3])
def test_run_checkpoint_rejects_superseded_versions(tmp_path: Path, version: int) -> None:
    model = FakeOpenAIModel((tool_batch("call-1"),))
    runner, agent = make_runner(model, tmp_path, interrupting_tool)
    paused = asyncio.run(
        run_agent(
            runner,
            agent,
            "work",
            session=Session("old-checkpoint", InMemorySessionStore()),
            limits=RunLimits(),
        )
    )
    assert isinstance(paused, RunSuspension)
    old = paused.checkpoint.model_dump(mode="json")
    old["version"] = version
    with pytest.raises(ValidationError, match="version"):
        RunCheckpoint.model_validate(old)


def test_run_config_rejects_the_superseded_turn_limit() -> None:
    with pytest.raises(ValidationError, match="max_turns"):
        RunLimits.model_validate({"max_turns": 1})


def test_runner_reads_a_large_file_then_reloads_its_original_result_by_reference(tmp_path: Path) -> None:
    class ReloadingModel(FakeOpenAIModel):
        async def stream(self, request: OpenAIRequest | Mapping[str, object]) -> AsyncIterator[OpenAIStreamChunk]:
            if len(self.requests) == 1:
                if not isinstance(request, OpenAIRequest):
                    raise TypeError("fake model requires a parsed OpenAI request")
                observation = request.messages[-1]
                assert observation.role == "tool" and isinstance(observation.content, str)
                reference = TypeAdapter[JsonObject](JsonObject).validate_json(observation.content)
                assert reference["compacted"] is True
                self.batches = iter(
                    (
                        tool_batch(
                            "reload-call",
                            name="read_tool_result",
                            arguments=json.dumps(reference["read_tool_result"]),
                        ),
                        text_batch("Evidence read successfully", "final-response"),
                    )
                )
            async for chunk in super().stream(request):
                yield chunk

    model = ReloadingModel(
        (tool_batch("file-call", name="read_file", arguments='{"scope":"workspace","path":"evidence.txt"}'),)
    )
    runner, agent = make_runner(model, tmp_path)
    (tmp_path / "workspace" / "evidence.txt").write_text("evidence " * 15_000, encoding="utf-8")
    runner.host_policy = HostPolicy(grants=CapabilityGrants(capabilities=frozenset({"file.read"})))
    memory = Session("read-evidence", SqliteSessionStore(tmp_path / "memory.sqlite3"))

    async def scenario() -> None:
        result = await run_agent(runner, agent, "Read evidence.txt", session=memory, limits=RunLimits())
        assert result.state is RunStatus.SUCCEEDED
        assert isinstance(result, RunResult)
        assert result.final_output == "Evidence read successfully"
        original = next(message for message in (await memory.history())[0].messages if isinstance(message, ToolMessage))
        assert len(original.content) > 100_000
        read_back = model.requests[2].messages[-1]
        assert read_back.role == "tool" and isinstance(read_back.content, str)
        page_result = TypeAdapter[JsonObject](JsonObject).validate_json(read_back.content)
        page = page_result["output"]
        assert isinstance(page, dict)
        assert page["content"] == original.content[:4_000]
        assert page["truncated"] is True
        assert page["next_start_char"] == 4_000
        restored = Session("read-evidence", SqliteSessionStore(tmp_path / "memory.sqlite3"))
        assert (
            next(
                message
                for step in await restored.history()
                for message in step.messages
                if message.message_id == original.message_id
            )
            == original
        )

    asyncio.run(scenario())


class BackgroundBackend:
    """Expose an external work barrier through a real FunctionTool binding."""

    def __init__(self, *, hold_admission: bool = False, reject: bool = False) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.admitting = asyncio.Event()
        self.admit = asyncio.Event()
        self.reject = reject
        if not hold_admission:
            self.admit.set()

    async def execute(self, context: ToolContext, arguments: JsonObject) -> JsonObject:
        """Remain active until completion or owner cancellation is observed."""
        self.started.set()
        try:
            await self.release.wait()
            return {"evidence": arguments["value"]}
        except asyncio.CancelledError:
            self.cancelled.set()
            raise


class BackgroundTestTool(FunctionTool):
    """Model a backend that explicitly accepts stable work and reports stop certainty."""

    def __init__(self, descriptor: ToolDescriptor, backend: BackgroundBackend, *, confirmed_stop: bool) -> None:
        super().__init__(descriptor, backend.execute, EchoArguments, policy=ToolPolicy(background=True))
        self.backend = backend
        self.confirmed_stop = confirmed_stop

    @property
    def supports_background(self) -> bool:
        return True

    async def execute_background(
        self, context: ToolContext, arguments: JsonObject, admission: BackgroundAdmission
    ) -> JsonObject:
        submission = ToolSubmission(
            operation_id=admission.operation_id,
            request_digest=hashlib.sha256(json.dumps(arguments, sort_keys=True).encode()).hexdigest(),
        )
        await admission.prepare(submission)
        self.backend.admitting.set()
        await self.backend.admit.wait()
        if self.backend.reject:
            raise BackgroundNotAcceptedError("The external backend rejected this work.")
        await admission.accept(
            ToolAcceptance(
                operation_id=admission.operation_id,
                backend="test",
                reference=admission.operation_id,
                request_digest=submission.request_digest,
            )
        )
        try:
            return await self.backend.execute(context, arguments)
        except asyncio.CancelledError as error:
            raise ToolCancelledError(confirmed_stop=self.confirmed_stop) from error


class StepBarriers:
    """Signal only persisted step completion through the public event boundary."""

    def __init__(self, count: int) -> None:
        self.events = tuple(asyncio.Event() for number in range(count))
        self.completed = 0

    async def receive(self, event: Event) -> None:
        """Release the corresponding barrier after the completed step is committed."""
        if isinstance(event, ModelStepCompletedEvent):
            self.events[self.completed].set()
            self.completed += 1


def background_runner(
    model: FakeOpenAIModel,
    backend: BackgroundBackend,
    barriers: StepBarriers,
    *,
    confirmed_stop: bool = False,
    observability: Observability | None = None,
) -> tuple[Runner, Agent]:
    """Bind a host-selected background backend without changing runtime mechanisms."""
    registry = ToolRegistry()
    descriptor = ToolDescriptor(name="echo", code="echo", description="Collect external evidence")
    registry.register(
        descriptor,
        lambda selected: BackgroundTestTool(selected, backend, confirmed_stop=confirmed_stop),
    )
    runner = Runner(
        tools=registry,
        skills=SkillLoader(),
        tool_runtime=ToolRuntime(),
        event_sink=barriers.receive,
        observability=observability,
    )
    return runner, Agent(
        name="background", prompt="Collect and report evidence.", model=model, tools=(ToolRef("echo"),)
    )


def test_background_completion_is_a_new_input_after_one_started_tool_message() -> None:
    """An accepted backend emits one paired startup result and a separate final observation."""

    async def scenario() -> None:
        backend = BackgroundBackend()
        barriers = StepBarriers(3)
        model = FakeOpenAIModel(
            (
                tool_batch("external-call"),
                text_batch("Waiting for evidence", "waiting"),
                text_batch("Evidence received", "final"),
            )
        )
        runner, agent = background_runner(model, backend, barriers)
        session = Session("background-complete", InMemorySessionStore())
        handle = await start_agent(runner, agent, "Collect evidence", session=session)
        async with handle:
            await asyncio.wait_for(backend.started.wait(), timeout=3)
            await asyncio.wait_for(barriers.events[1].wait(), timeout=3)
            before = await session.store.snapshot(session.session_id)
            paired = [value.message for value in before.messages if isinstance(value.message, ToolMessage)]
            assert len(paired) == 1
            startup = TypeAdapter[JsonObject](JsonObject).validate_json(paired[0].content)
            assert paired[0].tool_call_id == "external-call"
            output = startup["output"]
            assert isinstance(output, dict) and output["status"] == "started"
            assert len(before.operations) == 1 and before.operations[0].state == "running"
            assert not any(value.envelope.kind == "operation_result" for value in before.inputs)
            backend.release.set()
            result = await handle.result()
        assert isinstance(result, RunResult) and result.state is RunStatus.SUCCEEDED
        assert result.final_output == "Evidence received"
        after = await session.store.snapshot(session.session_id)
        paired_after = [value.message for value in after.messages if isinstance(value.message, ToolMessage)]
        assert len(paired_after) == 1 and paired_after[0] == paired[0]
        final_inputs = [value for value in after.inputs if value.envelope.kind == "operation_result"]
        assert len(final_inputs) == 1 and final_inputs[0].step_id is not None
        assert final_inputs[0].envelope.causation_id == after.operations[0].operation_id
        assert after.operations[0].state == "succeeded" and after.operations[0].result is not None
        assert after.operations[0].result.output == {"evidence": "x"}
        assert "operation_result" in model.requests[-1].model_dump_json()
        assert result.usage.tool_calls == 1 and result.usage.model_calls == 3

    asyncio.run(scenario())


def test_live_input_during_a_model_request_advances_without_a_background_operation(tmp_path: Path) -> None:
    """A live follow-up starts the next safe step without inventing a background wait."""

    async def scenario() -> None:
        entered, release = asyncio.Event(), asyncio.Event()

        class GatedModel(FakeOpenAIModel):
            async def stream(self, request: OpenAIRequest | Mapping[str, object]) -> AsyncIterator[OpenAIStreamChunk]:
                if not self.requests:
                    entered.set()
                    await release.wait()
                async for chunk in super().stream(request):
                    yield chunk

        model = GatedModel((text_batch("First response", "first"), text_batch("Follow-up completed", "second")))
        runner, agent = make_runner(model, tmp_path)
        session = Session("live-without-background", InMemorySessionStore())
        await session.enqueue(InputEnvelope(input_id="original", kind="user", source="test", content="Start work"))
        handle = await runner.start(
            agent,
            session=session,
            invocation_id="live-call",
            input_ids=("original",),
            input_mode="live",
            limits=RunLimits(),
        )
        async with handle:
            async with asyncio.timeout(3):
                await entered.wait()
                await session.enqueue(
                    InputEnvelope(input_id="follow-up", kind="user", source="test", content="Use the primary source")
                )
                release.set()
                result = await handle.result()
        assert isinstance(result, RunResult) and result.final_output == "Follow-up completed"
        assert result.usage.model_calls == 2 and result.run_id == handle.run_id
        assert "Use the primary source" in model.requests[1].model_dump_json()
        journal = await session.store.snapshot(session.session_id)
        assert len(journal.runs) == 1 and journal.operations == ()
        assert [step.input_ids for step in journal.steps] == [("original",), ("follow-up",)]
        assert all(step.completed for step in journal.steps)

    asyncio.run(scenario())


def test_live_user_input_reaches_the_same_run_while_its_background_backend_is_blocked() -> None:
    """Live intake advances a safe model step before an already owned backend settles."""

    async def scenario() -> None:
        backend = BackgroundBackend()
        barriers = StepBarriers(4)
        model = FakeOpenAIModel(
            (
                tool_batch("external-call"),
                text_batch("Waiting", "waiting"),
                text_batch("Updated direction accepted", "updated"),
                text_batch("Finished", "final"),
            )
        )
        runner, agent = background_runner(model, backend, barriers)
        session = Session("background-live", InMemorySessionStore())
        await session.enqueue(
            InputEnvelope(input_id="original", kind="user", source="test", content="Collect evidence")
        )
        handle = await runner.start(
            agent,
            session=session,
            invocation_id="live-call",
            input_ids=("original",),
            input_mode="live",
            limits=RunLimits(),
        )
        async with handle:
            await asyncio.wait_for(backend.started.wait(), timeout=3)
            await asyncio.wait_for(barriers.events[1].wait(), timeout=3)
            await session.enqueue(
                InputEnvelope(input_id="new-input", kind="user", source="test", content="Focus on the primary source")
            )
            await asyncio.wait_for(barriers.events[2].wait(), timeout=3)
            assert "Focus on the primary source" in model.requests[2].model_dump_json()
            assert not backend.release.is_set()
            state = await session.store.snapshot(session.session_id)
            assert state.operations[0].state == "running"
            update = next(value for value in state.inputs if value.envelope.input_id == "new-input")
            assert update.run_id == handle.run_id and update.step_id is not None
            backend.release.set()
            result = await handle.result()
        assert isinstance(result, RunResult) and result.state is RunStatus.SUCCEEDED
        assert result.run_id == handle.run_id and result.final_output == "Finished"
        assert result.usage.model_calls == 4

    asyncio.run(scenario())


@pytest.mark.parametrize("confirmed", [False, True], ids=["unknown-external-effect", "confirmed-backend-stop"])
def test_background_cancellation_preserves_unknown_effects_until_the_backend_confirms_stop(confirmed: bool) -> None:
    """Owner cancellation records backend certainty without inventing a final Tool result."""

    async def scenario() -> None:
        backend = BackgroundBackend()
        barriers = StepBarriers(2)
        model = FakeOpenAIModel((tool_batch("external-call"), text_batch("Waiting", "waiting")))
        runner, agent = background_runner(model, backend, barriers, confirmed_stop=confirmed)
        session = Session("background-cancel", InMemorySessionStore())
        handle = await start_agent(runner, agent, "Collect evidence", session=session)
        async with handle:
            await asyncio.wait_for(backend.started.wait(), timeout=3)
            await asyncio.wait_for(barriers.events[1].wait(), timeout=3)
            result = await handle.cancel(reason="host_cancelled")
        assert result.state is RunStatus.CANCELLED and backend.cancelled.is_set()
        state = await session.store.snapshot(session.session_id)
        assert state.operations[0].state == ("cancelled" if confirmed else "unknown")
        assert state.operations[0].result is None
        assert not any(value.envelope.kind == "operation_result" for value in state.inputs)
        paired = [value.message for value in state.messages if isinstance(value.message, ToolMessage)]
        assert len(paired) == 1 and paired[0].tool_call_id == "external-call"
        assert not backend.release.is_set()

    asyncio.run(scenario())


@pytest.mark.parametrize("changed", ["timeout", "deadline", "steps", "output", "concurrency", "grants"])
def test_start_retry_rejects_changed_parent_restrictions_but_keeps_identical_invocation(
    tmp_path: Path, changed: str
) -> None:
    """Idempotency compares host-supplied parent restrictions without recomputed local deadlines."""

    async def scenario() -> None:
        model = FakeOpenAIModel((text_batch("accepted", "first"),))
        runner, agent = make_runner(model, tmp_path)
        session = Session("parent-fingerprint", InMemorySessionStore())
        await session.enqueue(InputEnvelope(input_id="input", kind="user", source="host", content="Question"))
        limits = RunLimits()
        parent = effective_policy(limits, HostPolicy())
        handle = await runner.start(
            agent,
            session=session,
            invocation_id="same",
            input_ids=("input",),
            input_mode="batch",
            limits=limits,
            parent_run_id="parent",
            parent_policy=parent,
        )
        async with handle:
            assert (await handle.result()).state is RunStatus.SUCCEEDED
        retried = await runner.start(
            agent,
            session=session,
            invocation_id="same",
            input_ids=("input",),
            input_mode="batch",
            limits=limits,
            parent_run_id="parent",
            parent_policy=parent,
        )
        assert retried is handle and len(model.requests) == 1
        match changed:
            case "timeout":
                different = replace(parent, timeout_seconds=parent.timeout_seconds - 1)
            case "deadline":
                different = replace(parent, deadline=parent.deadline - timedelta(seconds=1))
            case "steps":
                different = replace(parent, max_model_steps=parent.max_model_steps - 1)
            case "output":
                different = replace(parent, max_output_tokens=parent.max_output_tokens - 1)
            case "concurrency":
                different = replace(parent, tool_concurrency=parent.tool_concurrency - 1)
            case _:
                different = replace(parent, grants=CapabilityGrants(resource_read=frozenset({"different-resource"})))
        with pytest.raises(SessionConflictError, match="different parameters"):
            await runner.start(
                agent,
                session=session,
                invocation_id="same",
                input_ids=("input",),
                input_mode="batch",
                limits=limits,
                parent_run_id="parent",
                parent_policy=different,
            )
        assert len(model.requests) == 1

    asyncio.run(scenario())


class TerminalCommitBarrierStore(SqliteSessionStore):
    """Hold the real SQLite thread after its first terminal receipt is committed."""

    def __init__(self, database: Path) -> None:
        super().__init__(database)
        self.committed = ThreadEvent()
        self.release = ThreadEvent()
        self.blocked = False

    def transact_sync[T](self, session_id: str, mutation: Callable[[SessionState], tuple[SessionState, T]]) -> T:
        result = super().transact_sync(session_id, mutation)
        if not self.blocked and any(value.receipt is not None for value in self.snapshot_sync(session_id).runs):
            self.blocked = True
            self.committed.set()
            if not self.release.wait(timeout=5):
                raise TimeoutError("The test did not release its terminal SQLite commit")
        return result


@pytest.mark.parametrize("succeeded", [True, False], ids=["succeeded", "failed-at-step-limit"])
def test_cancellation_after_terminal_receipt_commit_preserves_that_result_and_releases_the_writer(
    tmp_path: Path, succeeded: bool
) -> None:
    """An already committed terminal result survives cancellation during its acknowledgement."""

    async def scenario() -> None:
        batch = text_batch("Committed answer", "finished") if succeeded else tool_batch("unavailable-call")
        model = FakeOpenAIModel((batch,))
        runner, agent = make_runner(model, tmp_path)
        store = TerminalCommitBarrierStore(tmp_path / "terminal-commit.sqlite3")
        session = Session("terminal-cancel", store)
        handle = await start_agent(runner, agent, "Answer", session=session, limits=RunLimits(max_model_steps=1))
        cancel_started = asyncio.Event()

        async def cancel_owner() -> RunResult:
            cancel_started.set()
            return await handle.cancel(reason="host_disconnected_after_commit")

        async with handle:
            assert await asyncio.to_thread(store.committed.wait, 3)
            cancellation = asyncio.create_task(cancel_owner())
            try:
                await cancel_started.wait()
                assert not cancellation.done()
            finally:
                store.release.set()
            result = await cancellation
            assert result.state is (RunStatus.SUCCEEDED if succeeded else RunStatus.FAILED)
            assert result.final_output == ("Committed answer" if succeeded else "")
            if not succeeded:
                assert result.reason == "limit_reached"
            assert await handle.result() == result
        state = await store.snapshot(session.session_id)
        assert state.current_run_id is None
        assert state.runs[0].receipt == result.receipt
        assert len(state.steps) == 1 and state.steps[0].completed
        assert not any(value.kind == "run.cancelled" for value in state.events)
        assert len(model.requests) == 1

    asyncio.run(scenario())


def test_cancelling_after_a_completed_response_preserves_the_step_without_cleanup_errors(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    async def scenario() -> None:
        completed, release = asyncio.Event(), asyncio.Event()
        model = FakeOpenAIModel((text_batch('{"answer":7}', "invalid-schema"),))
        runner, agent = make_runner(model, tmp_path)
        session = Session("completed-response-cancel", InMemorySessionStore())

        async def observe(event: Event) -> None:
            if isinstance(event, ModelStepCompletedEvent):
                completed.set()
                await release.wait()

        runner.event_sink = observe
        handle = await start_agent(runner, agent, "Answer", session=session)
        async with handle:
            await asyncio.wait_for(completed.wait(), 3)
            result = await handle.cancel(reason="host_cancelled_after_response")
        assert result.state is RunStatus.CANCELLED
        history = await session.history()
        assert len(history) == 1 and history[0].completed
        assert history[0].messages[-1].content == '{"answer":7}'
        assert not any("memory step cleanup failed" in record.message for record in caplog.records)

    asyncio.run(scenario())


def test_background_started_waits_for_concrete_backend_admission() -> None:
    """Pending backend admission cannot publish a running state or startup ToolMessage."""

    async def scenario() -> None:
        backend = BackgroundBackend(hold_admission=True)
        barriers = StepBarriers(3)
        model = FakeOpenAIModel((tool_batch("pending"), text_batch("Waiting", "wait"), text_batch("Done", "final")))
        runner, agent = background_runner(model, backend, barriers)
        session = Session("admission-barrier", InMemorySessionStore())
        handle = await start_agent(runner, agent, "Collect evidence", session=session)
        async with handle:
            await asyncio.wait_for(backend.admitting.wait(), timeout=3)
            before = await session.store.snapshot(session.session_id)
            assert before.operations[0].state == "prepared"
            assert before.operations[0].backend_receipt is None
            assert not any(isinstance(value.message, ToolMessage) for value in before.messages)
            assert len(model.requests) == 1
            backend.admit.set()
            await asyncio.wait_for(barriers.events[1].wait(), timeout=3)
            accepted = await session.store.snapshot(session.session_id)
            receipt = accepted.operations[0].backend_receipt
            assert receipt is not None and receipt.backend == "test"
            assert receipt.reference == accepted.operations[0].operation_id
            backend.release.set()
            result = await handle.result()
        assert isinstance(result, RunResult) and result.state is RunStatus.SUCCEEDED

    asyncio.run(scenario())


def test_background_rejection_persists_no_receipt_and_returns_only_original_failed_tool_result(tmp_path: Path) -> None:
    """A rejected request survives SQLite reload without inventing admission or completion input."""

    async def scenario() -> None:
        backend = BackgroundBackend(reject=True)
        barriers = StepBarriers(2)
        model = FakeOpenAIModel((tool_batch("rejected"), text_batch("Backend unavailable", "final")))
        runner, agent = background_runner(model, backend, barriers)
        store = SqliteSessionStore(tmp_path / "rejected.sqlite3")
        session = Session("rejected-background", store)
        outcome = await run_agent(runner, agent, "Collect evidence", session=session)
        assert isinstance(outcome, RunResult) and outcome.state is RunStatus.SUCCEEDED
        state = await SqliteSessionStore(tmp_path / "rejected.sqlite3").snapshot(session.session_id)
        operation = state.operations[0]
        assert operation.state == "failed" and operation.backend_receipt is None
        assert operation.result is not None and not operation.result.ok
        assert operation.result.error_code == "tool.background.not_accepted"
        assert not any(value.envelope.kind == "operation_result" for value in state.inputs)
        paired = [value.message for value in state.messages if isinstance(value.message, ToolMessage)]
        assert len(paired) == 1 and paired[0].tool_call_id == "rejected"
        observation = TypeAdapter[JsonObject](JsonObject).validate_json(paired[0].content)
        assert observation["ok"] is False and observation["output"] is None
        assert not backend.started.is_set()

    asyncio.run(scenario())


def test_generic_function_tool_cannot_claim_background_without_an_admission_contract() -> None:
    """Host background policy requires a real implementation before accepting the Run."""

    async def scenario() -> None:
        registry = ToolRegistry()
        descriptor = ToolDescriptor(name="echo", code="echo", description="Echo")
        registry.register(
            descriptor,
            lambda selected: FunctionTool(selected, successful_tool, EchoArguments, policy=ToolPolicy(background=True)),
        )
        runner = Runner(tools=registry, skills=SkillLoader(), tool_runtime=ToolRuntime())
        agent = Agent(name="generic", prompt="Echo", model=FakeOpenAIModel(()), tools=(ToolRef("echo"),))
        session = Session("unsupported-background", InMemorySessionStore())
        with pytest.raises(AgentConfigurationError, match="Background"):
            await start_agent(runner, agent, "Echo", session=session)
        assert (await session.store.snapshot(session.session_id)).runs == ()

    asyncio.run(scenario())


@pytest.mark.parametrize("settlement", ["succeeded", "cancelled", "failed"])
def test_operation_metrics_follow_actual_backend_admission_and_persisted_settlement(settlement: str) -> None:
    """Real Run boundaries count accepted work and leave no active operation after settlement."""

    async def scenario() -> None:
        reader = InMemoryMetricReader()
        provider = MeterProvider(metric_readers=[reader])
        try:
            backend = BackgroundBackend(reject=settlement == "failed")
            barriers = StepBarriers(3)
            model = FakeOpenAIModel(
                (tool_batch("measured"), text_batch("Waiting", "wait"), text_batch("Done", "final"))
            )
            runner, agent = background_runner(
                model,
                backend,
                barriers,
                confirmed_stop=True,
                observability=Observability(meter_provider=provider),
            )
            session = Session("measured-operation", InMemorySessionStore())
            handle = await start_agent(runner, agent, "Measure this work", session=session)
            async with handle:
                if settlement == "failed":
                    await handle.result()
                else:
                    await asyncio.wait_for(barriers.events[1].wait(), timeout=3)
                    if settlement == "cancelled":
                        await handle.cancel(reason="host_cancelled")
                    else:
                        backend.release.set()
                        await handle.result()
            data = reader.get_metrics_data()
            assert data is not None
            observed = {
                metric.name: metric.data
                for resource in data.resource_metrics
                for scope in resource.scope_metrics
                for metric in scope.metrics
            }
            transitions = observed["pleiad.operation.transitions"]
            assert isinstance(transitions, Sum)
            counts = {
                str(point.attributes["status"]): point.value for point in transitions.data_points if point.attributes
            }
            assert counts == ({"failed": 1} if settlement == "failed" else {"started": 1, settlement: 1})
            active = observed.get("pleiad.operation.active")
            if settlement == "failed":
                assert active is None
            else:
                assert isinstance(active, Sum) and active.data_points[0].value == 0
            duration = observed["pleiad.operation.duration"]
            assert isinstance(duration, Histogram)
            assert len(duration.data_points) == 1 and duration.data_points[0].count == 1
            assert duration.data_points[0].sum >= 0
        finally:
            provider.shutdown()

    asyncio.run(scenario())


class CleanupBarrierBackend(BackgroundBackend):
    """Hold real external stop acknowledgement independently from repeated host requests."""

    def __init__(self) -> None:
        super().__init__()
        self.cleanup = asyncio.Event()
        self.cleaned = False

    async def execute(self, context: ToolContext, arguments: JsonObject) -> JsonObject:
        self.started.set()
        try:
            await self.release.wait()
            return {"evidence": arguments["value"]}
        except asyncio.CancelledError:
            self.cancelled.set()
            await self.cleanup.wait()
            self.cleaned = True
            raise


def test_repeated_handle_cancellation_waits_for_the_same_backend_cleanup() -> None:
    """Concurrent cancellation callers share one stop and observe its committed result."""

    async def scenario() -> None:
        backend = CleanupBarrierBackend()
        barriers = StepBarriers(2)
        model = FakeOpenAIModel((tool_batch("owned-stop"), text_batch("Waiting", "wait")))
        runner, agent = background_runner(model, backend, barriers, confirmed_stop=True)
        session = Session("repeated-cancel", InMemorySessionStore())
        handle = await start_agent(runner, agent, "Collect evidence", session=session)
        second_entered = asyncio.Event()

        async def second_cancel() -> RunResult:
            second_entered.set()
            return await handle.cancel(reason="repeated-stop")

        async with handle, asyncio.TaskGroup() as group:
            await asyncio.wait_for(barriers.events[1].wait(), timeout=3)
            first = group.create_task(handle.cancel(reason="original-stop"))
            await asyncio.wait_for(backend.cancelled.wait(), timeout=3)
            second = group.create_task(second_cancel())
            await second_entered.wait()
            backend.cleanup.set()
        assert backend.cleaned
        assert first.result() == second.result()
        assert first.result().state is RunStatus.CANCELLED and first.result().reason == "original-stop"
        state = await session.store.snapshot(session.session_id)
        assert state.operations[0].state == "cancelled" and state.current_run_id is None

    asyncio.run(scenario())


class ReadOnlyStopStore(SqliteSessionStore):
    """Inject actual SQLite write refusal after external work was admitted."""

    def __init__(self, database: Path) -> None:
        super().__init__(database)
        self.read_only = False

    def connect(self) -> sqlite3.Connection:
        connection = super().connect()
        if self.read_only:
            connection.execute("PRAGMA query_only=ON")
        return connection


def test_background_cleanup_still_stops_owned_work_when_cancellation_intent_cannot_be_persisted(tmp_path: Path) -> None:
    """A real database write failure preserves its error while draining the owned backend."""

    async def scenario() -> None:
        backend = BackgroundBackend()
        barriers = StepBarriers(2)
        model = FakeOpenAIModel((tool_batch("blocked-storage"), text_batch("Waiting", "wait")))
        runner, agent = background_runner(model, backend, barriers, confirmed_stop=True)
        store = ReadOnlyStopStore(tmp_path / "read-only-stop.sqlite3")
        handle = await start_agent(runner, agent, "Collect evidence", session=Session("disk-failure", store))
        await asyncio.wait_for(barriers.events[1].wait(), timeout=3)
        store.read_only = True
        try:
            with pytest.raises((SessionStorageError, ExceptionGroup)):
                await asyncio.wait_for(handle.cancel(reason="host_cancelled"), timeout=3)
            assert backend.cancelled.is_set()
        finally:
            store.read_only = False
            backend.release.set()

    asyncio.run(scenario())


@pytest.mark.parametrize("persisted", [False, True])
def test_a_megabyte_background_result_fits_context_and_remains_completely_readable_in_its_session(
    tmp_path: Path, persisted: bool
) -> None:
    class LargeBackend(BackgroundBackend):
        async def execute(self, context: ToolContext, arguments: JsonObject) -> JsonObject:
            self.started.set()
            await self.release.wait()
            return {"evidence": "原始证据🙂" * 90_000}

    class ReadingModel(FakeOpenAIModel):
        operation_id: str | None = None

        async def stream(self, request: OpenAIRequest | Mapping[str, object]) -> AsyncIterator[OpenAIStreamChunk]:
            if len(self.requests) == 2:
                if self.operation_id is None:
                    raise ValueError("The backend must accept the operation before the reader requests its result")
                self.batches = iter(
                    (
                        tool_batch(
                            "read-operation",
                            name="read_tool_result",
                            arguments=json.dumps({"operation_id": self.operation_id, "max_chars": 1_000}),
                        ),
                        text_batch("Original operation evidence was read", "complete"),
                    )
                )
            async for chunk in super().stream(request):
                yield chunk

    async def scenario() -> None:
        backend = LargeBackend()
        barriers = StepBarriers(4)
        model = ReadingModel((tool_batch("external-call"), text_batch("Waiting for the external result", "waiting")))
        runner, agent = background_runner(model, backend, barriers)
        database = tmp_path / "large-operation.sqlite3"
        store = SqliteSessionStore(database) if persisted else InMemorySessionStore()
        session = Session("large-operation", store)
        handle = await start_agent(
            runner,
            agent,
            "Collect the original evidence",
            session=session,
            limits=RunLimits(
                context_budget=ContextBudget(
                    window_tokens=8_192, output_reserve=1_024, compact_at=6_000, recent_tokens=4_000
                )
            ),
        )
        await asyncio.wait_for(barriers.events[1].wait(), timeout=3)
        active = await store.snapshot(session.session_id)
        model.operation_id = active.operations[0].operation_id
        backend.release.set()
        outcome = await asyncio.wait_for(handle.result(), timeout=10)
        assert isinstance(outcome, RunResult) and outcome.state is RunStatus.SUCCEEDED
        assert outcome.final_output == "Original operation evidence was read"
        restored_store = SqliteSessionStore(database) if persisted else store
        restored = Session(session.session_id, restored_store)
        journal = await restored_store.snapshot(session.session_id)
        operation = journal.operations[0]
        assert operation.result is not None
        original = operation.result.model_dump_json()
        assert len(original.encode("utf-8")) > 1_000_000
        reference = OperationResultReference.from_result(operation.operation_id, operation.result)
        accepted = next(value for value in journal.inputs if value.envelope.kind == "operation_result")
        assert isinstance(accepted.envelope.content, ResultReference)
        assert accepted.envelope.content == reference.input_reference()
        assert len(accepted.envelope.message().model_dump_json()) < 2_048
        assert "原始证据🙂" * 100 not in model.requests[2].model_dump_json()
        read_message = next(
            value.message
            for value in journal.messages
            if isinstance(value.message, ToolMessage) and value.message.tool_call_id == "read-operation"
        )
        read_result = TypeAdapter[JsonObject](JsonObject).validate_json(read_message.content)
        assert read_result["ok"] is True
        read_output = read_result["output"]
        assert isinstance(read_output, dict) and read_output["content"] == original[:1_000]
        await restored.enqueue(
            InputEnvelope(input_id="follow-up", kind="user", source="host", content="Verify old evidence")
        )
        continuation = await restored_store.start(
            restored.session_id,
            invocation_id="follow-up",
            parameters_digest=hashlib.sha256(b"follow-up").hexdigest(),
            input_ids=("follow-up",),
            input_mode="batch",
            agent_id="reviewer",
        )
        assert continuation.snapshot.run_id != operation.run_id
        reader = ReadToolResultTool(SessionWriter(restored, continuation.snapshot))
        pages: list[str] = []
        start = 0
        while start < len(original):
            page = await reader.execute(
                ToolContext(call_id="read-page"),
                {"operation_id": operation.operation_id, "start_char": start, "max_chars": 8_000},
            )
            assert isinstance(page, dict)
            content = page["content"]
            next_start = page["next_start_char"]
            assert isinstance(content, str) and len(content) <= 8_000
            assert isinstance(next_start, int) and next_start > start
            assert page["reference"] == reference.model_dump(mode="json")
            pages.append(content)
            start = next_start
        assert "".join(pages) == original
        assert reference.sha256 == hashlib.sha256("".join(pages).encode("utf-8")).hexdigest()
        listing = await reader.execute(ToolContext(call_id="list"), {"kind": "operation"})
        assert isinstance(listing, dict) and listing["references"] == [reference.model_dump(mode="json")]
        exhausted = await reader.execute(
            ToolContext(call_id="next-page"), {"kind": "operation", "after_operation_id": operation.operation_id}
        )
        assert isinstance(exhausted, dict) and exhausted == {"references": [], "next_operation_id": None}
        invalid_cursor = await ToolRuntime().execute(
            reader,
            ExecutionToolCall(
                call_id="bad-cursor",
                tool_code="read_tool_result",
                arguments={"kind": "operation", "after_operation_id": "missing"},
            ),
            ToolContext(call_id="bad-cursor"),
        )
        assert isinstance(invalid_cursor, ToolResult) and invalid_cursor.error_code == "tool.arguments.invalid"
        other = Session("other-session", restored_store)
        await other.enqueue(InputEnvelope(input_id="other-task", kind="user", source="host", content="Other task"))
        accepted_other = await restored_store.start(
            other.session_id,
            invocation_id="other-run",
            parameters_digest=hashlib.sha256(b"other-run").hexdigest(),
            input_ids=("other-task",),
            input_mode="batch",
            agent_id="other",
        )
        denied = await ToolRuntime().execute(
            ReadToolResultTool(SessionWriter(other, accepted_other.snapshot)),
            ExecutionToolCall(
                call_id="foreign-result", tool_code="read_tool_result", arguments=reference.read_arguments()
            ),
            ToolContext(call_id="foreign-result"),
        )
        assert isinstance(denied, ToolResult) and not denied.ok and denied.error_code == "tool.result.not_found"

    asyncio.run(scenario())


class AdmissionBarrierModel(FakeOpenAIModel):
    """Capture actual transport arguments before an external response can be returned."""

    def __init__(self) -> None:
        super().__init__((text_batch("completed", "response"),))
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.received: OpenAIRequest | None = None

    async def stream(self, request: OpenAIRequest | Mapping[str, object]) -> AsyncIterator[OpenAIStreamChunk]:
        if not isinstance(request, OpenAIRequest):
            raise TypeError("The provider boundary requires its real validated request")
        self.received = request
        self.started.set()
        await self.release.wait()
        async for chunk in super().stream(request):
            yield chunk


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_model_admission_is_durable_before_transport_and_cancel_keeps_its_actual_output_cap(
    tmp_path: Path, backend: str
) -> None:
    """In-flight cancellation retains one admitted call with unknown measurements."""

    async def scenario() -> None:
        model = AdmissionBarrierModel()
        runner, agent = make_runner(model, tmp_path)
        store = InMemorySessionStore() if backend == "memory" else SqliteSessionStore(tmp_path / "admission.sqlite3")
        session = Session("actual-admission", store)
        handle = await start_agent(
            runner, agent, "Read evidence", session=session, limits=RunLimits(max_output_tokens=19)
        )
        async with handle:
            await asyncio.wait_for(model.started.wait(), timeout=3)
            snapshot = await store.run(session.session_id, handle.run_id)
            assert model.received is not None and model.received.max_completion_tokens == 19
            assert len(snapshot.model_attempts) == 1
            attempt = snapshot.model_attempts[0]
            assert attempt.max_output_tokens == model.received.max_completion_tokens
            assert attempt.completed_at is None and snapshot.usage.model_calls == 1
            assert snapshot.usage.output_tokens is None and snapshot.usage.budget_output_tokens == 19
            cancelled = await handle.cancel(reason="stop_in_flight")
        assert cancelled.usage == snapshot.usage
        assert cancelled.state is RunStatus.CANCELLED
        saved = await store.run(session.session_id, handle.run_id)
        assert saved.receipt is not None and saved.receipt.usage == saved.usage == cancelled.usage

    asyncio.run(scenario())


def test_cancellation_at_model_completed_notification_keeps_the_already_recorded_provider_usage(tmp_path: Path) -> None:
    """Completed provider measurements replace the allowance before observers can cancel."""

    async def scenario() -> None:
        model = FakeOpenAIModel((text_batch("Actual response", "response"),))
        runner, agent = make_runner(model, tmp_path)
        completed = asyncio.Event()
        release = asyncio.Event()

        async def observe(event: Event) -> None:
            if isinstance(event, ModelCompletedEvent):
                completed.set()
                await release.wait()

        runner.event_sink = observe
        session = Session("known-model-cost", SqliteSessionStore(tmp_path / "known.sqlite3"))
        handle = await start_agent(runner, agent, "Answer", session=session, limits=RunLimits(max_output_tokens=19))
        async with handle:
            await asyncio.wait_for(completed.wait(), timeout=3)
            saved = await session.store.run(session.session_id, handle.run_id)
            assert saved.model_attempts[0].completed_at is not None
            assert saved.usage.model_calls == 1 and saved.usage.output_tokens == 1
            assert saved.usage.budget_output_tokens == 1
            cancelled = await handle.cancel(reason="stop_after_response")
        assert cancelled.usage == saved.usage
        assert cancelled.usage.budget_output_tokens == 1

    asyncio.run(scenario())
