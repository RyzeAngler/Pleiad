"""Provider-neutral request contracts for the LLM layer."""

from enum import StrEnum
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

type JsonObject = dict[str, Any]
"""JSON object carried across the provider boundary."""

type JsonValue = Any
"""JSON-compatible value returned by a provider."""


class LLMModel(BaseModel):
    """Strict immutable base for in-memory LLM contracts."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        validate_default=True,
    )


class MessageRole(StrEnum):
    """Neutral conversation roles understood by protocol adapters."""

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class ResponseFormatType(StrEnum):
    """Output formats requested from a model."""

    TEXT = "text"
    JSON_SCHEMA = "json_schema"
    JSON_OBJECT = "json_object"


class FinishReason(StrEnum):
    """Normalized reasons for a provider response to finish."""

    STOP = "stop"
    TOOL_CALL = "tool_call"
    LENGTH = "length"
    CONTENT_FILTER = "content_filter"
    ERROR = "error"
    UNKNOWN = "unknown"


class ToolDefinition(LLMModel):
    """Provider-neutral callable tool schema."""

    name: str = Field(min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=16_000)
    input_schema: JsonObject
    strict: bool = False


class ToolCall(LLMModel):
    """Provider-neutral tool invocation returned by a model."""

    call_id: str = Field(min_length=1, max_length=256)
    name: str = Field(min_length=1, max_length=128)
    arguments: JsonObject | None = None
    raw_arguments: str | None = None


class Message(LLMModel):
    """Provider-neutral conversation item with replay metadata."""

    role: MessageRole
    content: str | None = None
    content_blocks: tuple[JsonObject, ...] = ()
    name: str | None = Field(default=None, max_length=128)
    tool_call_id: str | None = Field(default=None, max_length=256)
    tool_calls: tuple[ToolCall, ...] = ()
    thinking: str | None = None
    provider_data: JsonObject = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_tool_shape(self) -> Self:
        """Require an identifier for tool result messages."""
        if self.role is MessageRole.TOOL and self.tool_call_id is None:
            raise ValueError("tool messages require a tool_call_id")
        return self


class ResponseFormat(LLMModel):
    """Requested text, JSON object, or JSON Schema response format."""

    type: ResponseFormatType = ResponseFormatType.TEXT
    name: str | None = Field(default=None, max_length=128)
    json_schema: JsonObject | None = None
    strict: bool = True

    @model_validator(mode="after")
    def validate_shape(self) -> Self:
        """Require schema metadata only for structured schema output."""
        if self.type is ResponseFormatType.JSON_SCHEMA and self.json_schema is None:
            raise ValueError("json_schema response format requires a schema")
        if self.type is not ResponseFormatType.JSON_SCHEMA and (
            self.name is not None or self.json_schema is not None
        ):
            raise ValueError("non-schema response formats cannot contain schema metadata")
        return self


class ThinkingConfig(LLMModel):
    """Normalized reasoning/thinking request options."""

    enabled: bool = False
    effort: Literal["low", "medium", "high", "max"] | None = None
    budget_tokens: int | None = Field(default=None, ge=1)


class LLMRequest(LLMModel):
    """Complete provider-neutral request sent to one model."""

    request_id: str = Field(min_length=1, max_length=256)
    model: str = Field(min_length=1, max_length=256)
    messages: tuple[Message, ...] = Field(min_length=1)
    tools: tuple[ToolDefinition, ...] = ()
    tool_choice: Literal["auto", "none", "required"] | str | None = None
    response_format: ResponseFormat = Field(default_factory=ResponseFormat)
    thinking: ThinkingConfig = Field(default_factory=ThinkingConfig)
    max_output_tokens: int | None = Field(default=None, ge=1)
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    stream: bool = False
    metadata: JsonObject = Field(default_factory=dict)
    provider_options: JsonObject = Field(default_factory=dict)


class Usage(LLMModel):
    """Normalized token usage reported by a provider."""

    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    reasoning_tokens: int = Field(default=0, ge=0)
    cached_input_tokens: int = Field(default=0, ge=0)
    details: JsonObject = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_total(self) -> Self:
        """Require total usage to match known input and output usage."""
        expected = self.input_tokens + self.output_tokens
        if self.total_tokens not in {0, expected}:
            raise ValueError("total_tokens must equal input_tokens plus output_tokens")
        return self
