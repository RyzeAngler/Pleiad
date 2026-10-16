import json
from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from pleiad.contracts import JsonObject, JsonValue, thaw_json_object
from pleiad.messages.message import AssistantMessage, ToolCall
from pleiad.models.result import (
    ModelFinishReason,
    ModelResult,
    ModelStreamEvent,
    ModelStreamEventKind,
    ModelUsage,
    parse_token_count,
    sum_tokens,
)


class OpenAIResponseModel(BaseModel):
    """Base validation model for OpenAI response bodies."""

    model_config = ConfigDict(
        extra="allow",
        frozen=True,
        validate_default=True,
    )


class OpenAIResponseFunction(OpenAIResponseModel):
    """Function data in an OpenAI response tool call."""

    name: str
    arguments: str


class OpenAIResponseToolCall(OpenAIResponseModel):
    """Tool call in an OpenAI response message."""

    index: int = 0
    id: str
    type: Literal["function"] = "function"
    function: OpenAIResponseFunction


class OpenAIResponseMessage(OpenAIResponseModel):
    """Assistant message in an OpenAI response."""

    role: str = "assistant"
    content: str | None = None
    refusal: str | None = None
    reasoning_content: str | None = None
    tool_calls: tuple[OpenAIResponseToolCall, ...] = ()


class OpenAIChoice(OpenAIResponseModel):
    """One completion choice in an OpenAI response."""

    index: int = 0
    message: OpenAIResponseMessage
    finish_reason: str | None = None


class OpenAIUsage(OpenAIResponseModel):
    """Token usage in an OpenAI response."""

    prompt_tokens: int | None = Field(default=None, ge=0)
    completion_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    reasoning_tokens: int | None = Field(default=None, ge=0)
    prompt_tokens_details: JsonObject | None = None

    @property
    def cached_input_tokens(self) -> int | None:
        """Read the reported cache count without inventing an absent value."""
        if self.prompt_tokens_details is None:
            return None
        return parse_token_count(self.prompt_tokens_details.get("cached_tokens"), "cached input tokens")


class OpenAIResponse(OpenAIResponseModel):
    """OpenAI Chat Completions response body."""

    id: str
    object: str = "chat.completion"
    model: str
    choices: tuple[OpenAIChoice, ...] = Field(min_length=1)
    usage: OpenAIUsage | None = None

    def to_result(self, *, provider: str) -> ModelResult:
        """Project the first OpenAI choice into the internal common result."""
        choice = self.choices[0]
        message = choice.message
        calls = tuple(
            ToolCall(
                id=call.id,
                name=call.function.name,
                arguments=call.function.arguments,
            )
            for call in message.tool_calls
        )
        usage = ModelUsage()
        if self.usage is not None:
            usage = ModelUsage(
                input_tokens=self.usage.prompt_tokens,
                output_tokens=self.usage.completion_tokens,
                total_tokens=self.usage.total_tokens
                if self.usage.total_tokens is not None
                else sum_tokens(self.usage.prompt_tokens, self.usage.completion_tokens),
                reasoning_tokens=self.usage.reasoning_tokens,
                cached_input_tokens=self.usage.cached_input_tokens,
            )
        return ModelResult(
            model=self.model,
            provider=provider,
            response_id=self.id,
            thinking=message.reasoning_content if message.reasoning_content is not None else "",
            message=AssistantMessage(content=message.content, tool_calls=calls, refusal=message.refusal),
            structured_output=parse_structured_output(message.content),
            finish_reason=finish_reason(choice.finish_reason, calls),
            usage=usage,
        )


class OpenAIStreamFunction(OpenAIResponseModel):
    """Partial function data in an OpenAI stream."""

    name: str | None = None
    arguments: str | None = None


class OpenAIStreamToolCall(OpenAIResponseModel):
    """Partial tool call in an OpenAI stream."""

    index: int
    id: str | None = None
    type: Literal["function"] | None = None
    function: OpenAIStreamFunction | None = None


class OpenAIStreamDelta(OpenAIResponseModel):
    """Partial assistant message in an OpenAI stream."""

    role: str | None = None
    content: str | None = None
    refusal: str | None = None
    reasoning_content: str | None = None
    tool_calls: tuple[OpenAIStreamToolCall, ...] = ()


class OpenAIStreamChoice(OpenAIResponseModel):
    """One streamed OpenAI choice."""

    index: int = 0
    delta: OpenAIStreamDelta
    finish_reason: str | None = None


class OpenAIStreamChunk(OpenAIResponseModel):
    """OpenAI Chat Completions stream chunk."""

    id: str | None = None
    object: str = "chat.completion.chunk"
    model: str
    choices: tuple[OpenAIStreamChoice, ...] = ()
    usage: OpenAIUsage | None = None

    def events(self) -> tuple[ModelStreamEvent, ...]:
        """Project visible OpenAI stream deltas into common internal events."""
        events: list[ModelStreamEvent] = []
        for choice in self.choices:
            delta = choice.delta
            if delta.content:
                events.append(ModelStreamEvent(ModelStreamEventKind.TEXT_DELTA, text=delta.content))
            if delta.refusal:
                events.append(ModelStreamEvent(ModelStreamEventKind.REFUSAL_DELTA, text=delta.refusal))
            if delta.reasoning_content:
                events.append(ModelStreamEvent(ModelStreamEventKind.THINKING_DELTA, text=delta.reasoning_content))
            for call in delta.tool_calls:
                function = call.function
                events.append(
                    ModelStreamEvent(
                        ModelStreamEventKind.TOOL_CALL_DELTA,
                        tool_call_index=call.index,
                        tool_call_id=call.id,
                        tool_name=function.name if function is not None else None,
                        arguments_delta=(
                            function.arguments if function is not None and function.arguments is not None else ""
                        ),
                    ),
                )
            if choice.finish_reason is not None:
                events.append(ModelStreamEvent(ModelStreamEventKind.COMPLETED))
        return tuple(events)


def parse_response(raw: object) -> OpenAIResponse:
    """Convert an official OpenAI response object into the protocol body."""
    choices_value = read_field(raw, "choices")
    choices = parse_choices(choices_value)
    response_id = required_text(read_field(raw, "id"), "response id")
    response_model = required_text(read_field(raw, "model"), "response model")
    return OpenAIResponse(
        id=response_id,
        object=required_text(read_field(raw, "object"), "response object"),
        model=response_model,
        choices=tuple(choices),
        usage=parse_usage(read_field(raw, "usage")),
    )


def parse_stream_chunk(raw: object) -> OpenAIStreamChunk:
    """Convert an official OpenAI stream object into a protocol chunk."""
    raw_choices = read_field(raw, "choices")
    choices: list[OpenAIStreamChoice] = []
    if raw_choices is not None:
        for item in sequence_value(raw_choices, "stream choices"):
            delta_value = read_field(item, "delta")
            if delta_value is None:
                raise ValueError("stream choice is missing delta")
            choices.append(
                OpenAIStreamChoice(
                    index=integer_value(read_field(item, "index"), "stream choice index"),
                    delta=parse_stream_delta(delta_value),
                    finish_reason=optional_text(read_field(item, "finish_reason")),
                ),
            )
    chunk_model = required_text(read_field(raw, "model"), "stream model")
    return OpenAIStreamChunk(
        id=optional_text(read_field(raw, "id")),
        object=required_text(read_field(raw, "object"), "stream object"),
        model=chunk_model,
        choices=tuple(choices),
        usage=parse_usage(read_field(raw, "usage")),
    )


def parse_choices(value: object) -> list[OpenAIChoice]:
    """Parse choices from an OpenAI response object."""
    choices: list[OpenAIChoice] = []
    for item in sequence_value(value, "response choices"):
        message_value = read_field(item, "message")
        if message_value is None:
            raise ValueError("response choice is missing message")
        choices.append(
            OpenAIChoice(
                index=integer_value(read_field(item, "index"), "response choice index"),
                message=parse_message(message_value),
                finish_reason=optional_text(read_field(item, "finish_reason")),
            ),
        )
    if not choices:
        raise ValueError("response choices must not be empty")
    return choices


def parse_message(value: object) -> OpenAIResponseMessage:
    """Parse an OpenAI assistant message."""
    raw_calls = read_field(value, "tool_calls")
    calls: list[OpenAIResponseToolCall] = []
    if raw_calls is not None:
        for item in sequence_value(raw_calls, "tool calls"):
            function_value = read_field(item, "function")
            if function_value is None:
                raise ValueError("tool call is missing function")
            calls.append(
                OpenAIResponseToolCall(
                    id=required_text(read_field(item, "id"), "tool call id"),
                    type=required_text(read_field(item, "type"), "tool call type"),
                    function=OpenAIResponseFunction(
                        name=required_text(read_field(function_value, "name"), "tool name"),
                        arguments=required_text(read_field(function_value, "arguments"), "tool arguments"),
                    ),
                ),
            )
    return OpenAIResponseMessage(
        role=required_text(read_field(value, "role"), "message role"),
        content=optional_text(read_field(value, "content")),
        refusal=optional_text(read_field(value, "refusal")),
        reasoning_content=optional_text(read_field(value, "reasoning_content")),
        tool_calls=tuple(calls),
    )


def parse_stream_delta(value: object) -> OpenAIStreamDelta:
    """Parse one OpenAI stream delta."""
    raw_calls = read_field(value, "tool_calls")
    calls: list[OpenAIStreamToolCall] = []
    if raw_calls is not None:
        for item in sequence_value(raw_calls, "stream tool calls"):
            function_value = read_field(item, "function")
            function = None
            if function_value is not None:
                function = OpenAIStreamFunction(
                    name=optional_text(read_field(function_value, "name")),
                    arguments=optional_text(read_field(function_value, "arguments")),
                )
            calls.append(
                OpenAIStreamToolCall(
                    index=integer_value(read_field(item, "index"), "stream tool call index"),
                    id=optional_text(read_field(item, "id")),
                    type=optional_text(read_field(item, "type")),
                    function=function,
                ),
            )
    return OpenAIStreamDelta(
        role=optional_text(read_field(value, "role")),
        content=optional_text(read_field(value, "content")),
        refusal=optional_text(read_field(value, "refusal")),
        reasoning_content=optional_text(read_field(value, "reasoning_content")),
        tool_calls=tuple(calls),
    )


def parse_usage(value: object) -> OpenAIUsage | None:
    """Parse optional OpenAI usage data."""
    if value is None:
        return None
    details_value = read_field(value, "prompt_tokens_details")
    details = None
    if details_value is not None:
        details = object_value(details_value, "prompt token details")
    reasoning = read_field(value, "reasoning_tokens")
    completion_details = read_field(value, "completion_tokens_details")
    if reasoning is None and completion_details is not None:
        reasoning = read_field(completion_details, "reasoning_tokens")
    return OpenAIUsage(
        prompt_tokens=parse_token_count(read_field(value, "prompt_tokens"), "prompt tokens"),
        completion_tokens=parse_token_count(read_field(value, "completion_tokens"), "completion tokens"),
        total_tokens=parse_token_count(read_field(value, "total_tokens"), "total tokens"),
        reasoning_tokens=parse_token_count(reasoning, "reasoning tokens"),
        prompt_tokens_details=details,
    )


def read_field(value: object, name: str) -> object:
    """Read a field from a mapping or an SDK model object."""
    if isinstance(value, Mapping):
        return value.get(name)
    try:
        attributes = vars(value)
    except TypeError:
        return None
    return attributes.get(name)


def sequence_value(value: object, name: str) -> list[object] | tuple[object, ...]:
    """Require a JSON-like sequence at a response field."""
    if not isinstance(value, list | tuple):
        raise ValueError(f"{name} must be a sequence")
    return value


def object_value(value: object, name: str) -> JsonObject:
    """Require a JSON object at a response field."""
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be an object")
    return thaw_json_object(value)


def required_text(value: object, name: str) -> str:
    """Require non-empty text at a response field."""
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be non-empty text")
    return value


def optional_text(value: object) -> str | None:
    """Return optional response text after validating its type."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("response text field must be text")
    return value


def integer_value(value: object, name: str, *, default: int | None = None) -> int:
    """Read an integer response field."""
    if value is None and default is not None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    return value


def parse_arguments(arguments: str) -> JsonObject | None:
    """Parse a tool argument string while preserving invalid raw arguments."""
    try:
        value: object = json.loads(arguments)
    except json.JSONDecodeError:
        return None
    if not isinstance(value, Mapping):
        return None
    return thaw_json_object(value)


def parse_structured_output(content: str | None) -> JsonValue | None:
    """Parse structured text when it is a JSON value."""
    if content is None:
        return None
    try:
        value: object = json.loads(content)
    except json.JSONDecodeError:
        return None
    return json_value(value)


def json_value(value: object) -> JsonValue:
    """Validate a decoded JSON value against the SDK JSON contract."""
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, list):
        return [json_value(item) for item in value]
    if isinstance(value, dict):
        return {key: json_value(item) for key, item in value.items()}
    raise ValueError("structured output must be JSON serializable")


def finish_reason(reason: str | None, calls: tuple[ToolCall, ...]) -> ModelFinishReason:
    """Map an OpenAI finish reason into the internal result vocabulary."""
    if calls:
        return ModelFinishReason.TOOL_CALL
    if reason == "stop":
        return ModelFinishReason.STOP
    if reason == "length":
        return ModelFinishReason.LENGTH
    if reason == "content_filter":
        return ModelFinishReason.CONTENT_FILTER
    return ModelFinishReason.UNKNOWN
