import asyncio
import json
import logging
from collections.abc import Awaitable, Callable

from pleiad.context.budget import BudgetGuard, BudgetStatus, ContextCountInput
from pleiad.context.compaction.models import CompactionResult, CompactionState
from pleiad.context.errors import ContextCompactionError, ContextWindowError
from pleiad.context.rendering import ContextRenderer
from pleiad.context.selection.models import ContextView
from pleiad.memory.turn import Turn
from pleiad.messages.message import Message, ToolMessage, UserMessage
from pleiad.messages.results import MAX_INLINE_TOOL_RESULT_CHARS, ToolResultReference
from pleiad.models.contracts import Tool

logger = logging.getLogger(__name__)

HistorySummarizer = Callable[[str | None, tuple[Turn, ...]], Awaitable[str]]


class ContextCompactor:
    """Reduce completed history only at complete Turn boundaries."""

    def __init__(self, budget: BudgetGuard, renderer: ContextRenderer | None = None) -> None:
        """Create a compactor with one explicit request budget."""
        self.budget = budget
        self.renderer = ContextRenderer() if renderer is None else renderer

    async def compact(
        self,
        turns: tuple[Turn, ...],
        *,
        instructions: str,
        user_message: UserMessage | None,
        tools: tuple[Tool, ...],
        context_view: ContextView,
        prior_state: CompactionState | None = None,
        summarizer: HistorySummarizer | None = None,
    ) -> CompactionResult:
        """Build a request-local history view and optional rolling summary."""
        if any(not turn.completed for turn in turns):
            raise ValueError("ContextCompactor accepts completed Turns only")
        selected_context = context_view
        full_messages, large_replacements = replace_large_tool_results(flatten_turns(turns))
        status = self.check(instructions, full_messages, tools, selected_context, user_message)
        original_context_count = len(selected_context.blocks)
        while status.exceeds_window and selected_context.blocks:
            selected_context = ContextView(selected_context.blocks[:-1])
            status = self.check(instructions, full_messages, tools, selected_context, user_message)
        if len(selected_context.blocks) < original_context_count:
            logger.warning(
                f"context blocks removed for budget removed={original_context_count - len(selected_context.blocks)}"
            )

        if not status.requires_compaction and not status.exceeds_window:
            return CompactionResult(full_messages, selected_context, prior_state, status)

        recent_turns, older_turns = self.select_recent_turns(
            turns,
            instructions=instructions,
            user_message=user_message,
            tools=tools,
            context_view=selected_context,
        )
        recent_messages = flatten_turns(recent_turns)
        replacements = dict(prior_state.replacements) if prior_state is not None else {}
        replacements.update(large_replacements)
        recent_messages, new_replacements = replace_old_tool_results(recent_messages, recent_turns)
        replacements.update(new_replacements)

        summary = prior_state.summary if prior_state is not None else None
        through_sequence = prior_state.through_sequence if prior_state is not None else 0
        if older_turns:
            previous_sequence = prior_state.through_sequence if prior_state is not None else 0
            new_older_turns = tuple(turn for turn in older_turns if turn.sequence > previous_sequence)
            if new_older_turns and summarizer is not None:
                try:
                    summary = await summarizer(summary, new_older_turns)
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    raise ContextCompactionError() from error
                if not summary.strip():
                    raise ContextCompactionError("The history summarizer returned empty content.")
                through_sequence = new_older_turns[-1].sequence
            elif new_older_turns and summary is not None:
                raise ContextWindowError("The rolling history summary needs an explicit summarizer to continue.")

        summary_instructions = add_summary(instructions, summary)
        status = self.check(summary_instructions, recent_messages, tools, selected_context, user_message)
        if status.exceeds_window:
            raise ContextWindowError(
                "The required instructions and recent complete Turn cannot fit the context window."
            )
        state = None
        if summary is not None:
            state = CompactionState(
                summary=summary,
                through_sequence=max(through_sequence, 1),
                replacements=replacements,
            )
        elif replacements:
            state = CompactionState(
                summary="The older history is unavailable in this request view.",
                through_sequence=max(through_sequence, 1),
                replacements=replacements,
            )
        logger.info(
            f"context compacted turns={len(turns)} retained_turns={len(recent_turns)} "
            f"through_sequence={through_sequence} recent_tokens={status.input_tokens}"
        )
        return CompactionResult(tuple(recent_messages), selected_context, state, status)

    def select_recent_turns(
        self,
        turns: tuple[Turn, ...],
        *,
        instructions: str,
        user_message: UserMessage | None,
        tools: tuple[Tool, ...],
        context_view: ContextView,
    ) -> tuple[tuple[Turn, ...], tuple[Turn, ...]]:
        """Select the newest complete Turns within the explicit recent budget."""
        selected: list[Turn] = []
        for turn in reversed(turns):
            candidate = (turn, *selected)
            status = self.check(
                instructions,
                flatten_turns(candidate),
                tools,
                context_view,
                user_message,
            )
            if not selected or status.input_tokens <= self.budget.budget.recent_tokens:
                selected.insert(0, turn)
            else:
                break
        selected_sequences = {turn.sequence for turn in selected}
        older = tuple(turn for turn in turns if turn.sequence not in selected_sequences)
        return tuple(selected), older

    def check(
        self,
        instructions: str,
        messages: tuple[Message, ...],
        tools: tuple[Tool, ...],
        context_view: ContextView,
        user_message: UserMessage | None,
    ) -> BudgetStatus:
        """Evaluate one complete request with a temporary User message."""
        request = ContextCountInput(
            instructions=instructions,
            messages=self.renderer.render_messages(
                replace_large_tool_results(messages)[0], user_message=user_message, context_view=context_view
            ),
            tools=tools,
            context_view=context_view,
        )
        return self.budget.check(request)


def flatten_turns(turns: tuple[Turn, ...]) -> tuple[Message, ...]:
    """Flatten complete Turns without changing their message order."""
    return tuple(message for turn in turns for message in turn.messages)


def replace_old_tool_results(
    messages: tuple[Message, ...],
    turns: tuple[Turn, ...],
) -> tuple[tuple[Message, ...], dict[str, str]]:
    """Replace only large Tool Result bodies from non-latest retained Turns."""
    if not turns:
        return messages, {}
    latest_sequence = turns[-1].sequence
    replacements: dict[str, str] = {}
    updated: list[Message] = []
    turn_by_message = {message.message_id: turn for turn in turns for message in turn.messages}
    for message in messages:
        turn = turn_by_message[message.message_id]
        if isinstance(message, ToolMessage) and (
            len(message.content) > MAX_INLINE_TOOL_RESULT_CHARS
            or (turn.sequence < latest_sequence and len(message.content) > 512)
        ):
            content = result_reference_content(message)
            replacements[message.message_id] = content
            updated.append(message.model_copy(update={"content": content}))
        else:
            updated.append(message)
    return tuple(updated), replacements


def result_reference_content(message: ToolMessage) -> str:
    """Render an executable, integrity-bearing reference to a retained observation."""
    reference = ToolResultReference.from_message(message)
    return json.dumps(
        {
            "compacted": True,
            **reference.model_dump(mode="json"),
            "read_tool_result": reference.read_arguments(),
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def replace_large_tool_results(messages: tuple[Message, ...]) -> tuple[tuple[Message, ...], dict[str, str]]:
    """Bound even the newest result in the request view while retaining its original."""
    replacements = {
        message.message_id: result_reference_content(message)
        for message in messages
        if isinstance(message, ToolMessage) and len(message.content) > MAX_INLINE_TOOL_RESULT_CHARS
    }
    return (
        tuple(
            message.model_copy(update={"content": replacements[message.message_id]})
            if message.message_id in replacements
            else message
            for message in messages
        ),
        replacements,
    )


def add_summary(instructions: str, summary: str | None) -> str:
    """Add a request-local historical summary without fabricating a history Message."""
    if summary is None:
        return instructions
    return f"{instructions}\n\nHistorical reference summary:\n{summary}"
