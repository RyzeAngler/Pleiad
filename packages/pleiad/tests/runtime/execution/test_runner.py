import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

import pytest
from pydantic import ValidationError

from pleiad.agent.agent import Agent
from pleiad.agent.errors import AgentConfigurationError
from pleiad.context.budget import ContextBudget
from pleiad.context.selection.models import ContextBlock
from pleiad.contracts import CapabilityGrants, ContractModel, JsonObject, SkillRef, ToolRef
from pleiad.events.contracts import Event
from pleiad.events.message import MessageAbortedEvent, MessageCompletedEvent, MessageStartedEvent, MessageTextDeltaEvent
from pleiad.events.runtime import ModelCompletedEvent, RunStartedEvent
from pleiad.memory.short_memory import ShortMemory
from pleiad.memory.turn import Turn
from pleiad.messages.message import AssistantMessage
from pleiad.models.anthropic.client import AnthropicModelClient
from pleiad.models.anthropic.request import AnthropicRequest
from pleiad.models.anthropic.response import (
    AnthropicContentBlock,
    AnthropicResponse,
    AnthropicStreamEvent,
    AnthropicStreamUsage,
    AnthropicUsage,
)
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
from pleiad.runtime.config import RunConfig
from pleiad.runtime.errors import RuntimeStateError
from pleiad.runtime.execution.runner import Runner, combined_event_sink
from pleiad.runtime.lifecycle.result import RunResult
from pleiad.runtime.lifecycle.state import RunState, RunStatus
from pleiad.runtime.policy import HostPolicy, effective_policy
from pleiad.sandbox.backends.oci import OciSandbox
from pleiad.sandbox.backends.process import ProcessResult
from pleiad.sandbox.policy import SandboxPolicy
from pleiad.sandbox.sandbox import Sandbox
from pleiad.skills.contracts import SkillDescriptor
from pleiad.skills.loader import SkillLoader
from pleiad.skills.package import SkillPackage
from pleiad.tools.base import FunctionTool, ToolOutput
from pleiad.tools.builtins.defaults import install_default_tools
from pleiad.tools.contracts import ToolContext, ToolDescriptor, ToolPolicy, ToolResult
from pleiad.tools.interruption import InterruptionResponse, ToolInterruption
from pleiad.tools.registry import ToolRegistry
from pleiad.tools.runtime import ToolRuntime


class EchoArguments(ContractModel):
    """Arguments accepted by the test Tool."""

    value: str


class FakeOpenAIModel(OpenAIModelClient):
    """Concrete protocol object with a deterministic provider-boundary stream."""

    def __init__(self, batches: tuple[tuple[OpenAIStreamChunk, ...], ...], *, report_usage: bool = True) -> None:
        self.provider = "fake"
        self.model = "fake-model"
        self.batches = iter(batches)
        self.report_usage = report_usage
        self.requests: list[OpenAIRequest] = []

    async def stream(self, request: OpenAIRequest | Mapping[str, object]) -> AsyncIterator[OpenAIStreamChunk]:
        """Record one projected request and yield its configured chunks."""
        if not isinstance(request, OpenAIRequest):
            raise TypeError("fake model requires a parsed OpenAI request")
        self.requests.append(request)
        try:
            batch = next(self.batches)
        except StopIteration as error:
            raise AssertionError("the Runtime requested more model responses than configured") from error
        for chunk in batch:
            yield chunk
        if self.report_usage and not any(chunk.usage is not None for chunk in batch):
            yield OpenAIStreamChunk(
                model=self.model, usage=OpenAIUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2)
            )


class FakeAnthropicModel(AnthropicModelClient):
    """Concrete Anthropic protocol object with deterministic stream batches."""

    def __init__(self, batches: tuple[tuple[AnthropicStreamEvent, ...], ...]) -> None:
        self.provider = "fake"
        self.model = "fake-model"
        self.batches = iter(batches)
        self.requests: list[AnthropicRequest] = []

    async def stream(self, request: AnthropicRequest | Mapping[str, object]) -> AsyncIterator[AnthropicStreamEvent]:
        """Record one projected request and yield its configured events."""
        if not isinstance(request, AnthropicRequest):
            raise TypeError("fake model requires a parsed Anthropic request")
        self.requests.append(request)
        try:
            batch = next(self.batches)
        except StopIteration as error:
            raise AssertionError("the Runtime requested more model responses than configured") from error
        for event in batch:
            if event.type == "message_stop" and not any(
                item.type == "message_delta" and item.usage is not None for item in batch
            ):
                yield AnthropicStreamEvent(type="message_delta", usage=AnthropicStreamUsage(output_tokens=1))
            yield event


class BlockingOpenAIModel(FakeOpenAIModel):
    """Concrete protocol object that waits until the Runtime is cancelled or times out."""

    def __init__(self) -> None:
        super().__init__(())
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def stream(self, request: OpenAIRequest | Mapping[str, object]) -> AsyncIterator[OpenAIStreamChunk]:
        """Block inside the provider stream after recording the request."""
        self.started.set()
        await self.release.wait()
        async for chunk in super().stream(request):
            yield chunk


class FailingOpenAIModel(FakeOpenAIModel):
    """Concrete protocol object that fails after the stream starts."""

    async def stream(self, request: OpenAIRequest | Mapping[str, object]) -> AsyncIterator[OpenAIStreamChunk]:
        """Raise one deterministic Provider-boundary failure."""
        if not isinstance(request, OpenAIRequest):
            raise TypeError("fake model requires a parsed OpenAI request")
        self.requests.append(request)
        yield OpenAIStreamChunk(id="failed-response", model="fake-model", choices=())
        raise RuntimeError("provider unavailable")


class SuccessfulExecutor:
    """Return deterministic OCI daemon facts without starting a process."""

    def __init__(self) -> None:
        self.removed = False

    async def __call__(
        self,
        command: tuple[str, ...],
        *,
        stdin: bytes | None,
        output_limit: int,
        timeout_seconds: float | None,
    ) -> ProcessResult:
        """Return successful create, start, inspect, and remove facts."""
        del stdin, output_limit, timeout_seconds
        if command[1] == "create":
            return ProcessResult(0, b"container-id\n", b"", False, 0.01, False)
        if command[1] == "start":
            return ProcessResult(0, b"", b"", False, 0.01, False)
        if command[1] == "rm":
            self.removed = True
            return ProcessResult(0, b"container-id\n", b"", False, 0.01, False)
        if command[1] == "inspect" and self.removed:
            return ProcessResult(1, b"", b"Error: No such object\n", False, 0.01, False)
        if command[1] == "inspect":
            return ProcessResult(0, b"0\n", b"", False, 0.01, False)
        raise AssertionError(f"unexpected OCI command: {command}")


def create_sandbox() -> Sandbox:
    """Create a real SDK Sandbox with only its external CLI boundary faked."""
    return Sandbox(OciSandbox(process_executor=SuccessfulExecutor()))


def text_batch(text: str, response_id: str) -> tuple[OpenAIStreamChunk, ...]:
    """Create a complete text response batch."""
    return (
        OpenAIStreamChunk(
            id=response_id,
            model="fake-model",
            choices=(OpenAIStreamChoice(delta=OpenAIStreamDelta(content=text)),),
        ),
        OpenAIStreamChunk(
            id=response_id,
            model="fake-model",
            choices=(OpenAIStreamChoice(delta=OpenAIStreamDelta(), finish_reason="stop"),),
        ),
    )


def anthropic_text_batch(text: str, response_id: str) -> tuple[AnthropicStreamEvent, ...]:
    """Create a complete Anthropic text response batch."""
    return (
        AnthropicStreamEvent(
            type="message_start",
            message=AnthropicResponse(
                id=response_id,
                content=(),
                model="fake-model",
                usage=AnthropicUsage(input_tokens=1, output_tokens=1),
            ),
        ),
        AnthropicStreamEvent(type="content_block_delta", index=0, delta={"type": "text_delta", "text": text}),
        AnthropicStreamEvent(type="message_delta", usage=AnthropicStreamUsage(output_tokens=1)),
        AnthropicStreamEvent(type="message_stop"),
    )


def tool_batch(call_id: str) -> tuple[OpenAIStreamChunk, ...]:
    """Create a complete single-Tool-Call response batch."""
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
                                function=OpenAIStreamFunction(name="echo", arguments='{"value":"x"}'),
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


def anthropic_tool_batch(call_id: str) -> tuple[AnthropicStreamEvent, ...]:
    """Create a complete Anthropic Tool Call response batch."""
    return (
        AnthropicStreamEvent(
            type="message_start",
            message=AnthropicResponse(
                id="response-tool",
                content=(),
                model="fake-model",
                usage=AnthropicUsage(input_tokens=1, output_tokens=1),
            ),
        ),
        AnthropicStreamEvent(
            type="content_block_start",
            index=0,
            content_block=AnthropicContentBlock(type="tool_use", id=call_id, name="echo", input={}),
        ),
        AnthropicStreamEvent(
            type="content_block_delta",
            index=0,
            delta={"type": "input_json_delta", "partial_json": '{"value":"x"}'},
        ),
        AnthropicStreamEvent(type="message_delta", usage=AnthropicStreamUsage(output_tokens=1)),
        AnthropicStreamEvent(type="message_stop"),
    )


def make_runner(
    model: FakeOpenAIModel | FakeAnthropicModel,
    tmp_path: Path,
    function: Callable[[ToolContext, JsonObject], Awaitable[ToolOutput]] | None = None,
) -> tuple[Runner, Agent]:
    """Bind a fake model, real default Tools, and an optional FunctionTool."""
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
    tools: tuple[ToolRef, ...] = ()
    if function is not None:
        descriptor = ToolDescriptor(name="echo", code="echo", description="Echo a value")
        registry.register(
            descriptor,
            lambda selected: FunctionTool(
                selected,
                function,
                EchoArguments,
                policy=ToolPolicy(parallel_safe=True),
            ),
        )
        tools = (ToolRef("echo"),)
    agent = Agent(name="assistant", model=model, prompt="Answer the user.", tools=tools)
    return Runner(tools=registry, skills=SkillLoader(), tool_runtime=ToolRuntime()), agent


async def successful_tool(context: ToolContext, arguments: JsonObject) -> JsonObject:
    """Return a model-visible object from a successful Tool invocation."""
    return {"echoed": arguments["value"]}


async def interrupting_tool(context: ToolContext, arguments: JsonObject) -> ToolInterruption:
    """Pause the Run before the external action is completed."""
    return ToolInterruption(
        interruption_id="interrupt-1",
        call_id="call-1",
        tool_code="echo",
        tool_name="echo",
        arguments=arguments,
        reason="host input required",
    )


def test_runner_persists_a_direct_model_answer_as_one_completed_turn(tmp_path: Path) -> None:
    """A direct response creates one ShortMemory Turn and returns its visible text."""
    model = FakeOpenAIModel((text_batch("done", "response-1"),))
    runner, agent = make_runner(model, tmp_path)
    memory = ShortMemory.in_memory("memory-direct")

    result = asyncio.run(runner.run(agent, "hello", memory=memory, config=RunConfig()))

    assert result.status is RunStatus.SUCCEEDED
    assert result.final_output == "done"
    turns = asyncio.run(memory.read())
    assert len(turns) == 1
    assert [message.role for message in turns[0].messages] == ["user", "assistant"]
    assert model.requests[0].messages[-1].content == "hello"


def test_runner_completes_the_model_tool_model_loop_at_turn_boundaries(tmp_path: Path) -> None:
    """A Tool response closes one Turn before the next model call starts."""
    model = FakeOpenAIModel((tool_batch("call-1"), text_batch("finished", "response-2")))
    runner, agent = make_runner(model, tmp_path, successful_tool)
    memory = ShortMemory.in_memory("memory-tool")

    result = asyncio.run(runner.run(agent, "use the Tool", memory=memory, config=RunConfig()))

    assert result.status is RunStatus.SUCCEEDED
    turns = asyncio.run(memory.read())
    assert len(turns) == 2
    assert [message.role for message in turns[0].messages] == [
        "user",
        "assistant",
        "tool",
    ]
    assert turns[0].completed is True
    assert turns[1].messages[-1].content == "finished"
    assert len(model.requests) == 2
    assert model.requests[1].messages[-1].role == "tool"


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
    memory = ShortMemory.in_memory("memory-anthropic-tool")

    result = asyncio.run(runner.run(agent, "use the Tool", memory=memory, config=RunConfig()))

    assert result.status is RunStatus.SUCCEEDED
    assert result.final_output == "finished"
    assert len(model.requests) == 2
    assert model.requests[1].messages[-1].role == "user"


def test_runner_resume_submits_a_resolved_interruption_once(tmp_path: Path) -> None:
    """Resume completes the paused Tool result and continues without repeating the Tool."""
    model = FakeOpenAIModel((tool_batch("call-1"), text_batch("resumed", "response-2")))
    runner, agent = make_runner(model, tmp_path, interrupting_tool)
    memory = ShortMemory.in_memory("memory-resume")

    interrupted = asyncio.run(runner.run(agent, "wait", memory=memory, config=RunConfig()))

    assert interrupted.status is RunStatus.INTERRUPTED
    assert interrupted.checkpoint is not None
    now = datetime.now(UTC)
    resolution = InterruptionResponse(
        interruption_id="interrupt-1",
        result=ToolResult(call_id="call-1", ok=True, output={"approved": True}, started_at=now, finished_at=now),
    )
    resumed = asyncio.run(
        runner.resume(
            agent,
            interrupted.checkpoint,
            memory=memory,
            resolutions=(resolution,),
            config=RunConfig(),
        ),
    )

    assert resumed.status is RunStatus.SUCCEEDED
    turns = asyncio.run(memory.read())
    assert len(turns) == 2
    assert (
        sum(
            len(message.tool_calls)
            for turn in turns
            for message in turn.messages
            if isinstance(message, AssistantMessage)
        )
        == 1
    )
    assert model.requests[1].messages[-1].role == "tool"


def test_streamed_resume_publishes_recovered_tool_messages_after_memory_accepts_them(tmp_path: Path) -> None:
    """Resume correlates the recovered Tool result and the next Turn with the original Run."""
    model = FakeOpenAIModel((tool_batch("call-1"), text_batch("resumed", "response-2")))
    runner, agent = make_runner(model, tmp_path, interrupting_tool)
    memory = ShortMemory.in_memory("memory-streamed-resume")
    interrupted = asyncio.run(runner.run(agent, "wait", memory=memory, config=RunConfig()))
    checkpoint = interrupted.checkpoint
    assert checkpoint is not None
    now = datetime.now(UTC)
    resolution = InterruptionResponse(
        interruption_id="interrupt-1",
        result=ToolResult(call_id="call-1", ok=True, output={"approved": True}, started_at=now, finished_at=now),
    )

    async def collect() -> tuple[list[Event], RunResult]:
        stream = runner.resume_streamed(
            agent,
            checkpoint,
            memory=memory,
            resolutions=(resolution,),
            config=RunConfig(),
        )
        events = [event async for event in stream.events()]
        return events, await stream.result()

    events, result = asyncio.run(collect())

    kinds = [event.kind for event in events]
    assert kinds[:5] == [
        "run.resumed",
        "tool.completed",
        "message.completed",
        "turn.completed",
        "turn.started",
    ]
    assert result.run_id == interrupted.run_id
    assert all(event.run_id == interrupted.run_id for event in events)


def test_model_stream_failure_aborts_the_message_and_reports_the_attempt(tmp_path: Path) -> None:
    """A Provider failure closes the streamed message and reports the failed model attempt."""
    model = FailingOpenAIModel(())
    runner, agent = make_runner(model, tmp_path)
    memory = ShortMemory.in_memory("memory-model-failure")

    async def collect() -> list[Event]:
        stream = runner.run_streamed(agent, "hello", memory=memory, config=RunConfig())
        events: list[Event] = []
        with pytest.raises(RuntimeError, match="provider unavailable"):
            async for event in stream.events():
                events.append(event)
        with pytest.raises(RuntimeError, match="provider unavailable"):
            await stream.result()
        return events

    events = asyncio.run(collect())

    assert [event.kind for event in events][-3:] == ["model.failed", "message.aborted", "run.failed"]


def test_runner_streams_safe_lifecycle_events_through_the_shared_execution_path(tmp_path: Path) -> None:
    """The streamed API exposes model deltas and its terminal event from one Run."""
    model = FakeOpenAIModel((text_batch("streamed", "response-1"),))
    runner, agent = make_runner(model, tmp_path)
    memory = ShortMemory.in_memory("memory-stream")

    async def collect() -> tuple[list[Event], RunResult]:
        stream = runner.run_streamed(agent, "hello", memory=memory, config=RunConfig())
        events = [event async for event in stream.events()]
        return events, await stream.result()

    events, result = asyncio.run(collect())

    assert result.status is RunStatus.SUCCEEDED
    assert [event.kind for event in events] == [
        "run.started",
        "turn.started",
        "message.completed",
        "model.started",
        "message.started",
        "message.text.delta",
        "model.completed",
        "message.completed",
        "turn.completed",
        "run.completed",
    ]
    assert isinstance(events[5], MessageTextDeltaEvent)
    assert events[5].delta == "streamed"
    assert isinstance(events[4], MessageStartedEvent)
    assert isinstance(events[7], MessageCompletedEvent)
    assert events[4].message_id == events[5].message_id == events[7].message.message_id
    assert all(event.event_id and event.schema_version == 1 for event in events)
    assert all(event.run_id == result.run_id for event in events)


def test_event_sink_accepts_application_event_subclasses() -> None:
    """Applications extend the stable Event envelope without SDK type registration."""

    class ProductEvent(Event):
        kind: Literal["product.notice"] = "product.notice"
        execution_id: str

    received: list[Event] = []

    async def sink(event: Event) -> None:
        received.append(event)

    combined = combined_event_sink(sink, None)
    assert combined is not None
    event = ProductEvent(execution_id="execution-1")

    async def publish() -> None:
        await combined(event)

    asyncio.run(publish())

    assert received == [event]


def test_runner_timeout_aborts_the_active_turn_and_returns_timed_out(tmp_path: Path) -> None:
    """The Run timeout cleans up an active ShortMemory Turn."""
    model = BlockingOpenAIModel()
    runner, agent = make_runner(model, tmp_path)
    memory = ShortMemory.in_memory("memory-timeout")
    events: list[Event] = []

    async def sink(event: Event) -> None:
        events.append(event)

    runner.event_sink = sink

    async def run() -> RunResult:
        return await runner.run(
            agent,
            "wait",
            memory=memory,
            config=RunConfig(timeout_seconds=0.05),
        )

    async def exercise() -> tuple[RunResult, Turn | None]:
        task = asyncio.create_task(run())
        await model.started.wait()
        result = await task
        return result, await memory.active()

    result, active = asyncio.run(exercise())

    assert result.status is RunStatus.TIMED_OUT
    assert active is None
    aborted = next(event for event in events if isinstance(event, MessageAbortedEvent))
    assert aborted.reason == "timed_out"


def test_runner_propagates_cancellation_after_aborting_the_active_turn(tmp_path: Path) -> None:
    """Caller cancellation remains CancelledError after Runtime cleanup."""
    model = BlockingOpenAIModel()
    runner, agent = make_runner(model, tmp_path)
    memory = ShortMemory.in_memory("memory-cancel")
    events: list[Event] = []

    async def sink(event: Event) -> None:
        events.append(event)

    runner.event_sink = sink

    async def exercise() -> Turn | None:
        task = asyncio.create_task(runner.run(agent, "cancel", memory=memory, config=RunConfig()))
        await model.started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return await memory.active()

    active = asyncio.run(exercise())

    assert active is None
    assert [event.kind for event in events][-2:] == ["message.aborted", "run.cancelled"]


def test_run_stream_context_closes_an_unconsumed_run(tmp_path: Path) -> None:
    """Leaving the explicit stream scope cancels and cleans its owned Run."""
    model = BlockingOpenAIModel()
    runner, agent = make_runner(model, tmp_path)
    memory = ShortMemory.in_memory("memory-stream-close")

    async def exercise() -> tuple[bool, Turn | None]:
        stream = runner.run_streamed(agent, "close", memory=memory, config=RunConfig())
        async with stream:
            await model.started.wait()
        return stream.task.done(), await memory.active()

    done, active = asyncio.run(exercise())

    assert done is True
    assert active is None


def test_missing_tool_binding_fails_before_turn_starts(tmp_path: Path) -> None:
    """An unbound default Tool fails before ShortMemory or the model is touched."""
    model = FakeOpenAIModel((text_batch("unexpected", "response-1"),))
    registry = ToolRegistry()
    runner = Runner(tools=registry, skills=SkillLoader(), tool_runtime=ToolRuntime())
    agent = Agent(name="assistant", model=model, prompt="Answer the user.")
    memory = ShortMemory.in_memory("memory-missing-tool")

    with pytest.raises(AgentConfigurationError, match="capability binding"):
        asyncio.run(runner.run(agent, "hello", memory=memory, config=RunConfig()))

    assert asyncio.run(memory.read()) == ()
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
    """A selected Skill without a host-bound read root fails before a Turn starts."""
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
    memory = ShortMemory.in_memory("memory-missing-skill-read")

    with pytest.raises(AgentConfigurationError, match="not readable"):
        asyncio.run(runner.run(agent, "hello", memory=memory, config=RunConfig()))

    assert asyncio.run(memory.read()) == ()
    assert model.requests == []


def test_invalid_final_output_is_not_success(tmp_path: Path) -> None:
    """A final response that violates the Agent schema returns a failed Run."""
    model = FakeOpenAIModel((text_batch("not json", "response-1"),))
    runner, _ = make_runner(model, tmp_path)
    agent = Agent(
        name="structured",
        model=model,
        prompt="Return JSON.",
        output_schema={"type": "object", "required": ["answer"], "properties": {"answer": {"type": "string"}}},
    )
    memory = ShortMemory.in_memory("memory-invalid-output")

    result = asyncio.run(runner.run(agent, "hello", memory=memory, config=RunConfig()))

    assert result.status is RunStatus.FAILED
    assert result.error is not None
    assert result.error.code == "agent.output.invalid"


def test_reused_agent_keeps_runs_isolated(tmp_path: Path) -> None:
    """Reusing one immutable Agent does not share messages between Runs."""
    model = FakeOpenAIModel((text_batch("first", "response-1"), text_batch("second", "response-2")))
    runner, agent = make_runner(model, tmp_path)
    first_memory = ShortMemory.in_memory("memory-first")
    second_memory = ShortMemory.in_memory("memory-second")

    first = asyncio.run(runner.run(agent, "one", memory=first_memory, config=RunConfig()))
    second = asyncio.run(runner.run(agent, "two", memory=second_memory, config=RunConfig()))

    assert first.status is RunStatus.SUCCEEDED
    assert second.status is RunStatus.SUCCEEDED
    assert first.final_output == "first"
    assert second.final_output == "second"
    first_turns = asyncio.run(first_memory.read())
    second_turns = asyncio.run(second_memory.read())
    assert first_turns[0].messages[0].content == "one"
    assert second_turns[0].messages[0].content == "two"


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


def approved_resolution() -> InterruptionResponse:
    now = datetime.now(UTC)
    return InterruptionResponse(
        interruption_id="interrupt-1",
        result=ToolResult(call_id="call-1", ok=True, output={"approved": True}, started_at=now, finished_at=now),
    )


@pytest.mark.parametrize("restriction", ["request", "parent"])
def test_resume_rejects_revoked_permissions_before_external_action(tmp_path: Path, restriction: str) -> None:
    runner, agent, tool = make_resume_runner(tmp_path)
    memory = ShortMemory.in_memory("revoked")

    async def exercise() -> None:
        parent_policy = effective_policy(RunConfig(), runner.host_policy) if restriction == "parent" else None
        interrupted = await runner.run(
            agent,
            "approve",
            memory=memory,
            config=RunConfig(),
            parent_run_id="parent-run" if parent_policy is not None else None,
            parent_policy=parent_policy,
        )
        assert interrupted.checkpoint is not None
        restricted = RunConfig(grants=CapabilityGrants())
        with pytest.raises(RuntimeStateError, match="no longer permits"):
            await runner.resume(
                agent,
                interrupted.checkpoint,
                memory=memory,
                resolutions=(approved_resolution(),),
                config=restricted if restriction == "request" else RunConfig(),
                parent_run_id="parent-run" if restriction == "parent" else None,
                parent_policy=effective_policy(restricted, runner.host_policy) if restriction == "parent" else None,
            )
        assert tool.contexts == []
        assert await memory.active() is not None

    asyncio.run(exercise())


def test_resume_expired_checkpoint_never_reenters_tool(tmp_path: Path) -> None:
    runner, agent, tool = make_resume_runner(tmp_path)
    memory = ShortMemory.in_memory("expired")

    async def exercise() -> None:
        interrupted = await runner.run(agent, "approve", memory=memory, config=RunConfig())
        assert interrupted.checkpoint is not None
        checkpoint = interrupted.checkpoint.model_copy(update={"deadline": datetime.now(UTC) - timedelta(seconds=1)})
        result = await runner.resume(
            agent, checkpoint, memory=memory, resolutions=(approved_resolution(),), config=RunConfig()
        )
        assert result.status is RunStatus.TIMED_OUT
        assert tool.contexts == []
        assert result.checkpoint is not None
        assert result.checkpoint.deadline == checkpoint.deadline
        assert result.checkpoint.interruptions == checkpoint.interruptions
        assert await memory.active() is not None

    asyncio.run(exercise())


def test_child_run_resume_requires_current_parent_policy(tmp_path: Path) -> None:
    runner, agent, tool = make_resume_runner(tmp_path)
    memory = ShortMemory.in_memory("child-resume")

    async def exercise() -> None:
        interrupted = await runner.run(
            agent,
            "approve",
            memory=memory,
            config=RunConfig(),
            parent_run_id="parent-run",
            parent_policy=effective_policy(RunConfig(), runner.host_policy),
        )
        assert interrupted.checkpoint is not None
        with pytest.raises(RuntimeStateError, match="current parent policy"):
            await runner.resume(
                agent,
                interrupted.checkpoint,
                memory=memory,
                resolutions=(approved_resolution(),),
                config=RunConfig(),
            )
        assert tool.contexts == []
        assert await memory.active() is not None

    asyncio.run(exercise())


def test_resume_deadline_cancels_waiting_tool_and_preserves_pending_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, agent, tool = make_resume_runner(tmp_path)
    memory = ShortMemory.in_memory("resume-deadline")

    async def exercise() -> None:
        interrupted = await runner.run(agent, "approve", memory=memory, config=RunConfig())
        assert interrupted.checkpoint is not None
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
        result = await runner.resume(
            agent, interrupted.checkpoint, memory=memory, resolutions=(approved_resolution(),), config=RunConfig()
        )
        assert result.status is RunStatus.TIMED_OUT
        assert cleaned.is_set()
        assert result.checkpoint is not None
        assert result.checkpoint.interruptions == interrupted.interruptions
        assert result.checkpoint.tool_results == ()

    asyncio.run(exercise())


def test_resume_at_turn_limit_leaves_memory_available_for_next_run(tmp_path: Path) -> None:
    runner, agent, tool = make_resume_runner(tmp_path)
    memory = ShortMemory.in_memory("resume-limit")

    async def exercise() -> None:
        interrupted = await runner.run(agent, "approve", memory=memory, config=RunConfig(max_turns=1))
        assert interrupted.checkpoint is not None
        result = await runner.resume(
            agent, interrupted.checkpoint, memory=memory, resolutions=(approved_resolution(),), config=RunConfig()
        )
        assert result.status is RunStatus.LIMIT_REACHED
        assert await memory.active() is None
        assert len(await memory.read()) == 1
        assert tool.contexts[0].parent_limits is not None
        assert tool.contexts[0].parent_limits.remaining_turns == 0
        next_result = await runner.run(agent, "next", memory=memory, config=RunConfig())
        assert next_result.status is RunStatus.SUCCEEDED
        assert next_result.final_output == "resumed"
        assert len(tool.contexts) == 1

    asyncio.run(exercise())


def test_stream_close_with_full_buffer_stops_producer_and_drain_terminates(tmp_path: Path) -> None:
    runner, agent = make_runner(FakeOpenAIModel(()), tmp_path)

    async def exercise() -> None:
        full = asyncio.Event()
        cleaned = asyncio.Event()

        async def produce(sink: Callable[[Event], Awaitable[None]]) -> RunResult:
            try:
                for index in range(257):
                    if index == 256:
                        full.set()
                    await sink(RunStartedEvent(run_id="buffered", agent_name=agent.name))
                return RunResult(run_id="buffered", status=RunStatus.SUCCEEDED)
            finally:
                cleaned.set()

        stream = runner.create_stream(produce)
        await asyncio.wait_for(full.wait(), timeout=1)
        await asyncio.wait_for(stream.aclose(), timeout=1)
        assert cleaned.is_set()
        observed: list[Event] = []
        with pytest.raises(asyncio.CancelledError):
            async with asyncio.timeout(1):
                async for event in stream.events():
                    observed.append(event)
        assert len(observed) == 256

    asyncio.run(exercise())


def test_runner_omits_budget_pruned_references_from_wire_request(tmp_path: Path) -> None:
    model = FakeOpenAIModel((text_batch("answer", "response-1"),))
    runner, agent = make_runner(model, tmp_path)
    memory = ShortMemory.in_memory("context-pruning")
    config = RunConfig(
        context_budget=ContextBudget(window_tokens=3_000, output_reserve=100, compact_at=2_800, recent_tokens=100)
    )
    result = asyncio.run(
        runner.run(
            agent,
            "question",
            memory=memory,
            config=config,
            context_blocks=(
                ContextBlock(kind="document", origin="review", content="PRUNED_REFERENCE " + "x" * 20_000),
            ),
        )
    )
    assert result.status is RunStatus.SUCCEEDED
    assert "PRUNED_REFERENCE" not in model.requests[0].model_dump_json()
    assert asyncio.run(memory.read())[0].messages[0].content == "question"


def test_run_missing_usage_remains_unknown_and_charges_request_budget(tmp_path: Path) -> None:
    model = FakeOpenAIModel((text_batch("answer", "unknown"),), report_usage=False)
    runner, agent = make_runner(model, tmp_path)
    events: list[Event] = []

    async def collect(event: Event) -> None:
        events.append(event)

    runner.event_sink = collect
    result = asyncio.run(
        runner.run(
            agent,
            "answer",
            memory=ShortMemory.in_memory("unknown-usage"),
            config=RunConfig(max_output_tokens=5),
        )
    )
    assert result.status is RunStatus.SUCCEEDED
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
        runner.run(
            agent,
            "use a tool",
            memory=ShortMemory.in_memory("unknown-budget"),
            config=RunConfig(max_output_tokens=5),
        )
    )
    assert result.status is RunStatus.LIMIT_REACHED
    assert result.usage.output_tokens is None
    assert result.usage.budget_output_tokens == 5
    assert len(model.requests) == 1


def test_model_requests_use_only_remaining_run_output_budget(tmp_path: Path) -> None:
    model = FakeOpenAIModel((tool_batch("call-1"), text_batch("done", "last")))
    runner, agent = make_runner(model, tmp_path)
    result = asyncio.run(
        runner.run(
            agent,
            "use a tool",
            memory=ShortMemory.in_memory("remaining-budget"),
            config=RunConfig(max_output_tokens=3),
        )
    )
    assert result.status is RunStatus.SUCCEEDED
    assert result.usage.output_tokens == 2
    assert result.usage.budget_output_tokens == 2
    assert [request.max_completion_tokens for request in model.requests] == [3, 2]


def test_resume_retains_unknown_usage_budget_and_closes_turn_at_output_limit(tmp_path: Path) -> None:
    model = FakeOpenAIModel((tool_batch("call-1"), text_batch("must not run", "unexpected")), report_usage=False)
    runner, agent = make_runner(model, tmp_path, interrupting_tool)
    memory = ShortMemory.in_memory("unknown-resume")

    async def scenario() -> None:
        paused = await runner.run(agent, "work", memory=memory, config=RunConfig(max_output_tokens=5))
        assert paused.status is RunStatus.INTERRUPTED
        assert paused.checkpoint is not None
        assert paused.checkpoint.version == 2
        assert paused.checkpoint.usage.budget_output_tokens == 5
        now = datetime.now(UTC)
        response = InterruptionResponse(
            interruption_id=paused.interruptions[0].interruption_id,
            result=ToolResult(call_id="call-1", ok=True, output={"approved": True}, started_at=now, finished_at=now),
        )
        resumed = await runner.resume(
            agent,
            paused.checkpoint,
            memory=memory,
            resolutions=(response,),
            config=RunConfig(max_output_tokens=5),
        )
        assert resumed.status is RunStatus.LIMIT_REACHED
        assert resumed.usage.output_tokens is None
        assert resumed.usage.budget_output_tokens == 5
        assert len(model.requests) == 1
        assert await memory.active() is None

    asyncio.run(scenario())


def test_run_checkpoint_rejects_old_usage_accounting_version(tmp_path: Path) -> None:
    model = FakeOpenAIModel((tool_batch("call-1"),))
    runner, agent = make_runner(model, tmp_path, interrupting_tool)
    paused = asyncio.run(
        runner.run(
            agent,
            "work",
            memory=ShortMemory.in_memory("old-checkpoint"),
            config=RunConfig(),
        )
    )
    assert paused.checkpoint is not None
    old = paused.checkpoint.model_dump(mode="json")
    old["version"] = 1
    with pytest.raises(ValidationError, match="version"):
        RunState.model_validate(old)
