import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from pathlib import Path

from pleiad.agent.agent import Agent
from pleiad.contracts import ContractModel, JsonObject, ToolRef
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
from pleiad.runtime.runner import Runner
from pleiad.sandbox.backends.oci import OciSandbox
from pleiad.sandbox.backends.process import ProcessResult
from pleiad.sandbox.policy import SandboxPolicy
from pleiad.sandbox.sandbox import Sandbox
from pleiad.skills.loader import SkillLoader
from pleiad.tools.base import FunctionTool, ToolOutput
from pleiad.tools.builtins.defaults import install_default_tools
from pleiad.tools.contracts import ToolContext, ToolDescriptor, ToolPolicy, default_tool_refs
from pleiad.tools.interruption import ToolInterruption
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
        if command[1] == "info":
            return ProcessResult(0, b"", b"", False, 0.01, False)
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


def tool_batch(call_id: str, *, name: str = "echo", arguments: str = '{"value":"x"}') -> tuple[OpenAIStreamChunk, ...]:
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
    tools = default_tool_refs()
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
        tools = (*tools, ToolRef("echo"))
    agent = Agent(name="assistant", model=model, prompt="Answer the user.", tools=tools)
    return Runner(tools=registry, skills=SkillLoader(), tool_runtime=ToolRuntime()), agent


async def successful_tool(context: ToolContext, arguments: JsonObject) -> JsonObject:
    """Return a model-visible object from a successful Tool invocation."""
    return {"echoed": arguments["value"]}


async def interrupting_tool(context: ToolContext, arguments: JsonObject) -> ToolInterruption:
    """Pause the Run before the external action is completed."""
    return ToolInterruption(
        interruption_id="interrupt-1",
        call_id=context.call_id,
        tool_code="echo",
        tool_name="echo",
        arguments=arguments,
        reason="host input required",
    )
