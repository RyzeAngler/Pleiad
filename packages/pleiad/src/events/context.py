from typing import Annotated, Literal

from pydantic import Field, model_validator

from pleiad.context.budget import BudgetStatus
from pleiad.contracts import ContractModel
from pleiad.events.runtime import ModelStepEvent

type CompactionTrigger = Literal["compact_at_reached", "context_window_exceeded"]
type CompactionStrategy = Literal["history_trim", "rolling_summary", "tool_result_reference", "context_block_trim"]


class CompactionTokens(ContractModel):
    """Describe the measured request input, including unresolved image costs."""

    input_tokens: int = Field(ge=0)
    estimated: bool
    image_tokens: int | None = Field(default=None, ge=0)
    unestimated_images: int = Field(ge=0)

    @classmethod
    def from_budget(cls, status: BudgetStatus) -> "CompactionTokens":
        """Copy actual budget measurements without substituting missing costs.

        Args:
            status: Facts measured by the request's configured budget guard.

        Returns:
            Serializable token facts with the counter's original estimation flag.
        """
        return cls(
            input_tokens=status.input_tokens,
            estimated=status.estimated,
            image_tokens=status.image_tokens,
            unestimated_images=status.unestimated_images,
        )


class CompactionStep(ContractModel):
    """Identify a historical Step using its Session sequence and original Run."""

    step_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    sequence: int = Field(ge=1)


class CompactionContextBlock(ContractModel):
    """Identify one block by its position in the original ordered ContextView."""

    index: int = Field(ge=0)
    kind: str = Field(min_length=1)
    origin: str = Field(min_length=1)


class CompactionSummary(ContractModel):
    """Expose real summary boundaries without claiming unknown prior coverage.

    The cursor is a high-water mark, not a contiguous coverage interval.
    Only added_steps describes exact coverage established by this attempt.
    """

    operation: Literal["created", "updated", "reused"]
    previous_through_sequence: int | None = Field(default=None, ge=1)
    through_sequence: int = Field(ge=1)
    added_steps: tuple[CompactionStep, ...]


class ContextCompactionEvent(ModelStepEvent):
    """Correlate an actual compaction attempt with its request and trigger."""

    compaction_id: str = Field(min_length=1)
    parent_run_id: str | None = Field(default=None, min_length=1)
    trigger_reasons: tuple[CompactionTrigger, ...] = Field(min_length=1)
    compact_at: int = Field(gt=0)
    available_tokens: int = Field(gt=0)
    before: CompactionTokens


class ContextCompactionStartedEvent(ContextCompactionEvent):
    """Report admission immediately before actual compaction work begins."""

    kind: Literal["context.compaction.started"] = "context.compaction.started"


class ContextCompactionTerminalEvent(ContextCompactionEvent):
    """Report measured work time and confirmed strategy stages for an attempt."""

    duration_ms: float = Field(ge=0, allow_inf_nan=False)
    strategies_attempted: tuple[CompactionStrategy, ...]
    strategies_completed: tuple[CompactionStrategy, ...]

    @model_validator(mode="after")
    def validate_strategies(self) -> "ContextCompactionTerminalEvent":
        """Require every completed stage to have been attempted exactly once."""
        if len(set(self.strategies_attempted)) != len(self.strategies_attempted):
            raise ValueError("Compaction strategies must not be repeated")
        if len(set(self.strategies_completed)) != len(self.strategies_completed):
            raise ValueError("Completed compaction strategies must not be repeated")
        if not set(self.strategies_completed) <= set(self.strategies_attempted):
            raise ValueError("Completed compaction strategies must have been attempted")
        return self


class ContextCompactionCompletedEvent(ContextCompactionTerminalEvent):
    """Describe the final request view after successful compaction."""

    kind: Literal["context.compaction.completed"] = "context.compaction.completed"
    after: CompactionTokens
    summary: CompactionSummary | None
    retained_steps: tuple[CompactionStep, ...]
    omitted_steps: tuple[CompactionStep, ...]
    retained_context_blocks: tuple[CompactionContextBlock, ...]
    removed_context_blocks: tuple[CompactionContextBlock, ...]
    replaced_tool_result_ids: tuple[str, ...]


class ContextCompactionFailedEvent(ContextCompactionTerminalEvent):
    """Report a failed attempt without inventing a final compacted request."""

    kind: Literal["context.compaction.failed"] = "context.compaction.failed"
    error_code: str = Field(min_length=1)
    error_message: str = Field(min_length=1)


class ContextCompactionCancelledEvent(ContextCompactionTerminalEvent):
    """Report interrupted work before propagating owner or deadline cancellation."""

    kind: Literal["context.compaction.cancelled"] = "context.compaction.cancelled"


ContextCompactionLifecycleEvent = Annotated[
    ContextCompactionStartedEvent
    | ContextCompactionCompletedEvent
    | ContextCompactionFailedEvent
    | ContextCompactionCancelledEvent,
    Field(discriminator="kind"),
]
