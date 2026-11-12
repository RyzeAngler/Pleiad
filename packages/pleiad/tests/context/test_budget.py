import pytest

from pleiad.context.budget import (
    ApproximateTokenCounter,
    BudgetGuard,
    ContextBudget,
    ContextCountInput,
    ImageTokenEstimateError,
)
from pleiad.context.errors import ContextWindowError
from pleiad.context.selection.models import ContextView
from pleiad.messages.images import ImageBlock, ImageReference
from pleiad.messages.message import TextBlock, UserMessage


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


def image_request(*, reference: str = "asset:product", copies: int = 1) -> ContextCountInput:
    image = ImageBlock(
        image=ImageReference(reference=reference, sha256="a" * 64, mime="image/png", width=100, height=200)
    )
    return ContextCountInput(
        instructions="rules",
        messages=(UserMessage(message_id="input", content=(TextBlock(text="inspect"), *((image,) * copies))),),
        tools=(),
        context_view=ContextView(),
    )


def test_image_budget_counts_duplicates() -> None:
    counter = ApproximateTokenCounter(lambda image: image.width * image.height // 100)
    one = counter.estimate(image_request())
    repeated = counter.estimate(image_request(copies=2))

    assert one.image_tokens == 200
    assert repeated.image_tokens == 400
    assert repeated.input_tokens - one.input_tokens == 200
    assert repeated.unestimated_images == 0


def test_image_budget_excludes_reference() -> None:
    counter = ApproximateTokenCounter(lambda image: 120)

    assert counter.estimate(image_request(reference="asset:a")) == counter.estimate(
        image_request(reference="asset:" + "a" * 500)
    )


def test_image_budget_requires_estimate() -> None:
    with pytest.raises(ImageTokenEstimateError) as raised:
        ApproximateTokenCounter().count(image_request(copies=2))

    assert raised.value.code == "context.image_tokens.unknown"
    assert raised.value.unestimated_images == 2


def test_image_budget_allows_unknown() -> None:
    budget = ContextBudget(window_tokens=1_000, output_reserve=100, compact_at=800, recent_tokens=400)
    counter = ApproximateTokenCounter(lambda image: None, allow_unestimated_images=True)

    status = BudgetGuard(budget, counter).check(image_request(copies=2))

    assert status.estimated
    assert status.image_tokens is None
    assert status.unestimated_images == 2
    assert status.input_tokens > 0


def test_image_budget_partial_estimates() -> None:
    known = image_request(reference="asset:known").messages[0]
    unknown = image_request(reference="asset:unknown").messages[0]
    combined = ContextCountInput(instructions="rules", messages=(known, unknown), tools=(), context_view=ContextView())
    counter = ApproximateTokenCounter(
        lambda image: 120 if image.reference == "asset:known" else None, allow_unestimated_images=True
    )
    unestimated = ApproximateTokenCounter(allow_unestimated_images=True).estimate(combined)

    estimate = counter.estimate(combined)

    assert estimate.input_tokens == unestimated.input_tokens + 120
    assert estimate.image_tokens is None
    assert estimate.unestimated_images == 1


@pytest.mark.parametrize("tokens", [-1, True])
def test_image_budget_rejects_bad_estimate(tokens: int) -> None:
    with pytest.raises(ValueError, match="non-negative integer"):
        ApproximateTokenCounter(lambda image: tokens).count(image_request())


def test_image_budget_reports_known_cost() -> None:
    budget = ContextBudget(window_tokens=1_000, output_reserve=100, compact_at=800, recent_tokens=400)

    status = BudgetGuard(budget, ApproximateTokenCounter(lambda image: 120)).check(image_request())

    assert status.image_tokens == 120
    assert status.input_tokens > status.image_tokens
    assert status.unestimated_images == 0


def test_text_budget_needs_no_estimator() -> None:
    estimate = ApproximateTokenCounter().estimate(request())

    assert estimate.input_tokens > 0
    assert estimate.image_tokens == 0
    assert estimate.unestimated_images == 0
