import json
import logging
from dataclasses import dataclass
from typing import Protocol

from pydantic import ConfigDict, Field, model_validator

from pleiad.context.errors import ContextWindowError
from pleiad.context.selection.models import ContextView
from pleiad.contracts import ContractModel
from pleiad.messages.message import Message
from pleiad.models.contracts import Tool

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ContextCountInput:
    """Provider-neutral values needed to estimate one complete model request."""

    instructions: str
    messages: tuple[Message, ...]
    tools: tuple[Tool, ...]
    context_view: ContextView


class TokenCounter(Protocol):
    """Count the input tokens for a provider-neutral model request."""

    estimated: bool

    def count(self, request: ContextCountInput) -> int:
        """Return a non-negative token count."""


class ApproximateTokenCounter:
    """Provide a deterministic request-size estimate without a provider SDK."""

    estimated = True

    def count(self, request: ContextCountInput) -> int:
        """Estimate tokens from canonical JSON size."""
        payload = {
            "instructions": request.instructions,
            "messages": [message.model_dump(mode="json") for message in request.messages],
            "tools": [tool.model_dump(mode="json") for tool in request.tools],
        }
        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return max(1, (len(serialized) + 3) // 4)


class ContextBudget(ContractModel):
    """Explicit model-window and history-compaction limits."""

    model_config = ConfigDict(strict=True)

    window_tokens: int = Field(gt=0)
    output_reserve: int = Field(gt=0)
    compact_at: int = Field(gt=0)
    recent_tokens: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_limits(self) -> "ContextBudget":
        """Ensure all limits leave room for the reserved model output."""
        if self.output_reserve >= self.window_tokens:
            raise ValueError("output_reserve must be smaller than window_tokens")
        if self.compact_at >= self.window_tokens - self.output_reserve:
            raise ValueError("compact_at must be below the available input window")
        if self.recent_tokens >= self.compact_at:
            raise ValueError("recent_tokens must be smaller than compact_at")
        return self

    @property
    def available_tokens(self) -> int:
        """Return the input capacity after reserving output tokens."""
        return self.window_tokens - self.output_reserve


@dataclass(frozen=True, slots=True)
class BudgetStatus:
    """Result of evaluating one complete request against a ContextBudget."""

    input_tokens: int
    available_tokens: int
    requires_compaction: bool
    exceeds_window: bool
    estimated: bool


class BudgetGuard:
    """Evaluate request-local context budgets without mutating their inputs."""

    def __init__(self, budget: ContextBudget, counter: TokenCounter | None = None) -> None:
        """Create a guard for one explicit budget and token counter."""
        self.budget = budget
        self.counter = ApproximateTokenCounter() if counter is None else counter

    def check(self, request: ContextCountInput) -> BudgetStatus:
        """Return token and compaction facts for a complete request."""
        input_tokens = self.counter.count(request)
        if input_tokens < 0:
            raise ValueError("token counters must return non-negative values")
        status = BudgetStatus(
            input_tokens=input_tokens,
            available_tokens=self.budget.available_tokens,
            requires_compaction=input_tokens >= self.budget.compact_at,
            exceeds_window=input_tokens > self.budget.available_tokens,
            estimated=self.counter.estimated,
        )
        logger.debug(
            f"context budget input_tokens={status.input_tokens} available_tokens={status.available_tokens} "
            f"estimated={status.estimated} requires_compaction={status.requires_compaction}"
        )
        return status

    def ensure_fit(self, request: ContextCountInput) -> BudgetStatus:
        """Return the status or raise when required request content cannot fit."""
        status = self.check(request)
        if status.exceeds_window:
            raise ContextWindowError("The required model input exceeds the available context window.")
        return status
