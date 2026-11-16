import asyncio
import hashlib
from pathlib import Path

import pytest
from pydantic import TypeAdapter

import pleiad.context.compaction.observation as observation_module
from pleiad.context.budget import ApproximateTokenCounter, BudgetGuard, ContextBudget, ContextCountInput
from pleiad.context.compaction.compactor import ContextCompactor
from pleiad.context.compaction.models import CompactionState
from pleiad.context.compaction.observation import CompactionEventContext
from pleiad.context.errors import ContextCompactionError, ContextWindowError
from pleiad.context.rendering import ContextRenderer
from pleiad.context.selection.models import ContextBlock, ContextView
from pleiad.contracts import JsonObject
from pleiad.events.context import (
    CompactionTokens,
    ContextCompactionCancelledEvent,
    ContextCompactionCompletedEvent,
    ContextCompactionFailedEvent,
    ContextCompactionStartedEvent,
)
from pleiad.events.contracts import Event
from pleiad.messages.images import ImageBlock, ImageReference
from pleiad.messages.message import AssistantMessage, ToolCall, ToolMessage, UserMessage
from pleiad.messages.results import ToolResultReference
from pleiad.runtime.execution.read_tool_result import ReadToolResultTool
from pleiad.sessions.backends.in_memory import InMemorySessionStore
from pleiad.sessions.backends.sqlite import SqliteSessionStore
from pleiad.sessions.input import InputEnvelope
from pleiad.sessions.model_step import ModelStep
from pleiad.sessions.session import Session
from pleiad.sessions.store import SessionStore
from pleiad.sessions.writer import SessionWriter
from pleiad.tools.contracts import ToolCall as ExecutionToolCall
from pleiad.tools.contracts import ToolContext, ToolResult
from pleiad.tools.runtime import ToolRuntime


class MessageCountCounter:
    """Count Messages to make ModelStep-boundary compaction deterministic."""

    estimated = False

    def count(self, request: ContextCountInput) -> int:
        """Return the number of request messages."""
        return len(request.messages)


class CompactionEvents:
    """Collect public notifications at the host event boundary."""

    def __init__(self) -> None:
        self.events: list[Event] = []
        self.context = CompactionEventContext(
            run_id="current-run",
            step_id="active-step",
            agent_name="request-agent",
            parent_run_id="parent-run",
            event_sink=self.receive,
        )

    async def receive(self, event: Event) -> None:
        self.events.append(event)


def completed_step(sequence: int) -> ModelStep:
    """Create one complete direct-answer ModelStep."""
    return ModelStep(
        step_id=f"step-{sequence}",
        run_id="run",
        run_sequence=sequence,
        sequence=sequence,
        agent_id="assistant",
        state="completed",
        messages=(
            UserMessage(message_id=f"user-{sequence}", content=f"question-{sequence}"),
            AssistantMessage(message_id=f"assistant-{sequence}", content=f"answer-{sequence}"),
        ),
    )


async def summarize(previous: str | None, steps: tuple[ModelStep, ...]) -> str:
    """Return a deterministic summary of omitted ModelStep sequences."""
    sequence_text = ",".join(str(step.sequence) for step in steps)
    return f"omitted={sequence_text} previous={previous or 'none'}"


async def create_writer(session_id: str, store: SessionStore, content: str) -> SessionWriter:
    session = Session(session_id, store)
    await session.enqueue(InputEnvelope(input_id="user-message", kind="user", source="test", content=content))
    snapshot = await store.start(
        session_id,
        invocation_id="test",
        parameters_digest=hashlib.sha256(content.encode()).hexdigest(),
        input_ids=("user-message",),
        input_mode="batch",
        agent_id="agent",
    )
    return SessionWriter(session, snapshot.snapshot)


def test_compactor_discards_only_whole_old_steps() -> None:
    """Compaction retains a complete recent ModelStep and records the omitted boundary."""
    budget = ContextBudget(window_tokens=20, output_reserve=2, compact_at=5, recent_tokens=3)
    compactor = ContextCompactor(BudgetGuard(budget, MessageCountCounter()))
    notifications = CompactionEvents()

    result = asyncio.run(
        compactor.compact(
            (completed_step(1), completed_step(2), completed_step(3)),
            instructions="rules",
            input_messages=(),
            tools=(),
            context_view=ContextView(),
            summarizer=summarize,
            event_context=notifications.context,
        ),
    )

    assert [message.message_id for message in result.messages] == ["user-3", "assistant-3"]
    assert result.state == CompactionState(
        summary="omitted=1,2 previous=none",
        through_sequence=2,
    )
    started, completed = notifications.events
    assert isinstance(started, ContextCompactionStartedEvent)
    assert isinstance(completed, ContextCompactionCompletedEvent)
    assert started.compaction_id == completed.compaction_id and started.event_id != completed.event_id
    assert (started.run_id, started.step_id, started.agent_name) == ("current-run", "active-step", "request-agent")
    assert started.parent_run_id == "parent-run"
    assert started.trigger_reasons == ("compact_at_reached",)
    assert started.before == CompactionTokens(input_tokens=6, estimated=False, unestimated_images=0)
    assert completed.after == CompactionTokens(input_tokens=2, estimated=False, unestimated_images=0)
    assert completed.duration_ms >= 0
    assert completed.strategies_attempted == completed.strategies_completed == ("history_trim", "rolling_summary")
    assert completed.summary is not None and completed.summary.operation == "created"
    assert completed.summary.previous_through_sequence is None and completed.summary.through_sequence == 2
    assert [step.sequence for step in completed.summary.added_steps] == [1, 2]
    assert [step.sequence for step in completed.retained_steps] == [3]
    assert [step.sequence for step in completed.omitted_steps] == [1, 2]
    assert all(step.run_id == "run" for step in completed.omitted_steps)


def test_compactor_counts_retained_reference_only_in_rendered_message() -> None:
    budget = ContextBudget(window_tokens=2_000, output_reserve=100, compact_at=1_800, recent_tokens=1_000)
    view = ContextView((ContextBlock(kind="document", origin="review", content="reference " * 100),))
    user = UserMessage(content="question")
    counter = ApproximateTokenCounter()
    result = asyncio.run(
        ContextCompactor(BudgetGuard(budget, counter)).compact(
            (), instructions="rules", input_messages=(user,), tools=(), context_view=view
        )
    )
    wire_user = ContextRenderer().render(user, context_view=result.context_view)
    wire_count = counter.count(
        ContextCountInput(instructions="rules", messages=(wire_user,), tools=(), context_view=ContextView())
    )
    assert result.context_view == view
    assert result.budget.input_tokens == wire_count
    assert user.content == "question"


@pytest.mark.parametrize("persisted", [False, True], ids=["in-memory", "sqlite-reopened"])
@pytest.mark.parametrize("force_compaction", [False, True], ids=["within-budget", "compact-latest-step"])
def test_large_tool_observation_can_be_read_in_pages_after_compaction(
    tmp_path: Path, persisted: bool, force_compaction: bool
) -> None:
    async def scenario() -> None:
        database = tmp_path / "memory.sqlite3"
        store = SqliteSessionStore(database) if persisted else InMemorySessionStore()
        memory = await create_writer("chat", store, "Find the original evidence")
        original = ToolMessage(message_id="original-result", tool_call_id="lookup", content="事实证据" * 30_000)
        step = await memory.begin("agent")
        await memory.complete(
            step.step_id,
            (
                AssistantMessage(tool_calls=(ToolCall(id="lookup", name="lookup", arguments="{}"),)),
                original,
            ),
        )
        if persisted:
            restored = Session("chat", SqliteSessionStore(database))
            memory = SessionWriter(restored, await restored.store.run("chat", memory.run_id))
        budget = ContextBudget(
            window_tokens=32_000,
            output_reserve=2_000,
            compact_at=2 if force_compaction else 24_000,
            recent_tokens=1 if force_compaction else 16_000,
        )
        counter = MessageCountCounter() if force_compaction else ApproximateTokenCounter()
        notifications = CompactionEvents()
        compacted = await ContextCompactor(BudgetGuard(budget, counter)).compact(
            await memory.read(),
            instructions="rules",
            input_messages=(),
            tools=(),
            context_view=ContextView(),
            event_context=notifications.context,
        )
        assert notifications.events == []
        view = next(message for message in compacted.messages if isinstance(message, ToolMessage))
        placeholder = TypeAdapter[JsonObject](JsonObject).validate_json(view.content)
        reference = ToolResultReference.model_validate(
            {key: placeholder[key] for key in ("message_id", "sha256", "total_chars")}
        )
        assert reference == ToolResultReference.from_message(original)
        assert len(view.content) < 1_000
        reader = ReadToolResultTool(memory)
        pages: list[str] = []
        start = 0
        while start < reference.total_chars:
            outcome = await ToolRuntime().execute(
                reader,
                ExecutionToolCall(
                    call_id="read-page",
                    tool_code="read_tool_result",
                    arguments={"message_id": reference.message_id, "start_char": start, "max_chars": 8_000},
                ),
                ToolContext(call_id="read-page"),
            )
            assert isinstance(outcome, ToolResult) and outcome.ok and outcome.output is not None
            content = outcome.output["content"]
            next_start = outcome.output["next_start_char"]
            assert isinstance(content, str) and isinstance(next_start, int)
            assert outcome.output["reference"] == reference.model_dump(mode="json")
            pages.append(content)
            start = next_start
        assert "".join(pages) == original.content
        assert await memory.read_tool_result(original.message_id) == original

    asyncio.run(scenario())


def test_result_reader_cannot_read_another_conversation_or_non_tool_message(tmp_path: Path) -> None:
    async def scenario() -> None:
        database = tmp_path / "memory.sqlite3"
        source = await create_writer("source", SqliteSessionStore(database), "private request")
        step = await source.begin("agent")
        await source.complete(
            step.step_id,
            (
                AssistantMessage(tool_calls=(ToolCall(id="private-call", name="lookup", arguments="{}"),)),
                ToolMessage(message_id="private-result", tool_call_id="private-call", content="private evidence"),
            ),
        )
        for memory, message_id in (
            (await create_writer("other", SqliteSessionStore(database), "other request"), "private-result"),
            (source, "user-message"),
        ):
            outcome = await ToolRuntime().execute(
                ReadToolResultTool(memory),
                ExecutionToolCall(call_id="read", tool_code="read_tool_result", arguments={"message_id": message_id}),
                ToolContext(call_id="read"),
            )
            assert isinstance(outcome, ToolResult) and not outcome.ok
            assert outcome.error_code == "tool.result.not_found"
            assert outcome.output is None

    asyncio.run(scenario())


def test_result_references_remain_discoverable_after_their_whole_step_is_omitted() -> None:
    async def scenario() -> None:
        memory = await create_writer("omitted-tool-step", InMemorySessionStore(), "original task")
        first = await memory.begin("agent")
        original = ToolMessage(message_id="omitted-result", tool_call_id="call", content="original facts" * 100)
        await memory.complete(
            first.step_id,
            (AssistantMessage(tool_calls=(ToolCall(id="call", name="lookup", arguments="{}"),)), original),
        )
        second = await memory.begin("agent", (UserMessage(content="next task"),))
        await memory.complete(second.step_id, (AssistantMessage(content="answer"),))
        budget = ContextBudget(window_tokens=20, output_reserve=2, compact_at=4, recent_tokens=2)
        compacted = await ContextCompactor(BudgetGuard(budget, MessageCountCounter())).compact(
            await memory.read(), instructions="rules", input_messages=(), tools=(), context_view=ContextView()
        )
        assert all(message.message_id != original.message_id for message in compacted.messages)
        outcome = await ToolRuntime().execute(
            ReadToolResultTool(memory),
            ExecutionToolCall(call_id="catalog", tool_code="read_tool_result", arguments={}),
            ToolContext(call_id="catalog"),
        )
        assert isinstance(outcome, ToolResult) and outcome.ok and outcome.output is not None
        references = outcome.output["references"]
        assert isinstance(references, list) and len(references) == 1
        reference = references[0]
        assert isinstance(reference, dict)
        assert reference["message_id"] == original.message_id
        assert reference["preview"] == original.content[:128]

    asyncio.run(scenario())


@pytest.mark.parametrize("retain_reference", [False, True], ids=["remove-all-references", "retain-small-reference"])
def test_continuation_budget_counts_and_prunes_references_beside_history_user(retain_reference: bool) -> None:
    """A continuation request counts its references before selecting a budget view."""
    budget = ContextBudget(window_tokens=1_000, output_reserve=100, compact_at=800, recent_tokens=500)
    steps = (completed_step(1),)
    blocks: tuple[ContextBlock, ...] = (
        ContextBlock(kind="knowledge", origin="large-document", content="large-reference " * 1_000),
    )
    if retain_reference:
        blocks = (
            ContextBlock(kind="knowledge", origin="retained-document", content="short retained reference"),
            *blocks,
        )
    view = ContextView(blocks)
    retained_count = 1 if retain_reference else 0
    notifications = CompactionEvents()
    result = asyncio.run(
        ContextCompactor(BudgetGuard(budget)).compact(
            steps,
            instructions="rules",
            input_messages=(),
            tools=(),
            context_view=view,
            event_context=notifications.context,
        )
    )

    assert result.context_view.blocks == view.blocks[:retained_count]
    assert result.messages == steps[0].messages
    wire_messages = ContextRenderer().render_messages(
        result.messages, input_messages=(), context_view=result.context_view
    )
    wire_count = ApproximateTokenCounter().count(
        ContextCountInput(instructions="rules", messages=wire_messages, tools=(), context_view=ContextView())
    )
    assert result.budget.input_tokens == wire_count
    started, completed = notifications.events
    assert isinstance(started, ContextCompactionStartedEvent)
    assert isinstance(completed, ContextCompactionCompletedEvent)
    assert started.trigger_reasons == ("compact_at_reached", "context_window_exceeded")
    assert started.before.input_tokens > budget.available_tokens
    assert started.before.estimated and completed.after.estimated
    assert completed.after.input_tokens == wire_count
    assert completed.strategies_attempted == completed.strategies_completed == ("context_block_trim",)
    assert completed.summary is None and completed.omitted_steps == ()
    assert [(block.index, block.kind, block.origin) for block in completed.retained_context_blocks] == [
        (index, block.kind, block.origin) for index, block in enumerate(view.blocks[:retained_count])
    ]
    assert [(block.index, block.kind, block.origin) for block in completed.removed_context_blocks] == [
        (retained_count, "knowledge", "large-document")
    ]


def test_compactor_rejects_a_batch_that_cannot_fit_without_dropping_current_inputs() -> None:
    budget = ContextBudget(window_tokens=4, output_reserve=1, compact_at=2, recent_tokens=1)
    inputs = tuple(UserMessage(content=f"request {index}") for index in range(4))

    with pytest.raises(ContextWindowError):
        asyncio.run(
            ContextCompactor(BudgetGuard(budget, MessageCountCounter())).compact(
                (), instructions="rules", input_messages=inputs, tools=(), context_view=ContextView()
            )
        )


@pytest.mark.parametrize(
    "arguments",
    [
        {"operation_id": "operation", "message_id": "message"},
        {"operation_id": "operation", "after_operation_id": "cursor"},
        {"kind": "operation", "after_message_id": "cursor"},
        {"after_operation_id": "cursor"},
    ],
)
def test_result_reader_rejects_ambiguous_operation_and_message_identities(arguments: JsonObject) -> None:
    async def scenario() -> None:
        writer = await create_writer("bound-session", InMemorySessionStore(), "Read existing evidence")
        outcome = await ToolRuntime().execute(
            ReadToolResultTool(writer),
            ExecutionToolCall(call_id="invalid-reference", tool_code="read_tool_result", arguments=arguments),
            ToolContext(call_id="invalid-reference"),
        )
        assert isinstance(outcome, ToolResult) and not outcome.ok and outcome.error_code == "tool.arguments.invalid"

    asyncio.run(scenario())


def test_compaction_keeps_current_context_when_only_older_history_exceeds_the_window() -> None:
    steps = tuple(completed_step(sequence) for sequence in range(1, 41))
    view = ContextView((ContextBlock(kind="routing", origin="current-batch", content="CURRENT_FIXED_INPUT_IDS " * 5),))
    inputs = (UserMessage(content="Handle the current input"),)
    budget = ContextBudget(window_tokens=300, output_reserve=32, compact_at=200, recent_tokens=128)
    result = asyncio.run(
        ContextCompactor(BudgetGuard(budget)).compact(
            steps, instructions="rules", input_messages=inputs, tools=(), context_view=view
        )
    )
    assert result.context_view == view
    assert result.messages[-1] == steps[-1].messages[-1]
    assert len(result.messages) < sum(len(step.messages) for step in steps)
    rendered = ContextRenderer().render_messages(
        result.messages, input_messages=inputs, context_view=result.context_view
    )
    assert any(
        isinstance(message, UserMessage) and "CURRENT_FIXED_INPUT_IDS" in str(message.content) for message in rendered
    )
    assert not result.budget.exceeds_window


def test_repeated_reference_compaction_continues_without_claiming_a_host_summary_exists() -> None:
    async def scenario() -> None:
        writer = await create_writer("reference-history", InMemorySessionStore(), "Read the original evidence")
        for index in range(1, 5):
            step = await writer.begin("agent")
            await writer.complete(
                step.step_id,
                (
                    AssistantMessage(tool_calls=(ToolCall(id=f"call-{index}", name="lookup", arguments="{}"),)),
                    ToolMessage(
                        message_id=f"evidence-{index}", tool_call_id=f"call-{index}", content=f"original-{index} " * 100
                    ),
                ),
            )
        budget = ContextBudget(window_tokens=2_000, output_reserve=200, compact_at=1_100, recent_tokens=1_000)
        compactor = ContextCompactor(BudgetGuard(budget))
        first = await compactor.compact(
            await writer.read(), instructions="rules", input_messages=(), tools=(), context_view=ContextView()
        )
        assert first.state is not None and first.state.replacements
        assert first.state.summary is None and first.state.through_sequence == 0
        next_step = await writer.begin("agent")
        await writer.complete(
            next_step.step_id,
            (
                AssistantMessage(tool_calls=(ToolCall(id="call-5", name="lookup", arguments="{}"),)),
                ToolMessage(message_id="evidence-5", tool_call_id="call-5", content="original-5 " * 100),
            ),
        )
        following = await compactor.compact(
            await writer.read(),
            instructions="rules",
            input_messages=(),
            tools=(),
            context_view=ContextView(),
            prior_state=first.state,
        )
        assert following.messages[-1].message_id == (await writer.read())[-1].messages[-1].message_id
        assert following.state is not None and following.state.summary is None
        original = await writer.read_tool_result("evidence-1")
        assert original is not None and original.content == "original-1 " * 100

    asyncio.run(scenario())


def test_a_real_host_summary_requires_an_updater_when_another_old_step_is_omitted() -> None:
    async def scenario() -> None:
        budget = ContextBudget(window_tokens=20, output_reserve=2, compact_at=5, recent_tokens=3)
        compactor = ContextCompactor(BudgetGuard(budget, MessageCountCounter()))
        first = await compactor.compact(
            tuple(completed_step(sequence) for sequence in range(1, 4)),
            instructions="rules",
            input_messages=(),
            tools=(),
            context_view=ContextView(),
            summarizer=summarize,
        )
        assert first.state is not None and first.state.summary is not None
        with pytest.raises(ContextWindowError, match="explicit summarizer"):
            await compactor.compact(
                tuple(completed_step(sequence) for sequence in range(1, 5)),
                instructions="rules",
                input_messages=(),
                tools=(),
                context_view=ContextView(),
                prior_state=first.state,
            )

    asyncio.run(scenario())


def image_tool_step(sequence: int) -> ModelStep:
    image = ImageBlock(
        image=ImageReference(reference=f"asset:{sequence}", sha256="a" * 64, mime="image/png", width=100, height=100)
    )
    return ModelStep(
        step_id=f"image-step-{sequence}",
        run_id="run",
        run_sequence=sequence,
        sequence=sequence,
        agent_id="assistant",
        state="completed",
        messages=(
            UserMessage(message_id=f"image-user-{sequence}", content=(image,)),
            AssistantMessage(
                message_id=f"image-assistant-{sequence}",
                tool_calls=(ToolCall(id=f"inspect-{sequence}", name="inspect", arguments="{}"),),
            ),
            ToolMessage(message_id=f"image-result-{sequence}", tool_call_id=f"inspect-{sequence}", content="checked"),
        ),
    )


def test_compaction_retains_image_steps() -> None:
    image_step = image_tool_step(2)
    latest = completed_step(5)
    budget = ContextBudget(window_tokens=20, output_reserve=2, compact_at=6, recent_tokens=5)
    notifications = CompactionEvents()

    compacted = asyncio.run(
        ContextCompactor(BudgetGuard(budget, MessageCountCounter())).compact(
            (completed_step(1), image_step, completed_step(3), completed_step(4), latest),
            instructions="rules",
            input_messages=(),
            tools=(),
            context_view=ContextView(),
            summarizer=summarize,
            event_context=notifications.context,
        )
    )

    assert compacted.messages == (*image_step.messages, *latest.messages)
    assert compacted.state == CompactionState(summary="omitted=1,3,4 previous=none", through_sequence=4)
    completed = notifications.events[-1]
    assert isinstance(completed, ContextCompactionCompletedEvent)
    assert completed.summary is not None and completed.summary.through_sequence == 4
    assert [step.sequence for step in completed.summary.added_steps] == [1, 3, 4]
    assert [step.step_id for step in completed.retained_steps] == ["image-step-2", "step-5"]
    assert [step.sequence for step in completed.omitted_steps] == [1, 3, 4]


def test_compaction_prioritizes_images() -> None:
    image_step = image_tool_step(1)
    latest = completed_step(4)
    budget = ContextBudget(window_tokens=7, output_reserve=1, compact_at=5, recent_tokens=4)

    compacted = asyncio.run(
        ContextCompactor(BudgetGuard(budget, MessageCountCounter())).compact(
            (image_step, completed_step(2), completed_step(3), latest),
            instructions="rules",
            input_messages=(),
            tools=(),
            context_view=ContextView(),
        )
    )

    assert compacted.messages == (*image_step.messages, *latest.messages)
    assert compacted.budget.input_tokens == 5
    assert not compacted.budget.exceeds_window


def test_compaction_rejects_image_overflow() -> None:
    budget = ContextBudget(window_tokens=7, output_reserve=1, compact_at=5, recent_tokens=4)

    with pytest.raises(ContextWindowError):
        asyncio.run(
            ContextCompactor(BudgetGuard(budget, MessageCountCounter())).compact(
                (image_tool_step(1), image_tool_step(2)),
                instructions="rules",
                input_messages=(UserMessage(content="compare both images"),),
                tools=(),
                context_view=ContextView(),
            )
        )


def test_summary_covers_text_between_pinned_image_and_recent_steps_after_restoration() -> None:
    async def scenario() -> None:
        image_step = image_tool_step(2)
        steps = (completed_step(1), image_step, completed_step(3), completed_step(4))
        budget = ContextBudget(window_tokens=20, output_reserve=2, compact_at=6, recent_tokens=5)
        compactor = ContextCompactor(BudgetGuard(budget, MessageCountCounter()))
        notifications = CompactionEvents()
        first = await compactor.compact(
            steps,
            instructions="rules",
            input_messages=(),
            tools=(),
            context_view=ContextView(),
            summarizer=summarize,
            event_context=notifications.context,
        )
        assert first.state is not None
        restored_state = CompactionState.model_validate_json(first.state.model_dump_json())
        following = await compactor.compact(
            (*steps, completed_step(5)),
            instructions="rules",
            input_messages=(),
            tools=(),
            context_view=ContextView(),
            prior_state=restored_state,
            summarizer=summarize,
            event_context=notifications.context,
        )

        assert following.messages == (*image_step.messages, *completed_step(5).messages)
        assert restored_state == CompactionState(summary="omitted=1,3 previous=none", through_sequence=3)
        assert following.state == CompactionState(
            summary="omitted=4 previous=omitted=1,3 previous=none", through_sequence=4
        )
        previous_completed = notifications.events[1]
        completed = notifications.events[-1]
        assert isinstance(previous_completed, ContextCompactionCompletedEvent)
        assert isinstance(completed, ContextCompactionCompletedEvent)
        assert previous_completed.compaction_id != completed.compaction_id
        assert completed.summary is not None and completed.summary.operation == "updated"
        assert completed.summary.previous_through_sequence == 3 and completed.summary.through_sequence == 4
        assert [step.sequence for step in completed.summary.added_steps] == [4]
        assert [step.sequence for step in completed.retained_steps] == [2, 5]
        attempts = [event for event in notifications.events if isinstance(event, ContextCompactionStartedEvent)]
        assert len(attempts) == 2 and attempts[0].compaction_id != attempts[1].compaction_id
        assert completed.compaction_id == attempts[1].compaction_id

    asyncio.run(scenario())


def test_first_image_remains_visible_while_every_intervening_text_step_is_summarized_once() -> None:
    async def scenario() -> None:
        image_step = image_tool_step(1)
        budget = ContextBudget(window_tokens=20, output_reserve=2, compact_at=6, recent_tokens=5)
        compactor = ContextCompactor(BudgetGuard(budget, MessageCountCounter()))
        submitted: list[tuple[int, ...]] = []

        async def record_summary(previous: str | None, steps: tuple[ModelStep, ...]) -> str:
            submitted.append(tuple(step.sequence for step in steps))
            return await summarize(previous, steps)

        state = None
        for latest_sequence in (4, 5, 6):
            steps = (image_step, *(completed_step(sequence) for sequence in range(2, latest_sequence + 1)))
            result = await compactor.compact(
                steps,
                instructions="rules",
                input_messages=(),
                tools=(),
                context_view=ContextView(),
                prior_state=state,
                summarizer=record_summary,
            )
            assert result.messages == (*image_step.messages, *completed_step(latest_sequence).messages)
            assert result.state is not None
            state = CompactionState.model_validate_json(result.state.model_dump_json())
            assert state.through_sequence == latest_sequence - 1
        assert submitted == [(2, 3), (4,), (5,)]
        assert state is not None and state.summary == "omitted=5 previous=omitted=4 previous=omitted=2,3 previous=none"

    asyncio.run(scenario())


def test_restored_prefix_summary_adds_previously_omitted_text_beyond_pinned_image() -> None:
    async def scenario() -> None:
        image_step = image_tool_step(2)
        steps = (completed_step(1), image_step, completed_step(3), completed_step(4))
        budget = ContextBudget(window_tokens=20, output_reserve=2, compact_at=6, recent_tokens=5)
        prior = CompactionState(summary="first question remains relevant", through_sequence=1)
        result = await ContextCompactor(BudgetGuard(budget, MessageCountCounter())).compact(
            steps,
            instructions="rules",
            input_messages=(),
            tools=(),
            context_view=ContextView(),
            prior_state=CompactionState.model_validate_json(prior.model_dump_json()),
            summarizer=summarize,
        )
        assert result.messages == (*image_step.messages, *completed_step(4).messages)
        assert result.state == CompactionState(
            summary="omitted=3 previous=first question remains relevant",
            through_sequence=3,
        )

    asyncio.run(scenario())


@pytest.mark.parametrize("compact_at", [2, 3], ids=["at-threshold-without-actions", "within-budget"])
def test_budget_checks_without_actual_reductions_emit_no_compaction_events(compact_at: int) -> None:
    notifications = CompactionEvents()
    step = completed_step(1)
    budget = ContextBudget(window_tokens=20, output_reserve=2, compact_at=compact_at, recent_tokens=1)

    result = asyncio.run(
        ContextCompactor(BudgetGuard(budget, MessageCountCounter())).compact(
            (step,),
            instructions="rules",
            input_messages=(),
            tools=(),
            context_view=ContextView(),
            event_context=notifications.context,
        )
    )

    assert result.messages == step.messages and result.state is None
    assert notifications.events == []


def test_reusing_a_summary_without_new_reductions_emits_no_compaction_events() -> None:
    notifications = CompactionEvents()
    prior = CompactionState(summary="previous history", through_sequence=1)
    budget = ContextBudget(window_tokens=20, output_reserve=2, compact_at=2, recent_tokens=1)

    result = asyncio.run(
        ContextCompactor(BudgetGuard(budget, MessageCountCounter())).compact(
            (completed_step(2),),
            instructions="rules",
            input_messages=(),
            tools=(),
            context_view=ContextView(),
            prior_state=prior,
            event_context=notifications.context,
        )
    )

    assert result.state == prior
    assert notifications.events == []


def test_compaction_duration_excludes_time_spent_notifying_observers(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        clock = 0.0
        events: list[Event] = []

        def read_clock() -> float:
            return clock

        async def observe(event: Event) -> None:
            nonlocal clock
            clock += 100
            events.append(event)

        async def summarize_history(previous: str | None, steps: tuple[ModelStep, ...]) -> str:
            nonlocal clock
            clock += 0.25
            return await summarize(previous, steps)

        monkeypatch.setattr(observation_module, "monotonic", read_clock)
        context = CompactionEventContext(run_id="run", step_id="active", agent_name="agent", event_sink=observe)
        budget = ContextBudget(window_tokens=20, output_reserve=2, compact_at=5, recent_tokens=3)
        await ContextCompactor(BudgetGuard(budget, MessageCountCounter())).compact(
            tuple(completed_step(sequence) for sequence in range(1, 4)),
            instructions="rules",
            input_messages=(),
            tools=(),
            context_view=ContextView(),
            summarizer=summarize_history,
            event_context=context,
        )
        assert clock == 200.25
        assert isinstance(events[-1], ContextCompactionCompletedEvent)
        assert events[-1].duration_ms == 250

    asyncio.run(scenario())


def test_reference_only_compaction_reports_real_replacements_without_claiming_a_summary() -> None:
    notifications = CompactionEvents()
    pinned = image_tool_step(1)
    original = ToolMessage(message_id="large-observation", tool_call_id="inspect-1", content="original facts " * 100)
    pinned = pinned.model_copy(update={"messages": (*pinned.messages[:-1], original)})
    latest = completed_step(2)
    budget = ContextBudget(window_tokens=20, output_reserve=2, compact_at=5, recent_tokens=4)

    result = asyncio.run(
        ContextCompactor(BudgetGuard(budget, MessageCountCounter())).compact(
            (pinned, latest),
            instructions="rules",
            input_messages=(),
            tools=(),
            context_view=ContextView(),
            event_context=notifications.context,
        )
    )

    completed = notifications.events[-1]
    assert isinstance(completed, ContextCompactionCompletedEvent)
    assert completed.strategies_attempted == completed.strategies_completed == ("tool_result_reference",)
    assert completed.replaced_tool_result_ids == (original.message_id,)
    assert completed.summary is None and completed.omitted_steps == ()
    assert [step.sequence for step in completed.retained_steps] == [1, 2]
    reference_message = next(message for message in result.messages if message.message_id == original.message_id)
    assert isinstance(reference_message, ToolMessage) and reference_message.content != original.content
    reference = TypeAdapter[JsonObject](JsonObject).validate_json(reference_message.content)
    assert reference["message_id"] == original.message_id
    assert reference["total_chars"] == len(original.content)
    assert pinned.messages[-1] == original


def test_compaction_keeps_unestimated_image_costs_unknown_in_both_token_snapshots() -> None:
    notifications = CompactionEvents()
    counter = ApproximateTokenCounter(allow_unestimated_images=True)
    budget = ContextBudget(window_tokens=10_000, output_reserve=100, compact_at=300, recent_tokens=250)
    text_steps = tuple(completed_step(sequence) for sequence in range(2, 9))
    result = asyncio.run(
        ContextCompactor(BudgetGuard(budget, counter)).compact(
            (image_tool_step(1), *text_steps),
            instructions="rules",
            input_messages=(),
            tools=(),
            context_view=ContextView(),
            event_context=notifications.context,
        )
    )

    started, completed = notifications.events
    assert isinstance(started, ContextCompactionStartedEvent)
    assert isinstance(completed, ContextCompactionCompletedEvent)
    assert started.before.estimated and completed.after.estimated
    assert started.before.image_tokens is None and completed.after.image_tokens is None
    assert started.before.unestimated_images == completed.after.unestimated_images == 1
    assert completed.after.input_tokens == result.budget.input_tokens < started.before.input_tokens
    assert any(step.step_id == "image-step-1" for step in completed.retained_steps)


@pytest.mark.parametrize("empty_summary", [False, True], ids=["summarizer-error", "empty-summary"])
def test_failed_summary_reports_only_confirmed_stages_and_preserves_the_original_failure(empty_summary: bool) -> None:
    async def scenario() -> None:
        notifications = CompactionEvents()
        original_error = RuntimeError("private upstream diagnostic")

        async def failing_summary(previous: str | None, steps: tuple[ModelStep, ...]) -> str:
            if empty_summary:
                return "  "
            raise original_error

        budget = ContextBudget(window_tokens=20, output_reserve=2, compact_at=5, recent_tokens=3)
        with pytest.raises(ContextCompactionError) as caught:
            await ContextCompactor(BudgetGuard(budget, MessageCountCounter())).compact(
                tuple(completed_step(sequence) for sequence in range(1, 4)),
                instructions="rules",
                input_messages=(),
                tools=(),
                context_view=ContextView(),
                summarizer=failing_summary,
                event_context=notifications.context,
            )

        started, failed = notifications.events
        assert isinstance(started, ContextCompactionStartedEvent)
        assert isinstance(failed, ContextCompactionFailedEvent)
        assert failed.compaction_id == started.compaction_id
        assert failed.strategies_attempted == ("history_trim", "rolling_summary")
        assert failed.strategies_completed == ("history_trim",)
        assert failed.error_code == "context.compaction.failed"
        assert failed.error_message == caught.value.message
        assert failed.duration_ms >= 0
        assert "after" not in failed.model_dump() and "summary" not in failed.model_dump()
        assert "private upstream diagnostic" not in failed.model_dump_json()
        if not empty_summary:
            assert caught.value.__cause__ is original_error

    asyncio.run(scenario())


def test_context_trimming_that_still_cannot_fit_reports_failure_without_a_final_request() -> None:
    notifications = CompactionEvents()
    budget = ContextBudget(window_tokens=4, output_reserve=1, compact_at=2, recent_tokens=1)
    inputs = tuple(UserMessage(content=f"required request {index}") for index in range(4))
    view = ContextView((ContextBlock(kind="knowledge", origin="document", content="optional reference"),))

    with pytest.raises(ContextWindowError) as caught:
        asyncio.run(
            ContextCompactor(BudgetGuard(budget, MessageCountCounter())).compact(
                (),
                instructions="rules",
                input_messages=inputs,
                tools=(),
                context_view=view,
                event_context=notifications.context,
            )
        )

    started, failed = notifications.events
    assert isinstance(started, ContextCompactionStartedEvent)
    assert isinstance(failed, ContextCompactionFailedEvent)
    assert failed.compaction_id == started.compaction_id
    assert failed.strategies_attempted == failed.strategies_completed == ("context_block_trim",)
    assert failed.error_code == "context.window.exceeded" and failed.error_message == caught.value.message
    assert "after" not in failed.model_dump() and "retained_context_blocks" not in failed.model_dump()


def test_cancelling_an_active_summary_reports_one_cancellation_and_propagates_cancelled_error() -> None:
    async def scenario() -> None:
        notifications = CompactionEvents()
        entered = asyncio.Event()
        cleaned = asyncio.Event()
        release = asyncio.Event()

        async def blocked_summary(previous: str | None, steps: tuple[ModelStep, ...]) -> str:
            entered.set()
            try:
                await release.wait()
                return await summarize(previous, steps)
            finally:
                cleaned.set()

        budget = ContextBudget(window_tokens=20, output_reserve=2, compact_at=5, recent_tokens=3)
        async with asyncio.TaskGroup() as tasks:
            task = tasks.create_task(
                ContextCompactor(BudgetGuard(budget, MessageCountCounter())).compact(
                    tuple(completed_step(sequence) for sequence in range(1, 4)),
                    instructions="rules",
                    input_messages=(),
                    tools=(),
                    context_view=ContextView(),
                    summarizer=blocked_summary,
                    event_context=notifications.context,
                )
            )
            await asyncio.wait_for(entered.wait(), timeout=3)
            assert len(notifications.events) == 1
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        started, cancelled = notifications.events
        assert isinstance(started, ContextCompactionStartedEvent)
        assert isinstance(cancelled, ContextCompactionCancelledEvent)
        assert cleaned.is_set() and cancelled.compaction_id == started.compaction_id
        assert cancelled.strategies_attempted == ("history_trim", "rolling_summary")
        assert cancelled.strategies_completed == ("history_trim",)
        assert cancelled.duration_ms >= 0
        assert "after" not in cancelled.model_dump() and "error_message" not in cancelled.model_dump()

    asyncio.run(scenario())
