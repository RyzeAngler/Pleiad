import asyncio
import json
from pathlib import Path

from pleiad.agent.agent import Agent
from pleiad.events.contracts import Event
from pleiad.memory.short_memory import ShortMemory
from pleiad.messages.message import AssistantMessage, ToolMessage
from pleiad.models.anthropic.response import (
    AnthropicContentBlock,
    AnthropicResponse,
    AnthropicStreamEvent,
    AnthropicUsage,
)
from pleiad.models.openai.response import (
    OpenAIStreamChoice,
    OpenAIStreamChunk,
    OpenAIStreamDelta,
    OpenAIStreamFunction,
    OpenAIStreamToolCall,
)
from pleiad.orchestration.handoff.handoff import Handoff
from pleiad.runtime.config import RunConfig
from pleiad.runtime.lifecycle.state import RunStatus
from tests.orchestration.supervisor.test_supervisor import tool_batch
from tests.runtime.execution.test_runner import (
    FakeAnthropicModel,
    FakeOpenAIModel,
    anthropic_text_batch,
    make_runner,
    text_batch,
)


def anthropic_handoff_batch(name: str, call_id: str) -> tuple[AnthropicStreamEvent, ...]:
    """Create a deterministic Anthropic Handoff tool-use stream."""
    return (
        AnthropicStreamEvent(
            type="message_start",
            message=AnthropicResponse(
                id="handoff-response",
                content=(),
                model="fake-model",
                usage=AnthropicUsage(input_tokens=1, output_tokens=1),
            ),
        ),
        AnthropicStreamEvent(
            type="content_block_start",
            index=0,
            content_block=AnthropicContentBlock(type="tool_use", id=call_id, name=name, input={}),
        ),
        AnthropicStreamEvent(
            type="content_block_delta",
            index=0,
            delta={"type": "input_json_delta", "partial_json": '{"task":"review"}'},
        ),
        AnthropicStreamEvent(type="message_stop"),
    )


def test_handoff_switches_the_next_model_with_one_run_and_one_memory(tmp_path: Path) -> None:
    """A valid Handoff is a Runtime control transition, not a child Run."""
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
    memory = ShortMemory.in_memory("handoff-memory")
    events: list[Event] = []

    async def event_sink(event: Event) -> None:
        events.append(event)

    runner.event_sink = event_sink

    result = asyncio.run(
        runner.run(
            source,
            "route this",
            memory=memory,
            config=RunConfig(),
            handoffs=(binding,),
        )
    )

    assert result.status is RunStatus.SUCCEEDED
    assert result.run_id
    assert result.final_output == "reviewed"
    assert len(source_model.requests) == 1
    assert len(target_model.requests) == 1
    first_turn = asyncio.run(memory.read())[0]
    handoff_message = first_turn.messages[-1]
    assert isinstance(handoff_message, ToolMessage)
    assert json.loads(handoff_message.content)["output"]["control"] == "handoff"
    handoff_events = [event.kind for event in events if event.kind in {"handoff.started", "handoff.completed"}]
    assert handoff_events == ["handoff.started", "handoff.completed"]
    turns = asyncio.run(memory.read())
    assert [turn.agent_id for turn in turns] == ["assistant", "reviewer"]
    assert all(
        not isinstance(message, AssistantMessage) or message.content != "reviewed"
        for turn in turns[:1]
        for message in turn.messages
    )


def test_handoff_conflict_rejects_the_whole_batch_before_any_control_transition(tmp_path: Path) -> None:
    """Two Handoffs in one model batch become paired feedback without switching."""
    conflict_batch: tuple[OpenAIStreamChunk, ...] = (
        OpenAIStreamChunk(
            id="conflict",
            model="fake-model",
            choices=(
                OpenAIStreamChoice(
                    delta=OpenAIStreamDelta(
                        tool_calls=(
                            OpenAIStreamToolCall(
                                index=0,
                                id="handoff-a",
                                function=OpenAIStreamFunction(
                                    name="transfer_to_reviewer",
                                    arguments='{"task":"a"}',
                                ),
                            ),
                            OpenAIStreamToolCall(
                                index=1,
                                id="handoff-b",
                                function=OpenAIStreamFunction(
                                    name="transfer_to_reviewer",
                                    arguments='{"task":"b"}',
                                ),
                            ),
                        ),
                    ),
                ),
            ),
        ),
        OpenAIStreamChunk(
            id="conflict",
            model="fake-model",
            choices=(OpenAIStreamChoice(delta=OpenAIStreamDelta(), finish_reason="tool_calls"),),
        ),
    )
    source_model = FakeOpenAIModel((conflict_batch, text_batch("source recovered", "source-final")))
    target_model = FakeAnthropicModel((anthropic_text_batch("unexpected target", "target-final"),))
    root = tmp_path / "root"
    root.mkdir()
    runner, source = make_runner(source_model, root)
    target = Agent(name="reviewer", model=target_model, prompt="Review the task.")
    binding = Handoff(
        source=source,
        target=target,
        name="transfer_to_reviewer",
        description="Transfer the current task to the reviewer.",
    ).bind(runner)

    result = asyncio.run(
        runner.run(
            source,
            "route this",
            memory=ShortMemory.in_memory("handoff-conflict"),
            config=RunConfig(),
            handoffs=(binding,),
        )
    )

    assert result.status is RunStatus.SUCCEEDED
    assert result.final_output == "source recovered"
    assert len(source_model.requests) == 2
    assert target_model.requests == []


def test_handoff_accepts_anthropic_source_and_openai_target_in_the_same_run(tmp_path: Path) -> None:
    """The normalized Runtime control path supports the reverse provider direction."""
    source_model = FakeAnthropicModel((anthropic_handoff_batch("transfer_to_reviewer", "handoff-call"),))
    target_model = FakeOpenAIModel((text_batch("reviewed in reverse", "target-final"),))
    root = tmp_path / "reverse"
    root.mkdir()
    runner, source = make_runner(source_model, root)
    target = Agent(name="reviewer", model=target_model, prompt="Review the task.")
    binding = Handoff(
        source=source,
        target=target,
        name="transfer_to_reviewer",
        description="Transfer the current task to the reviewer.",
    ).bind(runner)

    result = asyncio.run(
        runner.run(
            source,
            "route this",
            memory=ShortMemory.in_memory("reverse-handoff"),
            config=RunConfig(),
            handoffs=(binding,),
        )
    )

    assert result.status is RunStatus.SUCCEEDED
    assert result.final_output == "reviewed in reverse"
    assert len(source_model.requests) == 1
    assert len(target_model.requests) == 1
