import asyncio
import json
import logging
from collections.abc import Awaitable, Callable

from pleiad.context.budget import BudgetGuard, BudgetStatus, ContextCountInput
from pleiad.context.compaction.models import CompactionResult, CompactionState
from pleiad.context.compaction.observation import CompactionEventContext, CompactionObservation
from pleiad.context.errors import ContextCompactionError, ContextWindowError
from pleiad.context.rendering import ContextRenderer
from pleiad.context.selection.models import ContextView
from pleiad.messages.images import ImageBlock
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
        event_context: CompactionEventContext | None = None,
    ) -> CompactionResult:
        """Build a request-local history view and optional rolling summary.

        Args:
            steps: Ordered, completed Session history; originals remain unchanged.
            instructions: Required instructions before adding a rolling summary.
            input_messages: Current inputs that must remain in the request.
            tools: Tools declared for the current model request.
            context_view: Ordered optional request-local reference blocks.
            prior_state: Existing host summary and original-result references.
            summarizer: Explicit updater for newly omitted history.
            event_context: Current request identity and its durable event publisher.
                Checks without actual reductions never publish lifecycle events.

        Returns:
            The reduced request view, real summary state, and final budget facts.

        Raises:
            ValueError: If history contains an incomplete Step.
            ContextCompactionError: If the supplied summarizer fails or returns empty content.
            ContextWindowError: If required content cannot fit or a summary updater is missing.
            asyncio.CancelledError: After recording cancellation of admitted compaction work.
        """
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
        summary = prior_state.summary if prior_state is not None else None
        previous_sequence = prior_state.through_sequence if prior_state is not None else 0
        through_sequence = previous_sequence
        new_older_steps = tuple(step for step in older_steps if step.sequence > previous_sequence)
        reference_changes = tuple(
            message_id for message_id in old_tool_result_ids(recent_steps) if message_id not in large_replacements
        )
        has_actions = bool(older_steps or reference_changes)
        if not has_actions:
            candidate_status = self.check(
                add_summary(instructions, summary), recent_messages, tools, selected_context, input_messages
            )
            has_actions = candidate_status.exceeds_window and bool(selected_context.blocks)
        observation = CompactionObservation(
            event_context if has_actions else None, status, self.budget.budget.compact_at
        )
        try:
            await observation.start()
        except asyncio.CancelledError:
            await observation.cancel()
            raise

        try:
            if older_steps:
                observation.attempted.append("history_trim")
                observation.completed.append("history_trim")
            if reference_changes:
                observation.attempted.append("tool_result_reference")
            recent_messages, new_replacements = replace_old_tool_results(recent_messages, recent_steps)
            replacements = dict(prior_state.replacements) if prior_state is not None else {}
            replacements.update(large_replacements)
            replacements.update(new_replacements)
            if reference_changes:
                observation.completed.append("tool_result_reference")
            summarized_steps: tuple[ModelStep, ...] = ()
            if new_older_steps and summarizer is not None:
                observation.attempted.append("rolling_summary")
                try:
                    summary = await summarizer(summary, new_older_steps)
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    raise ContextCompactionError() from error
                if not summary.strip():
                    raise ContextCompactionError("The history summarizer returned empty content.")
                through_sequence = new_older_steps[-1].sequence
                summarized_steps = new_older_steps
                observation.completed.append("rolling_summary")
            elif new_older_steps and summary is not None:
                raise ContextWindowError("The rolling history summary needs an explicit summarizer to continue.")

            summary_instructions = add_summary(instructions, summary)
            status = self.check(summary_instructions, recent_messages, tools, selected_context, input_messages)
            if status.exceeds_window and selected_context.blocks:
                observation.attempted.append("context_block_trim")
            while status.exceeds_window and selected_context.blocks:
                selected_context = ContextView(selected_context.blocks[:-1])
                status = self.check(summary_instructions, recent_messages, tools, selected_context, input_messages)
            if len(selected_context.blocks) < original_context_count:
                observation.completed.append("context_block_trim")
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
                state = CompactionState(replacements=replacements)
            result = CompactionResult(tuple(recent_messages), selected_context, state, status)
            completed = observation.completion(
                result,
                retained=recent_steps,
                omitted=older_steps,
                summarized=summarized_steps,
                previous_sequence=previous_sequence,
                original_context=context_view,
                reference_changes=reference_changes,
            )
            logger.info(
                f"context compacted steps={len(steps)} retained_steps={len(recent_steps)} "
                f"through_sequence={through_sequence} recent_tokens={status.input_tokens}"
            )
        except asyncio.CancelledError:
            await observation.cancel()
            raise
        except Exception as error:
            await observation.fail(error)
            raise
        # Terminal delivery is outside the work scope: observer cancellation cannot change its outcome.
        if completed is not None:
            await observation.publish(completed)
        return result

    def select_recent_steps(
        self,
        steps: tuple[ModelStep, ...],
        *,
        instructions: str,
        input_messages: tuple[UserMessage, ...],
        tools: tuple[Tool, ...],
        context_view: ContextView,
    ) -> tuple[tuple[ModelStep, ...], tuple[ModelStep, ...]]:
        """Retain image steps and select recent complete steps within their shared budget."""
        selected_sequences = {step.sequence for step in steps if step_has_images(step)}
        for index, step in enumerate(reversed(steps)):
            if step.sequence in selected_sequences:
                continue
            candidate_sequences = selected_sequences | {step.sequence}
            candidate = tuple(value for value in steps if value.sequence in candidate_sequences)
            status = self.check(
                instructions,
                flatten_steps(candidate),
                tools,
                context_view,
                input_messages,
            )
            if index == 0 or status.input_tokens <= self.budget.budget.recent_tokens:
                selected_sequences.add(step.sequence)
            else:
                break
        retained = tuple(step for step in steps if step.sequence in selected_sequences)
        # Pinned image steps stay visible while every omitted complete step enters the summary.
        older = tuple(step for step in steps if step.sequence not in selected_sequences)
        return retained, older

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


def step_has_images(step: ModelStep) -> bool:
    """Return whether a complete step contains an image that must remain visible."""
    return any(
        isinstance(block, ImageBlock)
        for message in step.messages
        if isinstance(message, UserMessage) and not isinstance(message.content, str)
        for block in message.content
    )


def replace_old_tool_results(
    messages: tuple[Message, ...],
    steps: tuple[ModelStep, ...],
) -> tuple[tuple[Message, ...], dict[str, str]]:
    """Replace only large Tool Result bodies from non-latest retained ModelSteps."""
    candidates = set(old_tool_result_ids(steps))
    replacements: dict[str, str] = {}
    updated: list[Message] = []
    for message in messages:
        if isinstance(message, ToolMessage) and message.message_id in candidates:
            content = result_reference_content(message)
            replacements[message.message_id] = content
            updated.append(message.model_copy(update={"content": content}))
        else:
            updated.append(message)
    return tuple(updated), replacements


def old_tool_result_ids(steps: tuple[ModelStep, ...]) -> tuple[str, ...]:
    """Select reference candidates without constructing replacements during budget checks."""
    if not steps:
        return ()
    latest_sequence = steps[-1].sequence
    return tuple(
        message.message_id
        for step in steps
        for message in step.messages
        if isinstance(message, ToolMessage)
        and (
            len(message.content) > MAX_INLINE_TOOL_RESULT_CHARS
            or (step.sequence < latest_sequence and len(message.content) > 512)
        )
    )


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
