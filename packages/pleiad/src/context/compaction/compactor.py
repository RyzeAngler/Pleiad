import asyncio
import json
import logging
from collections.abc import Awaitable, Callable

from pleiad.context.budget import BudgetGuard, BudgetStatus, ContextCountInput
from pleiad.context.compaction.models import CompactionResult, CompactionState
from pleiad.context.errors import ContextCompactionError, ContextWindowError
from pleiad.context.rendering import ContextRenderer
from pleiad.context.selection.models import ContextView
from pleiad.messages.message import Message, ToolMessage, UserMessage
from pleiad.messages.results import MAX_INLINE_TOOL_RESULT_CHARS, ToolResultReference
from pleiad.models.contracts import Tool
from pleiad.sessions.model_step import ModelStep

logger = logging.getLogger(__name__)

HistorySummarizer = Callable[[str | None, tuple[ModelStep, ...]], Awaitable[str]]


class ContextCompactor:
    """Reduce completed history only at complete ModelStep boundaries."""

    def __init__(self, budget: BudgetGuard, renderer: ContextRenderer | None = None) -> None:
        """Create a compactor with one explicit request budget."""
        self.budget = budget
        self.renderer = ContextRenderer() if renderer is None else renderer

    async def compact(
        self,
        steps: tuple[ModelStep, ...],
        *,
        instructions: str,
        input_messages: tuple[UserMessage, ...],
        tools: tuple[Tool, ...],
        context_view: ContextView,
        prior_state: CompactionState | None = None,
        summarizer: HistorySummarizer | None = None,
    ) -> CompactionResult:
        """Build a request-local history view and optional rolling summary."""
        if any(not step.completed for step in steps):
            raise ValueError("ContextCompactor accepts completed ModelSteps only")
        selected_context = context_view
        full_messages, large_replacements = replace_large_tool_results(flatten_steps(steps))
        status = self.check(instructions, full_messages, tools, selected_context, input_messages)
        original_context_count = len(selected_context.blocks)
        if not status.requires_compaction and not status.exceeds_window:
            return CompactionResult(full_messages, selected_context, prior_state, status)

        recent_steps, older_steps = self.select_recent_steps(
            steps,
            instructions=instructions,
            input_messages=input_messages,
            tools=tools,
            context_view=selected_context,
        )
        recent_messages = flatten_steps(recent_steps)
        replacements = dict(prior_state.replacements) if prior_state is not None else {}
        replacements.update(large_replacements)
        recent_messages, new_replacements = replace_old_tool_results(recent_messages, recent_steps)
        replacements.update(new_replacements)

        summary = prior_state.summary if prior_state is not None else None
        through_sequence = prior_state.through_sequence if prior_state is not None else 0
        if older_steps:
            previous_sequence = prior_state.through_sequence if prior_state is not None else 0
            new_older_steps = tuple(step for step in older_steps if step.sequence > previous_sequence)
            if new_older_steps and summarizer is not None:
                try:
                    summary = await summarizer(summary, new_older_steps)
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    raise ContextCompactionError() from error
                if not summary.strip():
                    raise ContextCompactionError("The history summarizer returned empty content.")
                through_sequence = new_older_steps[-1].sequence
            elif new_older_steps and summary is not None:
                raise ContextWindowError("The rolling history summary needs an explicit summarizer to continue.")

        summary_instructions = add_summary(instructions, summary)
        status = self.check(summary_instructions, recent_messages, tools, selected_context, input_messages)
        while status.exceeds_window and selected_context.blocks:
            selected_context = ContextView(selected_context.blocks[:-1])
            status = self.check(summary_instructions, recent_messages, tools, selected_context, input_messages)
        if len(selected_context.blocks) < original_context_count:
            logger.warning(
                f"context blocks removed for budget removed={original_context_count - len(selected_context.blocks)}"
            )
        if status.exceeds_window:
            raise ContextWindowError(
                "The required instructions and recent complete ModelStep cannot fit the context window."
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
                replacements=replacements,
            )
        logger.info(
            f"context compacted steps={len(steps)} retained_steps={len(recent_steps)} "
            f"through_sequence={through_sequence} recent_tokens={status.input_tokens}"
        )
        return CompactionResult(tuple(recent_messages), selected_context, state, status)

    def select_recent_steps(
        self,
        steps: tuple[ModelStep, ...],
        *,
        instructions: str,
        input_messages: tuple[UserMessage, ...],
        tools: tuple[Tool, ...],
        context_view: ContextView,
    ) -> tuple[tuple[ModelStep, ...], tuple[ModelStep, ...]]:
        """Select the newest complete ModelSteps within the explicit recent budget."""
        selected: list[ModelStep] = []
        for step in reversed(steps):
            candidate = (step, *selected)
            status = self.check(
                instructions,
                flatten_steps(candidate),
                tools,
                context_view,
                input_messages,
            )
            if not selected or status.input_tokens <= self.budget.budget.recent_tokens:
                selected.insert(0, step)
            else:
                break
        selected_sequences = {step.sequence for step in selected}
        older = tuple(step for step in steps if step.sequence not in selected_sequences)
        return tuple(selected), older

    def check(
        self,
        instructions: str,
        messages: tuple[Message, ...],
        tools: tuple[Tool, ...],
        context_view: ContextView,
        input_messages: tuple[UserMessage, ...],
    ) -> BudgetStatus:
        """Evaluate one complete request with a temporary User message."""
        request = ContextCountInput(
            instructions=instructions,
            messages=self.renderer.render_messages(
                replace_large_tool_results(messages)[0], input_messages=input_messages, context_view=context_view
            ),
            tools=tools,
            context_view=context_view,
        )
        return self.budget.check(request)


def flatten_steps(steps: tuple[ModelStep, ...]) -> tuple[Message, ...]:
    """Flatten complete ModelSteps without changing their message order."""
    return tuple(message for step in steps for message in step.messages)


def replace_old_tool_results(
    messages: tuple[Message, ...],
    steps: tuple[ModelStep, ...],
) -> tuple[tuple[Message, ...], dict[str, str]]:
    """Replace only large Tool Result bodies from non-latest retained ModelSteps."""
    if not steps:
        return messages, {}
    latest_sequence = steps[-1].sequence
    replacements: dict[str, str] = {}
    updated: list[Message] = []
    step_by_message = {message.message_id: step for step in steps for message in step.messages}
    for message in messages:
        step = step_by_message[message.message_id]
        if isinstance(message, ToolMessage) and (
            len(message.content) > MAX_INLINE_TOOL_RESULT_CHARS
            or (step.sequence < latest_sequence and len(message.content) > 512)
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
