from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from pleiad.contracts import JsonObject, JsonValue, thaw_json_object


def default_object_schema() -> JsonObject:
    """Return the default object schema for an Anthropic tool."""
    return {"type": "object"}


class AnthropicRequestModel(BaseModel):
    """Base validation model for Anthropic request bodies."""

    model_config = ConfigDict(
        extra="allow",
        frozen=True,
        validate_default=True,
    )


class AnthropicMessage(AnthropicRequestModel):
    """Message accepted by the Anthropic Messages protocol."""

    role: Literal["user", "assistant"]
    content: str | list[JsonObject]


class AnthropicTool(AnthropicRequestModel):
    """Tool declaration accepted by Anthropic Messages."""

    name: str = Field(min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=16_000)
    input_schema: JsonObject = Field(default_factory=default_object_schema)


class AnthropicRequest(AnthropicRequestModel):
    """Anthropic Messages request body."""

    model: str | None = Field(default=None, min_length=1, max_length=256)
    messages: list[AnthropicMessage] = Field(min_length=1)
    max_tokens: int = Field(ge=1)
    system: str | list[JsonObject] | None = None
    tools: list[AnthropicTool] | None = None
    tool_choice: JsonObject | None = None
    thinking: JsonObject | None = None
    temperature: float | None = Field(default=None, ge=0.0, le=1.0)
    top_p: float | None = Field(default=None, ge=0.0, le=1.0)
    top_k: int | None = Field(default=None, ge=0)
    stop_sequences: list[str] | None = None
    stream: bool = False
    metadata: JsonObject | None = None

    def payload(self, *, model: str, provider_options: Mapping[str, JsonValue]) -> JsonObject:
        """Return a detached request body with the configured model name.

        Args:
            model: Model name configured on the concrete client.
            provider_options: Additional provider-owned request fields.

        Returns:
            A JSON object suitable for the selected provider binding.

        Raises:
            ValueError: If the request body names a different model.
        """
        if self.model is not None and self.model != model:
            raise ValueError("request model must match the model client")
        payload = thaw_json_object(self.model_dump(mode="json", exclude_none=True))
        payload["model"] = model
        for name, value in provider_options.items():
            if name in AnthropicRequest.model_fields:
                raise ValueError(f"provider option cannot override protocol field: {name}")
            payload[name] = value
        return payload


def parse_request(value: AnthropicRequest | Mapping[str, object]) -> AnthropicRequest:
    """Validate one caller-supplied Anthropic request body."""
    if isinstance(value, AnthropicRequest):
        return value
    return AnthropicRequest.model_validate(value)
