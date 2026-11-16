from dataclasses import dataclass
from time import monotonic
from typing import Literal
from uuid import uuid4

from pleiad.context.budget import BudgetStatus
from pleiad.context.compaction.models import CompactionResult
from pleiad.context.selection.models import ContextView
from pleiad.errors.base import PleiadError
from pleiad.events.context import (
    CompactionContextBlock,
    CompactionStep,
    CompactionStrategy,
    CompactionSummary,
    CompactionTokens,
    CompactionTrigger,
    ContextCompactionCancelledEvent,
    ContextCompactionCompletedEvent,
    ContextCompactionFailedEvent,
    ContextCompactionStartedEvent,
)
from pleiad.events.contracts import Event, EventSink
from pleiad.sessions.model_step import ModelStep


@dataclass(frozen=True, slots=True)
class CompactionEventContext:
    """Bind request identity to the Runtime's existing durable event publisher.

    Args:
        run_id: Run preparing the current request.
        step_id: Active request Step, distinct from historical coverage references.
        agent_name: Agent preparing the request, including after a Handoff.
        event_sink: Publisher that commits each event before notifying observers.
        parent_run_id: Actual parent of a child Run, when present.
    """

    run_id: str
    step_id: str
    agent_name: str
    event_sink: EventSink
    parent_run_id: str | None = None


class CompactionObservation:
    """Collect real strategy stages without timing event delivery waits."""

    def __init__(self, context: CompactionEventContext | None, before: BudgetStatus, compact_at: int) -> None:
        """Prepare metadata only after an actual compaction action is selected."""
        self.context = context
        reasons: list[CompactionTrigger] = []
        if before.requires_compaction:
            reasons.append("compact_at_reached")
        if before.exceeds_window:
            reasons.append("context_window_exceeded")
        self.started = (
            None
            if context is None
            else ContextCompactionStartedEvent(
                run_id=context.run_id,
                step_id=context.step_id,
                agent_name=context.agent_name,
                parent_run_id=context.parent_run_id,
                compaction_id=uuid4().hex,
                trigger_reasons=tuple(reasons),
                compact_at=compact_at,
                available_tokens=before.available_tokens,
                before=CompactionTokens.from_budget(before),
            )
        )
        self.started_at: float | None = None
        self.attempted: list[CompactionStrategy] = []
        self.completed: list[CompactionStrategy] = []

    async def publish(self, event: Event) -> None:
        """Deliver through the supplied publisher without a separate event bus."""
        if self.context is not None:
            await self.context.event_sink(event)

    async def start(self) -> None:
        """Publish admission before starting the work clock."""
        if self.started is not None:
            await self.publish(self.started)
        self.started_at = monotonic()

    def duration_ms(self) -> float:
        """Return actual elapsed work, or zero if admission was interrupted."""
        return 0.0 if self.started_at is None else (monotonic() - self.started_at) * 1000

    def completion(
        self,
        result: CompactionResult,
        *,
        retained: tuple[ModelStep, ...],
        omitted: tuple[ModelStep, ...],
        summarized: tuple[ModelStep, ...],
        previous_sequence: int,
        original_context: ContextView,
        reference_changes: tuple[str, ...],
    ) -> ContextCompactionCompletedEvent | None:
        """Freeze confirmed output metadata before terminal event delivery."""
        if self.started is None:
            return None
        summary = None
        if result.state is not None and result.state.summary is not None:
            operation: Literal["created", "updated", "reused"] = "reused"
            if summarized:
                operation = "updated" if previous_sequence > 0 else "created"
            summary = CompactionSummary(
                operation=operation,
                previous_through_sequence=previous_sequence if previous_sequence > 0 else None,
                through_sequence=result.state.through_sequence,
                added_steps=step_references(summarized),
            )
        blocks = tuple(
            CompactionContextBlock(index=index, kind=block.kind, origin=block.origin)
            for index, block in enumerate(original_context.blocks)
        )
        retained_count = len(result.context_view.blocks)
        return ContextCompactionCompletedEvent(
            **self.started.model_dump(exclude={"kind", "event_id", "timestamp"}),
            duration_ms=self.duration_ms(),
            strategies_attempted=tuple(self.attempted),
            strategies_completed=tuple(self.completed),
            after=CompactionTokens.from_budget(result.budget),
            summary=summary,
            retained_steps=step_references(retained),
            omitted_steps=step_references(omitted),
            retained_context_blocks=blocks[:retained_count],
            removed_context_blocks=blocks[retained_count:],
            replaced_tool_result_ids=reference_changes,
        )

    async def cancel(self) -> None:
        """Publish cancellation using only already observed strategy stages."""
        if self.started is not None:
            await self.publish(
                ContextCompactionCancelledEvent(
                    **self.started.model_dump(exclude={"kind", "event_id", "timestamp"}),
                    duration_ms=self.duration_ms(),
                    strategies_attempted=tuple(self.attempted),
                    strategies_completed=tuple(self.completed),
                )
            )

    async def fail(self, error: Exception) -> None:
        """Publish classified errors without leaking arbitrary exception content."""
        if self.started is not None:
            await self.publish(
                ContextCompactionFailedEvent(
                    **self.started.model_dump(exclude={"kind", "event_id", "timestamp"}),
                    duration_ms=self.duration_ms(),
                    strategies_attempted=tuple(self.attempted),
                    strategies_completed=tuple(self.completed),
                    error_code=error.code if isinstance(error, PleiadError) else "context.compaction.failed",
                    error_message=error.message if isinstance(error, PleiadError) else "Context compaction failed.",
                )
            )


def step_references(steps: tuple[ModelStep, ...]) -> tuple[CompactionStep, ...]:
    """Describe exact, possibly discontinuous Step coverage without message content."""
    return tuple(CompactionStep(step_id=step.step_id, run_id=step.run_id, sequence=step.sequence) for step in steps)
