"""Complete-request token budgets for Context validation."""

import json
from dataclasses import dataclass
from typing import Protocol

from pleiad.llm.contracts import LLMRequest


class TokenCounter(Protocol):
    """Count tokens for a provider-neutral model request."""

    def count(self, request: LLMRequest) -> int:
        """Return an input-token estimate for one complete request."""


class ApproximateTokenCounter:
    """Portable fallback using serialized UTF-8 size."""

    def count(self, request: LLMRequest) -> int:
        """Estimate tokens without a provider tokenizer."""
        serialized = json.dumps(
            request.model_dump(mode="json"),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return max(1, (len(serialized) + 3) // 4)


@dataclass(frozen=True, slots=True)
class ContextBudgetPolicy:
    """High and low watermarks for one model request budget."""

    model_window_tokens: int = 128_000
    reserved_output_tokens: int = 8_192
    expected_growth_tokens: int = 2_048
    safety_margin_tokens: int = 1_024
    high_watermark: float = 0.85
    low_watermark: float = 0.55

    @property
    def usable_input_tokens(self) -> int:
        """Return input space after output and growth reservations."""
        return max(
            1,
            self.model_window_tokens
            - self.reserved_output_tokens
            - self.expected_growth_tokens
            - self.safety_margin_tokens,
        )


@dataclass(frozen=True, slots=True)
class BudgetReport:
    """Deterministic result of checking one complete model request."""

    estimated_tokens: int
    usable_input_tokens: int
    high_watermark_tokens: int
    low_watermark_tokens: int

    @property
    def requires_compaction(self) -> bool:
        """Return whether the request crossed the high watermark."""
        return self.estimated_tokens > self.high_watermark_tokens


class BudgetGuard:
    """Check complete model requests with an injectable token counter."""

    def __init__(
        self,
        policy: ContextBudgetPolicy | None = None,
        counter: TokenCounter | None = None,
    ) -> None:
        self.policy = policy or ContextBudgetPolicy()
        self.counter = counter or ApproximateTokenCounter()

    def check(self, request: LLMRequest) -> BudgetReport:
        """Return the current request budget without mutating it."""
        usable = self.policy.usable_input_tokens
        return BudgetReport(
            estimated_tokens=self.counter.count(request),
            usable_input_tokens=usable,
            high_watermark_tokens=int(usable * self.policy.high_watermark),
            low_watermark_tokens=int(usable * self.policy.low_watermark),
        )
