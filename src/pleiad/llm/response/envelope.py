"""Response envelope and raw-item preservation."""

from pydantic import Field

from pleiad.llm.config import LLMProtocol, LLMProvider
from pleiad.llm.contracts import FinishReason, JsonObject, LLMModel, ToolCall, Usage
from pleiad.llm.response.parts import ResponsePart, TextPart, ThinkingPart, ToolCallPart


class RawResponseItem(LLMModel):
    """Raw provider item retained for diagnostics and protocol replay."""

    provider_type: str
    item_id: str | None = None
    payload: JsonObject
    replayable: bool = True


class LLMResponse(LLMModel):
    """Provider-neutral response with ordered parts as the source of truth."""

    request_id: str
    protocol: LLMProtocol
    provider: LLMProvider
    model: str
    response_id: str | None = None
    parts: tuple[ResponsePart, ...] = ()
    structured_output: object | None = None
    usage: Usage = Field(default_factory=Usage)
    finish_reason: FinishReason = FinishReason.UNKNOWN
    raw_items: tuple[RawResponseItem, ...] = ()
    provider_metadata: JsonObject = Field(default_factory=dict)

    @property
    def text(self) -> str:
        """Project visible text from ordered response parts."""
        return "".join(part.text for part in self.parts if isinstance(part, TextPart))

    @property
    def thinking(self) -> str:
        """Project non-redacted thinking text from ordered response parts."""
        return "".join(
            part.text or ""
            for part in self.parts
            if isinstance(part, ThinkingPart) and not part.redacted
        )

    @property
    def tool_calls(self) -> tuple[ToolCall, ...]:
        """Project normalized tool calls from ordered response parts."""
        return tuple(part.as_tool_call() for part in self.parts if isinstance(part, ToolCallPart))

    @property
    def has_tool_calls(self) -> bool:
        """Return whether the model requested at least one tool."""
        return any(isinstance(part, ToolCallPart) for part in self.parts)
