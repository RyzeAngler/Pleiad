from dataclasses import dataclass

from pydantic import Field

from pleiad.context.budget import BudgetStatus
from pleiad.context.selection.models import ContextView
from pleiad.contracts import ContractModel
from pleiad.messages.message import Message


class CompactionState(ContractModel):
    """Serializable request-local state for a rolling history summary."""

    summary: str = Field(min_length=1)
    through_sequence: int = Field(ge=1)
    replacements: dict[str, str] = Field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CompactionResult:
    """Reduced model history and the state needed to continue reducing it."""

    messages: tuple[Message, ...]
    context_view: ContextView
    state: CompactionState | None
    budget: BudgetStatus
