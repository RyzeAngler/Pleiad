import asyncio

from pleiad.context.budget import ApproximateTokenCounter, BudgetGuard, ContextBudget, ContextCountInput
from pleiad.context.compaction.compactor import ContextCompactor
from pleiad.context.compaction.models import CompactionState
from pleiad.context.rendering import ContextRenderer
from pleiad.context.selection.models import ContextBlock, ContextView
from pleiad.memory.turn import Turn
from pleiad.messages.message import AssistantMessage, UserMessage


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
