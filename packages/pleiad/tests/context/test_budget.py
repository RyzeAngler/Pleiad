import pytest

from pleiad.context.budget import BudgetGuard, ContextBudget, ContextCountInput
from pleiad.context.errors import ContextWindowError
from pleiad.context.selection.models import ContextView
from pleiad.messages.message import UserMessage


class FixedTokenCounter:
    """Return a deterministic request size for budget tests."""

    estimated = False

    def __init__(self, tokens: int) -> None:
        self.tokens = tokens

    def count(self, request: ContextCountInput) -> int:
        """Return the configured token count."""
        return self.tokens


def request() -> ContextCountInput:
    """Build one minimal provider-neutral request."""
    return ContextCountInput(
        instructions="instructions",
        messages=(UserMessage(content="hello"),),
        tools=(),
        context_view=ContextView(),
    )


def test_budget_guard_marks_compaction_before_the_hard_window() -> None:
    """The compaction threshold is distinct from the available input limit."""
    budget = ContextBudget(window_tokens=100, output_reserve=10, compact_at=50, recent_tokens=20)

    status = BudgetGuard(budget, FixedTokenCounter(50)).check(request())

    assert status.input_tokens == 50
    assert status.available_tokens == 90
    assert status.requires_compaction is True
    assert status.exceeds_window is False
    assert status.estimated is False


def test_budget_guard_rejects_required_input_that_cannot_fit() -> None:
    """An input beyond the reserved model window raises the Context business error."""
    budget = ContextBudget(window_tokens=100, output_reserve=10, compact_at=50, recent_tokens=20)

    with pytest.raises(ContextWindowError):
        BudgetGuard(budget, FixedTokenCounter(91)).ensure_fit(request())
