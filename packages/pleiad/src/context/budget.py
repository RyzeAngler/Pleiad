import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from pydantic import ConfigDict, Field, model_validator

from pleiad.context.errors import ContextWindowError
from pleiad.context.selection.models import ContextView
from pleiad.contracts import ContractModel
from pleiad.errors.base import BusinessError
from pleiad.messages.images import ImageBlock, ImageReference
from pleiad.messages.message import Message, UserMessage
from pleiad.models.contracts import Tool

logger = logging.getLogger(__name__)

ImageTokenEstimator = Callable[[ImageReference], int | None]


class ImageTokenEstimateError(BusinessError):
    """Report an image whose model-specific token cost is unknown."""

    def __init__(self, unestimated_images: int) -> None:
        """Record the unresolved count without exposing image identities."""
        self.unestimated_images = unestimated_images
        super().__init__(
            "context.image_tokens.unknown",
            "Image token estimates are unavailable. "
            "Configure a model-specific estimator or explicitly allow unknown costs.",
        )


@dataclass(frozen=True, slots=True)
class TokenEstimate:
    """Separate counted input tokens from unavailable image estimates."""

    input_tokens: int
    image_tokens: int | None
    unestimated_images: int


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

    def __init__(
        self,
        image_token_estimator: ImageTokenEstimator | None = None,
        *,
        allow_unestimated_images: bool = False,
    ) -> None:
        """Bind a model-specific image estimator and explicit unknown-cost policy.

        Args:
            image_token_estimator: Host rule returning tokens or None for an unknown image cost.
            allow_unestimated_images: Permit an incomplete token total when the host accepts that risk.
        """
        self.image_token_estimator = image_token_estimator
        self.allow_unestimated_images = allow_unestimated_images

    def count(self, request: ContextCountInput) -> int:
        """Return estimated text and known image tokens for a complete request."""
        return self.estimate(request).input_tokens

    def estimate(self, request: ContextCountInput) -> TokenEstimate:
        """Estimate text independently of image references and binary transport.

        Args:
            request: Complete provider-neutral input to count.

        Returns:
            Token facts, with image_tokens set to None if any image cost is unknown.

        Raises:
            ImageTokenEstimateError: If an image cost is unknown and policy forbids it.
            ValueError: If the host estimator returns an invalid token count.
        """
        text_messages = []
        image_tokens = 0
        unestimated_images = 0
        for message in request.messages:
            text_message = message.model_dump(mode="json")
            if isinstance(message, UserMessage) and not isinstance(message.content, str):
                text_message["content"] = [
                    block.model_dump(mode="json") for block in message.content if not isinstance(block, ImageBlock)
                ]
                for block in message.content:
                    if not isinstance(block, ImageBlock):
                        continue
                    tokens = None if self.image_token_estimator is None else self.image_token_estimator(block.image)
                    if tokens is None:
                        unestimated_images += 1
                    elif type(tokens) is not int or tokens < 0:
                        raise ValueError("image token estimators must return a non-negative integer or None")
                    else:
                        image_tokens += tokens
            text_messages.append(text_message)
        if unestimated_images and not self.allow_unestimated_images:
            raise ImageTokenEstimateError(unestimated_images)
        payload = {
            "instructions": request.instructions,
            "messages": text_messages,
            "tools": [tool.model_dump(mode="json") for tool in request.tools],
        }
        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        text_tokens = max(1, (len(serialized) + 3) // 4)
        return TokenEstimate(
            input_tokens=text_tokens + image_tokens,
            image_tokens=None if unestimated_images else image_tokens,
            unestimated_images=unestimated_images,
        )


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
    image_tokens: int | None = None
    unestimated_images: int = 0


class BudgetGuard:
    """Evaluate request-local context budgets without mutating their inputs."""

    def __init__(self, budget: ContextBudget, counter: TokenCounter | None = None) -> None:
        """Create a guard for one explicit budget and token counter."""
        self.budget = budget
        self.counter = ApproximateTokenCounter() if counter is None else counter

    def check(self, request: ContextCountInput) -> BudgetStatus:
        """Return token and compaction facts for a complete request."""
        image_tokens = None
        unestimated_images = 0
        if isinstance(self.counter, ApproximateTokenCounter):
            estimate = self.counter.estimate(request)
            input_tokens = estimate.input_tokens
            image_tokens = estimate.image_tokens
            unestimated_images = estimate.unestimated_images
        else:
            input_tokens = self.counter.count(request)
        if input_tokens < 0:
            raise ValueError("token counters must return non-negative values")
        status = BudgetStatus(
            input_tokens=input_tokens,
            available_tokens=self.budget.available_tokens,
            requires_compaction=input_tokens >= self.budget.compact_at,
            exceeds_window=input_tokens > self.budget.available_tokens,
            estimated=self.counter.estimated,
            image_tokens=image_tokens,
            unestimated_images=unestimated_images,
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
