from dataclasses import dataclass
from enum import StrEnum

from pleiad.contracts import JsonValue
from pleiad.messages.message import AssistantMessage, ToolCall


class ModelFinishReason(StrEnum):
    """Normalized reasons for a model response to finish."""

    STOP = "stop"
    TOOL_CALL = "tool_call"
    LENGTH = "length"
    CONTENT_FILTER = "content_filter"
    ERROR = "error"
    UNKNOWN = "unknown"


class ModelStreamEventKind(StrEnum):
    """Normalized categories emitted while a model response is streaming."""

    TEXT_DELTA = "text_delta"
    REFUSAL_DELTA = "refusal_delta"
    THINKING_DELTA = "thinking_delta"
    TOOL_CALL_DELTA = "tool_call_delta"
    COMPLETED = "completed"


@dataclass(frozen=True, slots=True)
class ModelUsage:
    """Normalized token usage reported by a model provider."""

    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    reasoning_tokens: int | None = None
    cached_input_tokens: int | None = None

    def __post_init__(self) -> None:
        """Reject invalid reported counts before they enter events or budgets."""
        for value in (
            self.input_tokens,
            self.output_tokens,
            self.total_tokens,
            self.reasoning_tokens,
            self.cached_input_tokens,
        ):
            parse_token_count(value, "model usage")
        if (
            self.total_tokens is not None
            and self.input_tokens is not None
            and self.output_tokens is not None
            and self.total_tokens != self.input_tokens + self.output_tokens
        ):
            raise ValueError("Model total usage must equal input plus output tokens")


@dataclass(frozen=True, slots=True)
class ModelResult:
    """Internal common result consumed by the SDK runtime."""

    model: str
    provider: str
    response_id: str | None
    thinking: str
    message: AssistantMessage
    structured_output: JsonValue | None
    finish_reason: ModelFinishReason
    usage: ModelUsage

    @property
    def has_tool_calls(self) -> bool:
        """Return whether the result contains at least one tool call."""
        return bool(self.message.tool_calls)

    @property
    def text(self) -> str:
        """Return the Assistant text as a convenience view."""
        return self.message.content or ""

    @property
    def tool_calls(self) -> tuple[ToolCall, ...]:
        """Return the Tool declarations owned by the completed message."""
        return self.message.tool_calls

    @property
    def refusal(self) -> str | None:
        """Return the Provider refusal captured by the completed message."""
        return self.message.refusal


@dataclass(frozen=True, slots=True)
class ModelStreamEvent:
    """Internal common stream event projected from a protocol stream."""

    kind: ModelStreamEventKind
    text: str = ""
    tool_call_index: int | None = None
    tool_call_id: str | None = None
    tool_name: str | None = None
    arguments_delta: str = ""
    result: ModelResult | None = None


def sum_tokens(left: int | None, right: int | None) -> int | None:
    """Sum token counts only when both operands are known.

    Args:
        left: A reported or already aggregated token count.
        right: A reported token count.

    Returns:
        The sum, or None if either count is unknown.
    """
    return None if left is None or right is None else left + right


def parse_token_count(value: object, name: str) -> int | None:
    """Preserve missing counts and reject invalid provider measurements.

    Args:
        value: Provider count or an omitted field.
        name: Diagnostic field name.

    Returns:
        The nonnegative count or None.

    Raises:
        ValueError: If the count is negative, Boolean, or not an integer.
    """
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a nonnegative integer")
    return value
