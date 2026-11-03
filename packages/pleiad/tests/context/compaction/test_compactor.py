import asyncio
from pathlib import Path

import pytest
from pydantic import TypeAdapter

from pleiad.context.budget import ApproximateTokenCounter, BudgetGuard, ContextBudget, ContextCountInput
from pleiad.context.compaction.compactor import ContextCompactor
from pleiad.context.compaction.models import CompactionState
from pleiad.context.rendering import ContextRenderer
from pleiad.context.selection.models import ContextBlock, ContextView
from pleiad.contracts import JsonObject
from pleiad.memory.short_memory import ShortMemory
from pleiad.memory.turn import Turn
from pleiad.messages.message import AssistantMessage, ToolCall, ToolMessage, UserMessage
from pleiad.messages.results import ToolResultReference
from pleiad.runtime.execution.read_tool_result import ReadToolResultTool
from pleiad.tools.contracts import ToolCall as ExecutionToolCall
from pleiad.tools.contracts import ToolContext, ToolResult
from pleiad.tools.runtime import ToolRuntime


class MessageCountCounter:
    """Count Messages to make Turn-boundary compaction deterministic."""

    estimated = False

    def count(self, request: ContextCountInput) -> int:
        """Return the number of request messages."""
        return len(request.messages)


def completed_turn(sequence: int) -> Turn:
    """Create one complete direct-answer Turn."""
    return Turn(
        turn_id=f"turn-{sequence}",
        sequence=sequence,
        agent_id="assistant",
        completed=True,
        messages=(
            UserMessage(message_id=f"user-{sequence}", content=f"question-{sequence}"),
            AssistantMessage(message_id=f"assistant-{sequence}", content=f"answer-{sequence}"),
        ),
    )


async def summarize(previous: str | None, turns: tuple[Turn, ...]) -> str:
    """Return a deterministic summary of omitted Turn sequences."""
    sequence_text = ",".join(str(turn.sequence) for turn in turns)
    return f"omitted={sequence_text} previous={previous or 'none'}"


def test_compactor_discards_only_whole_old_turns() -> None:
    """Compaction retains a complete recent Turn and records the omitted boundary."""
    budget = ContextBudget(window_tokens=20, output_reserve=2, compact_at=5, recent_tokens=3)
    compactor = ContextCompactor(BudgetGuard(budget, MessageCountCounter()))

    result = asyncio.run(
        compactor.compact(
            (completed_turn(1), completed_turn(2), completed_turn(3)),
            instructions="rules",
            user_message=None,
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
            (), instructions="rules", user_message=user, tools=(), context_view=view
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
@pytest.mark.parametrize("force_compaction", [False, True], ids=["within-budget", "compact-latest-turn"])
def test_large_tool_observation_can_be_read_in_pages_after_compaction(
    tmp_path: Path, persisted: bool, force_compaction: bool
) -> None:
    async def scenario() -> None:
        database = tmp_path / "memory.sqlite3"
        memory = ShortMemory.sqlite("chat", database) if persisted else ShortMemory.in_memory("chat")
        original = ToolMessage(message_id="original-result", tool_call_id="lookup", content="事实证据" * 30_000)
        turn = await memory.begin("agent", (UserMessage(content="Find the original evidence"),))
        await memory.complete(
            turn.turn_id,
            (
                AssistantMessage(tool_calls=(ToolCall(id="lookup", name="lookup", arguments="{}"),)),
                original,
            ),
        )
        if persisted:
            memory = ShortMemory.sqlite("chat", database)
        budget = ContextBudget(
            window_tokens=32_000,
            output_reserve=2_000,
            compact_at=2 if force_compaction else 24_000,
            recent_tokens=1 if force_compaction else 16_000,
        )
        counter = MessageCountCounter() if force_compaction else ApproximateTokenCounter()
        compacted = await ContextCompactor(BudgetGuard(budget, counter)).compact(
            await memory.read(), instructions="rules", user_message=None, tools=(), context_view=ContextView()
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
        source = ShortMemory.sqlite("source", database)
        turn = await source.begin("agent", (UserMessage(message_id="user-message", content="private request"),))
        await source.complete(
            turn.turn_id,
            (
                AssistantMessage(tool_calls=(ToolCall(id="private-call", name="lookup", arguments="{}"),)),
                ToolMessage(message_id="private-result", tool_call_id="private-call", content="private evidence"),
            ),
        )
        for memory, message_id in (
            (ShortMemory.sqlite("other", database), "private-result"),
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


def test_result_references_remain_discoverable_after_their_whole_turn_is_omitted() -> None:
    async def scenario() -> None:
        memory = ShortMemory.in_memory("omitted-tool-turn")
        first = await memory.begin("agent", (UserMessage(content="original task"),))
        original = ToolMessage(message_id="omitted-result", tool_call_id="call", content="original facts" * 100)
        await memory.complete(
            first.turn_id,
            (AssistantMessage(tool_calls=(ToolCall(id="call", name="lookup", arguments="{}"),)), original),
        )
        second = await memory.begin("agent", (UserMessage(content="next task"),))
        await memory.complete(second.turn_id, (AssistantMessage(content="answer"),))
        budget = ContextBudget(window_tokens=20, output_reserve=2, compact_at=4, recent_tokens=2)
        compacted = await ContextCompactor(BudgetGuard(budget, MessageCountCounter())).compact(
            await memory.read(), instructions="rules", user_message=None, tools=(), context_view=ContextView()
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
    turns = (completed_turn(1),)
    view = ContextView((ContextBlock(kind="knowledge", origin="document", content="large-reference " * 1_000),))
    result = asyncio.run(
        ContextCompactor(BudgetGuard(budget)).compact(
            turns, instructions="rules", user_message=None, tools=(), context_view=view
        )
    )

    assert result.context_view.blocks == ()
    assert result.messages == turns[0].messages
    wire_messages = ContextRenderer().render_messages(
        result.messages, user_message=None, context_view=result.context_view
    )
    wire_count = ApproximateTokenCounter().count(
        ContextCountInput(instructions="rules", messages=wire_messages, tools=(), context_view=ContextView())
    )
    assert result.budget.input_tokens == wire_count
