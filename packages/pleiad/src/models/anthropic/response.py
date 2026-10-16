import json
from collections.abc import Mapping

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


class AnthropicResponseModel(BaseModel):
    """Base validation model for Anthropic response bodies."""

    model_config = ConfigDict(
        extra="allow",
        frozen=True,
        validate_default=True,
    )


class AnthropicContentBlock(AnthropicResponseModel):
    """Content block in an Anthropic message response."""

    type: str
    text: str | None = None
    id: str | None = None
    name: str | None = None
    input: JsonObject | None = None
    thinking: str | None = None
    signature: str | None = None


class AnthropicUsage(AnthropicResponseModel):
    """Token usage in an Anthropic response."""

    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    cache_creation_input_tokens: int | None = Field(default=None, ge=0)
    cache_read_input_tokens: int | None = Field(default=None, ge=0)


class AnthropicResponse(AnthropicResponseModel):
    """Anthropic Messages response body."""

    id: str
    type: str = "message"
    role: str = "assistant"
    content: tuple[AnthropicContentBlock, ...]
    model: str
    stop_reason: str | None = None
    usage: AnthropicUsage | None = None

    def to_result(self, *, provider: str) -> ModelResult:
        """Project an Anthropic response into the internal common result."""
        text_parts: list[str] = []
        thinking_parts: list[str] = []
        calls: list[ToolCall] = []
        refusal: str | None = None
        for block in self.content:
            if block.type == "text" and block.text is not None:
                text_parts.append(block.text)
            elif block.type == "thinking" and block.thinking is not None:
                thinking_parts.append(block.thinking)
            elif block.type == "tool_use":
                if block.id is None or block.name is None or block.input is None:
                    raise ValueError("Anthropic tool_use block is incomplete")
                calls.append(ToolCall(id=block.id, name=block.name, arguments=json.dumps(block.input, sort_keys=True)))
            elif block.type in {"redacted_thinking", "server_tool_use"}:
                continue
            elif block.type == "refusal" and block.text is not None:
                refusal = block.text
            else:
                raise ValueError(f"Unsupported Anthropic content block: {block.type}")
        usage = ModelUsage()
        if self.usage is not None:
            usage = ModelUsage(
                input_tokens=self.usage.input_tokens,
                output_tokens=self.usage.output_tokens,
                total_tokens=sum_tokens(self.usage.input_tokens, self.usage.output_tokens),
                cached_input_tokens=self.usage.cache_read_input_tokens,
            )
        return ModelResult(
            model=self.model,
            provider=provider,
            response_id=self.id,
            thinking="".join(thinking_parts),
            message=AssistantMessage(content="".join(text_parts) or None, tool_calls=tuple(calls), refusal=refusal),
            structured_output=parse_structured_output("".join(text_parts)),
            finish_reason=finish_reason(self.stop_reason, bool(calls)),
            usage=usage,
        )


class AnthropicStreamUsage(AnthropicResponseModel):
    """Cumulative usage updates whose omitted fields retain message-start values."""

    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    cache_read_input_tokens: int | None = Field(default=None, ge=0)


class AnthropicStreamEvent(AnthropicResponseModel):
    """Anthropic Messages stream event."""

    type: str
    index: int | None = None
    content_block: AnthropicContentBlock | None = None
    delta: JsonObject | None = None
    message: AnthropicResponse | None = None
    usage: AnthropicStreamUsage | None = None

    def events(self) -> tuple[ModelStreamEvent, ...]:
        """Project one Anthropic event into common internal events."""
        if self.type == "content_block_start" and self.content_block is not None:
            block = self.content_block
            if block.type == "tool_use":
                return (
                    ModelStreamEvent(
                        ModelStreamEventKind.TOOL_CALL_DELTA,
                        tool_call_index=self.index,
                        tool_call_id=block.id,
                        tool_name=block.name,
                    ),
                )
        if self.type == "message_stop":
            return (ModelStreamEvent(ModelStreamEventKind.COMPLETED),)
        if self.type != "content_block_delta" or self.delta is None:
            return ()
        delta_type = self.delta.get("type")
        if delta_type == "text_delta":
            text = self.delta.get("text")
            if isinstance(text, str):
                return (ModelStreamEvent(ModelStreamEventKind.TEXT_DELTA, text=text),)
        if delta_type == "thinking_delta":
            thinking = self.delta.get("thinking")
            if isinstance(thinking, str):
                return (ModelStreamEvent(ModelStreamEventKind.THINKING_DELTA, text=thinking),)
        if delta_type == "input_json_delta":
            arguments = self.delta.get("partial_json")
            if isinstance(arguments, str):
                return (
                    ModelStreamEvent(
                        ModelStreamEventKind.TOOL_CALL_DELTA,
                        tool_call_index=self.index,
                        arguments_delta=arguments,
                    ),
                )
        return ()


def parse_response(raw: object, *, allow_empty_content: bool = False) -> AnthropicResponse:
    """Convert an official Anthropic response object into the protocol body."""
    response_id = required_text(read_field(raw, "id"), "response id")
    response_model = required_text(read_field(raw, "model"), "response model")
    raw_content = read_field(raw, "content")
    blocks = parse_content(raw_content, allow_empty=allow_empty_content)
    return AnthropicResponse(
        id=response_id,
        type=required_text(read_field(raw, "type"), "response type"),
        role=required_text(read_field(raw, "role"), "response role"),
        content=tuple(blocks),
        model=response_model,
        stop_reason=optional_text(read_field(raw, "stop_reason")),
        usage=parse_usage(read_field(raw, "usage")),
    )


def parse_event(raw: object) -> AnthropicStreamEvent:
    """Convert one official Anthropic stream event into the protocol event."""
    event_type = required_text(read_field(raw, "type"), "stream event type")
    raw_block = read_field(raw, "content_block")
    block = parse_block(raw_block) if raw_block is not None else None
    raw_message = read_field(raw, "message")
    message = parse_response(raw_message, allow_empty_content=True) if raw_message is not None else None
    raw_delta = read_field(raw, "delta")
    delta = object_value(raw_delta, "stream delta") if raw_delta is not None else None
    raw_index = read_field(raw, "index")
    index = integer_value(raw_index, "stream block index") if raw_index is not None else None
    raw_usage = read_field(raw, "usage")
    usage = (
        AnthropicStreamUsage.model_validate(object_value(raw_usage, "stream usage")) if raw_usage is not None else None
    )
    return AnthropicStreamEvent(
        type=event_type,
        index=index,
        content_block=block,
        delta=delta,
        message=message,
        usage=usage,
    )


def parse_content(value: object, *, allow_empty: bool = False) -> list[AnthropicContentBlock]:
    """Parse Anthropic response content blocks."""
    blocks: list[AnthropicContentBlock] = []
    for item in sequence_value(value, "response content"):
        blocks.append(parse_block(item))
    if not blocks and not allow_empty:
        raise ValueError("response content must not be empty")
    return blocks


def parse_block(value: object) -> AnthropicContentBlock:
    """Parse one Anthropic content block."""
    block_type = required_text(read_field(value, "type"), "content block type")
    raw_input = read_field(value, "input")
    parsed_input = object_value(raw_input, "tool input") if raw_input is not None else None
    text = optional_text(read_field(value, "text"))
    thinking = optional_text(read_field(value, "thinking"))
    block_id = optional_text(read_field(value, "id"))
    name = optional_text(read_field(value, "name"))
    if block_type == "text" and text is None:
        raise ValueError("text content block is missing text")
    if block_type == "thinking" and thinking is None:
        raise ValueError("thinking content block is missing thinking")
    if block_type == "tool_use" and (block_id is None or name is None or parsed_input is None):
        raise ValueError("tool_use content block is incomplete")
    return AnthropicContentBlock(
        type=block_type,
        text=text,
        id=block_id,
        name=name,
        input=parsed_input,
        thinking=thinking,
        signature=optional_text(read_field(value, "signature")),
    )


def parse_usage(value: object) -> AnthropicUsage | None:
    """Parse optional Anthropic usage data."""
    if value is None:
        return None
    return AnthropicUsage(
        input_tokens=parse_token_count(read_field(value, "input_tokens"), "input tokens"),
        output_tokens=parse_token_count(read_field(value, "output_tokens"), "output tokens"),
        cache_creation_input_tokens=parse_token_count(
            read_field(value, "cache_creation_input_tokens"),
            "cache creation input tokens",
        ),
        cache_read_input_tokens=parse_token_count(
            read_field(value, "cache_read_input_tokens"),
            "cache read input tokens",
        ),
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


def parse_structured_output(content: str) -> JsonValue | None:
    """Parse structured text when it is a JSON value."""
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


def finish_reason(reason: str | None, has_tool_calls: bool) -> ModelFinishReason:
    """Map an Anthropic stop reason into the internal result vocabulary."""
    if has_tool_calls or reason == "tool_use":
        return ModelFinishReason.TOOL_CALL
    if reason in {"end_turn", "stop_sequence"}:
        return ModelFinishReason.STOP
    if reason == "max_tokens":
        return ModelFinishReason.LENGTH
    return ModelFinishReason.UNKNOWN
