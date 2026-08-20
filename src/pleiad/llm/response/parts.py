"""Ordered provider-neutral parts emitted by an LLM response."""

from typing import Annotated, Literal

from pydantic import Field

from pleiad.llm.contracts import JsonObject, LLMModel, ToolCall


class TextPart(LLMModel):
    """Visible assistant text."""

    type: Literal["text"] = "text"
    text: str
    provider_data: JsonObject = Field(default_factory=dict)


class ThinkingPart(LLMModel):
    """Provider-exposed thinking or reasoning content."""

    type: Literal["thinking"] = "thinking"
    text: str | None = None
    redacted: bool = False
    signature: str | None = None
    replayable: bool = True
    provider_data: JsonObject = Field(default_factory=dict)


class ToolCallPart(LLMModel):
    """A model-emitted tool call, including invalid raw arguments."""

    type: Literal["tool_call"] = "tool_call"
    call_id: str
    name: str
    arguments: JsonObject | None = None
    raw_arguments: str | None = None
    provider_data: JsonObject = Field(default_factory=dict)

    def as_tool_call(self) -> ToolCall:
        """Return the provider-neutral tool call projection."""
        return ToolCall(
            call_id=self.call_id,
            name=self.name,
            arguments=self.arguments,
            raw_arguments=self.raw_arguments,
        )


class RefusalPart(LLMModel):
    """Provider refusal content."""

    type: Literal["refusal"] = "refusal"
    refusal: str
    provider_data: JsonObject = Field(default_factory=dict)


class UnknownPart(LLMModel):
    """Unmapped provider content retained without losing the response."""

    type: Literal["unknown"] = "unknown"
    provider_type: str
    payload: JsonObject


type ResponsePart = Annotated[
    TextPart | ThinkingPart | ToolCallPart | RefusalPart | UnknownPart,
    Field(discriminator="type"),
]
