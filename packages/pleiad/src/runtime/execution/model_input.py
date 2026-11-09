import hashlib
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from uuid import NAMESPACE_URL, uuid5

from pleiad.contracts import JsonObject, JsonSchema, JsonValue, thaw_json_object
from pleiad.messages.message import AssistantMessage, Message, SystemMessage, ToolCall, ToolMessage, UserMessage
from pleiad.models.anthropic.client import AnthropicModelClient
from pleiad.models.anthropic.request import AnthropicMessage, AnthropicRequest, AnthropicTool
from pleiad.models.anthropic.response import AnthropicResponse, AnthropicStreamEvent
from pleiad.models.anthropic.response import finish_reason as anthropic_finish_reason
from pleiad.models.contracts import Tool
from pleiad.models.openai.client import OpenAIModelClient
from pleiad.models.openai.request import (
    OpenAIFunctionCall,
    OpenAIFunctionDefinition,
    OpenAIMessage,
    OpenAIRequest,
    OpenAITool,
    OpenAIToolCall,
)
from pleiad.models.openai.response import OpenAIResponse, OpenAIStreamChunk
from pleiad.models.openai.response import finish_reason as openai_finish_reason
from pleiad.models.result import (
    ModelResult,
    ModelStreamEvent,
    ModelStreamEventKind,
    ModelUsage,
    sum_tokens,
)
from pleiad.tools.contracts import ToolResult


@dataclass(frozen=True, slots=True)
class ModelInput:
    """Provider-neutral request assembled by Runtime for one model call."""

    instructions: str
    messages: tuple[Message, ...]
    tools: tuple[Tool, ...]
    max_output_tokens: int
    output_schema: JsonSchema | None

    def digest(self) -> str:
        """Identify the exact resolved request without exposing its original content."""
        payload = {
            "instructions": self.instructions,
            "messages": [message.model_dump(mode="json") for message in self.messages],
            "tools": [tool.model_dump(mode="json") for tool in self.tools],
            "max_output_tokens": self.max_output_tokens,
            "output_schema": None if self.output_schema is None else thaw_json_object(self.output_schema),
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(slots=True)
class StreamToolCallState:
    """Accumulate one streamed Tool Call's protocol fragments."""

    call_id: str | None = None
    name: str | None = None
    arguments: str = ""
    initial_arguments: str | None = None

    def to_call(self) -> ToolCall:
        """Require the provider's real identity before exposing a completed call."""
        if not self.call_id or not self.name:
            raise ValueError("Streamed tool call is missing its id or name")
        arguments = self.arguments
        if not arguments and self.initial_arguments is not None:
            arguments = self.initial_arguments
        return ToolCall(id=self.call_id, name=self.name, arguments=arguments)


def tool_result_message(
    call: ToolCall,
    result: ToolResult,
    *,
    run_id: str,
    step_id: str,
) -> ToolMessage:
    """Project one Tool result into its stable model-visible message.

    Args:
        call: Model-declared Tool call receiving the result.
        result: Normalized execution result.
        run_id: Run that owns the Tool execution.
        step_id: ModelStep that owns the Tool declaration.

    Returns:
        The Tool message persisted in Session and sent to the model.
    """
    content = json.dumps(
        {
            "ok": result.ok,
            "output": result.output,
            "error_code": result.error_code,
            "error_message": result.error_message,
            "error_type": result.error_type,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    message_id = uuid5(NAMESPACE_URL, f"pleiad:tool-result:{run_id}:{step_id}:{call.id}").hex
    return ToolMessage(message_id=message_id, tool_call_id=call.id, content=content)


def build_model_input(
    *,
    instructions: str,
    messages: tuple[Message, ...],
    tools: tuple[Tool, ...],
    max_output_tokens: int,
    output_schema: JsonSchema | None,
) -> ModelInput:
    """Create one immutable provider-neutral model input."""
    return ModelInput(
        instructions=instructions,
        messages=messages,
        tools=tools,
        max_output_tokens=max_output_tokens,
        output_schema=output_schema,
    )


async def complete_model(
    model: OpenAIModelClient | AnthropicModelClient,
    model_input: ModelInput,
) -> ModelResult:
    """Project and complete one request through the model object's protocol."""
    if isinstance(model, OpenAIModelClient):
        openai_response: OpenAIResponse = await model.complete(openai_request(model, model_input))
        return openai_response.to_result(provider=model.provider)
    if isinstance(model, AnthropicModelClient):
        anthropic_response: AnthropicResponse = await model.complete(anthropic_request(model, model_input))
        return anthropic_response.to_result(provider=model.provider)
    raise TypeError("Agent model must be an OpenAIModelClient or AnthropicModelClient")


async def stream_model(
    model: OpenAIModelClient | AnthropicModelClient,
    model_input: ModelInput,
) -> AsyncIterator[ModelStreamEvent]:
    """Stream visible deltas and finish with one normalized ModelResult."""
    if isinstance(model, OpenAIModelClient):
        openai_accumulator = OpenAIStreamAccumulator(model.model, model.provider)
        request = openai_request(model, model_input).model_copy(update={"stream_options": {"include_usage": True}})
        async for chunk in model.stream(request):
            openai_accumulator.add(chunk)
            for event in chunk.events():
                if event.kind is not ModelStreamEventKind.COMPLETED:
                    yield event
        yield ModelStreamEvent(ModelStreamEventKind.COMPLETED, result=openai_accumulator.result())
        return
    if isinstance(model, AnthropicModelClient):
        anthropic_accumulator = AnthropicStreamAccumulator(model.model, model.provider)
        async for anthropic_event in model.stream(anthropic_request(model, model_input)):
            anthropic_accumulator.add(anthropic_event)
            for stream_event in anthropic_event.events():
                if stream_event.kind is not ModelStreamEventKind.COMPLETED:
                    yield stream_event
        yield ModelStreamEvent(ModelStreamEventKind.COMPLETED, result=anthropic_accumulator.result())
        return
    raise TypeError("Agent model must be an OpenAIModelClient or AnthropicModelClient")


def openai_request(model: OpenAIModelClient, model_input: ModelInput) -> OpenAIRequest:
    """Project ModelInput into an OpenAI Chat Completions request."""
    response_format: JsonObject | None = None
    if model_input.output_schema is not None:
        response_format = {
            "type": "json_schema",
            "json_schema": {
                "name": "agent_output",
                "schema": thaw_json_object(model_input.output_schema),
                "strict": True,
            },
        }
    return OpenAIRequest(
        model=model.model,
        messages=openai_messages(model_input),
        tools=[openai_tool(tool) for tool in model_input.tools] or None,
        response_format=response_format,
        max_completion_tokens=model_input.max_output_tokens,
    )


def anthropic_request(model: AnthropicModelClient, model_input: ModelInput) -> AnthropicRequest:
    """Project ModelInput into an Anthropic Messages request."""
    return AnthropicRequest(
        model=model.model,
        messages=anthropic_messages(model_input),
        system=model_input.instructions,
        tools=[anthropic_tool(tool) for tool in model_input.tools] or None,
        max_tokens=model_input.max_output_tokens,
    )


def openai_messages(model_input: ModelInput) -> list[OpenAIMessage]:
    """Convert ordered provider-neutral messages into OpenAI messages."""
    messages: list[OpenAIMessage] = [OpenAIMessage(role="system", content=model_input.instructions)]
    for message in model_input.messages:
        if isinstance(message, UserMessage):
            messages.append(OpenAIMessage(role="user", content=openai_message_content(message)))
        elif isinstance(message, AssistantMessage):
            messages.append(
                OpenAIMessage(
                    role="assistant",
                    content=message.content,
                    refusal=message.refusal,
                    tool_calls=[openai_tool_call(call) for call in message.tool_calls] or None,
                ),
            )
        elif isinstance(message, ToolMessage):
            messages.append(
                OpenAIMessage(
                    role="tool",
                    content=message.content,
                    tool_call_id=message.tool_call_id,
                ),
            )
        elif isinstance(message, SystemMessage):
            raise ValueError("System messages are supplied by ModelInput instructions")
    return messages


def anthropic_messages(model_input: ModelInput) -> list[AnthropicMessage]:
    """Convert ordered provider-neutral messages into Anthropic messages."""
    messages: list[AnthropicMessage] = []
    index = 0
    while index < len(model_input.messages):
        message = model_input.messages[index]
        if isinstance(message, UserMessage):
            messages.append(AnthropicMessage(role="user", content=anthropic_message_content(message)))
            index += 1
        elif isinstance(message, AssistantMessage):
            blocks: list[JsonObject] = []
            if message.content:
                blocks.append({"type": "text", "text": message.content})
            if message.refusal:
                blocks.append({"type": "text", "text": message.refusal})
            for call in message.tool_calls:
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": call.id,
                        "name": call.name,
                        "input": tool_call_arguments(call),
                    },
                )
            assistant_content: str | list[JsonObject] = blocks if blocks else ""
            messages.append(AnthropicMessage(role="assistant", content=assistant_content))
            index += 1
        elif isinstance(message, ToolMessage):
            results: list[JsonObject] = []
            while index < len(model_input.messages) and isinstance(model_input.messages[index], ToolMessage):
                result = model_input.messages[index]
                if not isinstance(result, ToolMessage):
                    raise RuntimeError("Tool message narrowing failed")
                results.append({"type": "tool_result", "tool_use_id": result.tool_call_id, "content": result.content})
                index += 1
            messages.append(AnthropicMessage(role="user", content=results))
        else:
            raise ValueError("System messages are supplied by ModelInput instructions")
    return messages


def openai_message_content(message: UserMessage) -> str | list[JsonValue]:
    """Project a Message's original content and blocks for OpenAI."""
    if isinstance(message.content, str):
        return message.content
    return [block.model_dump(mode="json") for block in message.content]


def anthropic_message_content(message: UserMessage) -> str | list[JsonObject]:
    """Project a Message's original content and blocks for Anthropic."""
    if isinstance(message.content, str):
        return message.content
    return [block.model_dump(mode="json") for block in message.content]


def tool_call_arguments(message: ToolCall) -> JsonObject:
    """Return parsed Tool Call arguments without using truthiness as a fallback."""
    return parse_raw_arguments(message.arguments)


def openai_tool(tool: Tool) -> OpenAITool:
    """Project one provider-neutral Tool into OpenAI's function shape."""
    return OpenAITool(
        function=OpenAIFunctionDefinition(
            name=tool.name,
            description=tool.description,
            parameters=tool.input_schema,
            strict=tool.strict,
        ),
    )


def anthropic_tool(tool: Tool) -> AnthropicTool:
    """Project one provider-neutral Tool into Anthropic's tool shape."""
    return AnthropicTool(name=tool.name, description=tool.description, input_schema=tool.input_schema)


def openai_tool_call(message: ToolCall) -> OpenAIToolCall:
    """Project one provider-neutral Tool Call into OpenAI's assistant shape."""
    return OpenAIToolCall(
        id=message.id,
        function=OpenAIFunctionCall(name=message.name, arguments=message.arguments),
    )


def parse_raw_arguments(raw_arguments: str | None) -> JsonObject:
    """Parse Tool Call arguments or fail at the protocol boundary."""
    if raw_arguments is None:
        return {}
    value: object = json.loads(raw_arguments)
    if not isinstance(value, dict):
        raise ValueError("Tool Call arguments must be a JSON object")
    return thaw_json_object(value)


class OpenAIStreamAccumulator:
    """Accumulate one OpenAI stream without retaining provider-private payloads."""

    def __init__(self, model: str, provider: str) -> None:
        self.model = model
        self.provider = provider
        self.response_id: str | None = None
        self.text = ""
        self.thinking = ""
        self.refusal: str | None = None
        self.calls: dict[int, StreamToolCallState] = {}
        self.finish_reason: str | None = None
        self.usage = ModelUsage()

    def add(self, chunk: OpenAIStreamChunk) -> None:
        """Accumulate normalized fields from one OpenAI stream chunk."""
        if self.response_id is None:
            self.response_id = chunk.id
        for choice in chunk.choices:
            self.finish_reason = choice.finish_reason or self.finish_reason
            if choice.delta.content:
                self.text += choice.delta.content
            if choice.delta.refusal is not None:
                self.refusal = (self.refusal or "") + choice.delta.refusal
            if choice.delta.reasoning_content:
                self.thinking += choice.delta.reasoning_content
            for call in choice.delta.tool_calls:
                fields = self.calls.setdefault(call.index, StreamToolCallState())
                if call.id is not None:
                    fields.call_id = call.id
                if call.function is not None and call.function.name is not None:
                    fields.name = call.function.name
                if call.function is not None and call.function.arguments is not None:
                    fields.arguments += call.function.arguments
        if chunk.usage is not None:
            input_tokens = (
                chunk.usage.prompt_tokens if chunk.usage.prompt_tokens is not None else self.usage.input_tokens
            )
            output_tokens = (
                chunk.usage.completion_tokens if chunk.usage.completion_tokens is not None else self.usage.output_tokens
            )
            total_tokens = chunk.usage.total_tokens
            if total_tokens is None:
                total_tokens = sum_tokens(input_tokens, output_tokens)
            if total_tokens is None:
                total_tokens = self.usage.total_tokens
            self.usage = ModelUsage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=total_tokens,
                reasoning_tokens=chunk.usage.reasoning_tokens
                if chunk.usage.reasoning_tokens is not None
                else self.usage.reasoning_tokens,
                cached_input_tokens=chunk.usage.cached_input_tokens
                if chunk.usage.cached_input_tokens is not None
                else self.usage.cached_input_tokens,
            )

    def result(self) -> ModelResult:
        """Build the normalized result at stream completion."""
        calls = tuple(fields.to_call() for index, fields in sorted(self.calls.items()))
        return ModelResult(
            model=self.model,
            provider=self.provider,
            response_id=self.response_id,
            thinking=self.thinking,
            message=AssistantMessage(content=self.text or None, tool_calls=calls, refusal=self.refusal),
            structured_output=parse_json_value(self.text),
            finish_reason=openai_finish_reason(self.finish_reason, calls),
            usage=self.usage,
        )


class AnthropicStreamAccumulator:
    """Accumulate one Anthropic stream without retaining provider-private payloads."""

    def __init__(self, model: str, provider: str) -> None:
        self.model = model
        self.provider = provider
        self.response_id: str | None = None
        self.text = ""
        self.thinking = ""
        self.calls: dict[int, StreamToolCallState] = {}
        self.stop_reason: str | None = None
        self.usage = ModelUsage()

    def add(self, event: AnthropicStreamEvent) -> None:
        """Accumulate normalized fields from one Anthropic stream event."""
        if event.message is not None:
            self.response_id = event.message.id
            self.stop_reason = event.message.stop_reason
            if event.message.usage is not None:
                self.usage = ModelUsage(
                    input_tokens=event.message.usage.input_tokens,
                    output_tokens=None,
                    total_tokens=None,
                    cached_input_tokens=event.message.usage.cache_read_input_tokens,
                )
        if event.content_block is not None and event.content_block.type == "tool_use" and event.index is not None:
            fields = self.calls.setdefault(event.index, StreamToolCallState())
            fields.call_id = event.content_block.id
            fields.name = event.content_block.name
            if event.content_block.input is not None:
                fields.initial_arguments = json.dumps(event.content_block.input, sort_keys=True)
        if event.usage is not None:
            input_tokens = event.usage.input_tokens if event.usage.input_tokens is not None else self.usage.input_tokens
            cached_input_tokens = (
                event.usage.cache_read_input_tokens
                if event.usage.cache_read_input_tokens is not None
                else self.usage.cached_input_tokens
            )
            self.usage = ModelUsage(
                input_tokens=input_tokens,
                output_tokens=event.usage.output_tokens
                if event.usage.output_tokens is not None
                else self.usage.output_tokens,
                total_tokens=sum_tokens(
                    input_tokens,
                    event.usage.output_tokens if event.usage.output_tokens is not None else self.usage.output_tokens,
                ),
                cached_input_tokens=cached_input_tokens,
            )
        if event.type == "message_delta" and event.delta is not None:
            stop_reason = event.delta.get("stop_reason")
            if isinstance(stop_reason, str):
                self.stop_reason = stop_reason
        if event.delta is None or event.index is None:
            return
        delta_type = event.delta.get("type")
        text = event.delta.get("text")
        thinking = event.delta.get("thinking")
        partial_json = event.delta.get("partial_json")
        if delta_type == "text_delta" and isinstance(text, str):
            self.text += text
        elif delta_type == "thinking_delta" and isinstance(thinking, str):
            self.thinking += thinking
        elif delta_type == "input_json_delta" and isinstance(partial_json, str):
            fields = self.calls.setdefault(event.index, StreamToolCallState())
            fields.arguments += partial_json

    def result(self) -> ModelResult:
        """Build the normalized result at stream completion."""
        calls = tuple(fields.to_call() for index, fields in sorted(self.calls.items()))
        finish_reason = anthropic_finish_reason(self.stop_reason, bool(calls))
        return ModelResult(
            model=self.model,
            provider=self.provider,
            response_id=self.response_id,
            thinking=self.thinking,
            message=AssistantMessage(content=self.text or None, tool_calls=calls),
            structured_output=parse_json_value(self.text),
            finish_reason=finish_reason,
            usage=self.usage,
        )


def parse_arguments(arguments: str) -> JsonObject | None:
    """Parse streamed Tool arguments while preserving malformed raw text."""
    try:
        value: object = json.loads(arguments)
    except json.JSONDecodeError:
        return None
    if not isinstance(value, dict):
        return None
    return thaw_json_object(value)


def parse_json_value(content: str) -> JsonValue | None:
    """Parse structured text when it contains a JSON value."""
    try:
        value: object = json.loads(content)
    except json.JSONDecodeError:
        return None
    return json_value(value)


def json_value(value: object) -> JsonValue:
    """Validate a decoded value against the SDK JSON contract."""
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, list):
        return [json_value(item) for item in value]
    if isinstance(value, dict):
        return {key: json_value(item) for key, item in value.items()}
    raise ValueError("structured output must be JSON serializable")
