from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from pleiad.contracts import JsonObject, JsonValue, thaw_json_object


def default_object_schema() -> JsonObject:
    """Return the default object schema for a function-like value."""
    return {"type": "object"}


class OpenAIRequestModel(BaseModel):
    """Base validation model for OpenAI request bodies."""

    model_config = ConfigDict(
        extra="allow",
        frozen=True,
        validate_default=True,
    )


class OpenAIFunctionDefinition(OpenAIRequestModel):
    """Function schema carried by an OpenAI tool definition."""

    name: str = Field(min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=16_000)
    parameters: JsonObject = Field(default_factory=default_object_schema)
    strict: bool | None = None


class OpenAITool(OpenAIRequestModel):
    """OpenAI function tool declaration."""

    type: Literal["function"] = "function"
    function: OpenAIFunctionDefinition


class OpenAIFunctionCall(OpenAIRequestModel):
    """Function call embedded in an assistant message."""

    name: str = Field(min_length=1, max_length=128)
    arguments: str


class OpenAIToolCall(OpenAIRequestModel):
    """Tool call embedded in an OpenAI assistant message."""

    id: str = Field(min_length=1, max_length=256)
    type: Literal["function"] = "function"
    function: OpenAIFunctionCall


class OpenAIMessage(OpenAIRequestModel):
    """Message accepted by the OpenAI Chat Completions protocol."""

    role: Literal["developer", "system", "user", "assistant", "tool"]
    content: str | list[JsonValue] | None = None
    refusal: str | None = None
    name: str | None = Field(default=None, max_length=128)
    tool_call_id: str | None = Field(default=None, max_length=256)
    tool_calls: list[OpenAIToolCall] | None = None


class OpenAIRequest(OpenAIRequestModel):
    """OpenAI Chat Completions request body."""

    model: str | None = Field(default=None, min_length=1, max_length=256)
    messages: list[OpenAIMessage] = Field(min_length=1)
    tools: list[OpenAITool] | None = None
    tool_choice: str | JsonObject | None = None
    parallel_tool_calls: bool | None = None
    response_format: JsonObject | None = None
    max_tokens: int | None = Field(default=None, ge=1)
    max_completion_tokens: int | None = Field(default=None, ge=1)
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    top_p: float | None = Field(default=None, ge=0.0, le=1.0)
    stream: bool = False
    stream_options: JsonObject | None = None
    stop: str | list[str] | None = None
    seed: int | None = None
    user: str | None = None
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
            if name in OpenAIRequest.model_fields:
                raise ValueError(f"provider option cannot override protocol field: {name}")
            payload[name] = value
        return payload


def parse_request(value: OpenAIRequest | Mapping[str, object]) -> OpenAIRequest:
    """Validate one caller-supplied OpenAI request body."""
    if isinstance(value, OpenAIRequest):
        return value
    return OpenAIRequest.model_validate(value)
