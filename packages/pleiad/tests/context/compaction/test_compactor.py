import asyncio
import hashlib
from pathlib import Path

import pytest
from pydantic import TypeAdapter

from pleiad.context.budget import ApproximateTokenCounter, BudgetGuard, ContextBudget, ContextCountInput
from pleiad.context.compaction.compactor import ContextCompactor
from pleiad.context.compaction.models import CompactionState
from pleiad.context.errors import ContextWindowError
from pleiad.context.rendering import ContextRenderer
from pleiad.context.selection.models import ContextBlock, ContextView
from pleiad.contracts import JsonObject
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

    result = asyncio.run(
        compactor.compact(
            (completed_step(1), completed_step(2), completed_step(3)),
            instructions="rules",
            input_messages=(),
            tools=(),
            context_view=ContextView(),
            summarizer=summarize,
        ),
    )

    assert [message.message_id for message in result.messages] == ["user-3", "assistant-3"]
    assert result.state == CompactionState(
        summary="omitted=1,2 previous=none",
        through_sequence=2,
    )


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
        compacted = await ContextCompactor(BudgetGuard(budget, counter)).compact(
            await memory.read(), instructions="rules", input_messages=(), tools=(), context_view=ContextView()
        )
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


def test_continuation_budget_counts_and_prunes_references_beside_history_user() -> None:
    """A continuation request counts its references before selecting a budget view."""
    budget = ContextBudget(window_tokens=1_000, output_reserve=100, compact_at=800, recent_tokens=500)
    steps = (completed_step(1),)
    view = ContextView((ContextBlock(kind="knowledge", origin="document", content="large-reference " * 1_000),))
    result = asyncio.run(
        ContextCompactor(BudgetGuard(budget)).compact(
            steps, instructions="rules", input_messages=(), tools=(), context_view=view
        )
    )

    assert result.context_view.blocks == ()
    assert result.messages == steps[0].messages
    wire_messages = ContextRenderer().render_messages(
        result.messages, input_messages=(), context_view=result.context_view
    )
    wire_count = ApproximateTokenCounter().count(
        ContextCountInput(instructions="rules", messages=wire_messages, tools=(), context_view=ContextView())
    )
    assert result.budget.input_tokens == wire_count


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
