from dataclasses import dataclass

from pydantic import Field, model_validator

from pleiad.context.budget import BudgetStatus
from pleiad.context.selection.models import ContextView
from pleiad.contracts import ContractModel
from pleiad.messages.message import Message


class CompactionState(ContractModel):
    """Retain real optional summaries and executable original-result references."""

    summary: str | None = Field(default=None, min_length=1)
    through_sequence: int = Field(default=0, ge=0)
    replacements: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_summary_boundary(self) -> "CompactionState":
        """Record a covered history boundary only for an actual host summary."""
        if (self.summary is None) != (self.through_sequence == 0):
            raise ValueError("A history summary and its covered step boundary must be present together")
        return self


@dataclass(frozen=True, slots=True)
class CompactionResult:
    """Reduced model history and the state needed to continue reducing it."""

    messages: tuple[Message, ...]
    context_view: ContextView
    state: CompactionState | None
    budget: BudgetStatus
