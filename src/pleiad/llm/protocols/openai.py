"""OpenAI SDK protocol with explicit provider-specific request conversion."""

import json
import logging
from collections.abc import AsyncIterator, Mapping
from dataclasses import replace
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlparse

from openai import AsyncOpenAI, OpenAIError

from pleiad.llm.capabilities import LLMCapabilities, require_capabilities
from pleiad.llm.client import LLMStreamEvent, LLMStreamEventKind
from pleiad.llm.config import LLMConfig, LLMProtocol, LLMProvider
from pleiad.llm.contracts import (
    FinishReason,
    JsonObject,
    LLMRequest,
    Message,
    MessageRole,
    ResponseFormatType,
    ToolDefinition,
    Usage,
)
from pleiad.llm.errors import LLMError, LLMErrorCode
from pleiad.llm.payload_logging import capture_payload, log_raw_request, log_raw_response
from pleiad.llm.registry import ProviderProfile
from pleiad.llm.response.envelope import LLMResponse, RawResponseItem
from pleiad.llm.response.parts import (
    RefusalPart,
    ResponsePart,
    TextPart,
    ThinkingPart,
    ToolCallPart,
    UnknownPart,
)

logger = logging.getLogger(__name__)


class OpenAIClient:
    """Call OpenAI and OpenAI-compatible providers through `AsyncOpenAI`."""

    def __init__(
        self,
        config: LLMConfig,
        profile: ProviderProfile,
        sdk_client: AsyncOpenAI | None = None,
    ) -> None:
        """Create an SDK-backed OpenAI protocol client."""
        if profile.protocol is not LLMProtocol.OPENAI:
            raise ValueError("OpenAIClient requires an openai protocol profile")
        self._config = config
        self._profile = _effective_profile(config, profile)
        self._client = sdk_client or AsyncOpenAI(
            api_key=config.api_key.get_secret_value(),
            base_url=config.base_url or profile.default_base_url,
            timeout=config.timeout_seconds,
            max_retries=config.max_retries,
            default_headers=_headers(config, profile),
        )

    @property
    def capabilities(self) -> LLMCapabilities:
        """Return the selected provider capabilities."""
        return self._profile.capabilities

    @property
    def protocol(self) -> LLMProtocol:
        """Return the public protocol name."""
        return self._profile.protocol

    @property
    def provider(self) -> LLMProvider:
        """Return the selected provider name."""
        return self._profile.provider

    @property
    def model(self) -> str:
        """Return the concrete model configured for this client."""
        return self._config.model

    async def complete(self, request: LLMRequest) -> LLMResponse:
        """Convert a request, call the official SDK, and normalize its response."""
        if request.stream:
            raise LLMError(
                LLMErrorCode.INVALID_REQUEST,
                "streaming requests must use the stream method",
                retryable=False,
                protocol=self.protocol,
                provider=self.provider,
            )
        require_capabilities(
            request,
            self.capabilities,
            protocol=self.protocol,
            provider=self.provider,
        )
        options = _provider_options(self._config, request, self._profile)
        params = _request_params(request, self._profile, options)
        log_raw_request(
            logger,
            request_id=request.request_id,
            protocol=self.protocol,
            provider=self.provider,
            body=params,
        )
        try:
            if self._profile.wire_mode == "responses":
                raw = await self._client.responses.create(**params)
            else:
                raw = await self._client.chat.completions.create(**params)
            log_raw_response(
                logger,
                request_id=request.request_id,
                protocol=self.protocol,
                provider=self.provider,
                body=raw,
            )
            if self._profile.wire_mode == "responses":
                return _parse_responses(raw, request, self._profile)
            return _parse_chat(raw, request, self._profile)
        except OpenAIError as error:
            log_raw_response(
                logger,
                request_id=request.request_id,
                protocol=self.protocol,
                provider=self.provider,
                body=_error_payload(error),
            )
            raise _normalize_error(error, self._profile) from error
        except (TypeError, ValueError) as error:
            raise _malformed(self._profile, "provider returned an invalid response") from error

    async def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        """Stream normalized text, thinking, and the completed provider response."""
        if not request.stream:
            request = request.model_copy(update={"stream": True})
        require_capabilities(
            request,
            self.capabilities,
            protocol=self.protocol,
            provider=self.provider,
        )
        options = _provider_options(self._config, request, self._profile)
        params = _request_params(request, self._profile, options)
        log_raw_request(
            logger,
            request_id=request.request_id,
            protocol=self.protocol,
            provider=self.provider,
            body=params,
        )
        try:
            if self._profile.wire_mode == "responses":
                async for event in self._stream_responses(params, request):
                    yield event
            else:
                async for event in self._stream_chat(params, request):
                    yield event
        except OpenAIError as error:
            log_raw_response(
                logger,
                request_id=request.request_id,
                protocol=self.protocol,
                provider=self.provider,
                body=_error_payload(error),
            )
            raise _normalize_error(error, self._profile) from error
        except (TypeError, ValueError) as error:
            raise _malformed(self._profile, "provider returned an invalid stream") from error

    async def _stream_responses(
        self,
        params: dict[str, Any],
        request: LLMRequest,
    ) -> AsyncIterator[LLMStreamEvent]:
        """Translate OpenAI Responses stream events into the neutral contract."""
        raw_stream = await self._client.responses.create(**params)
        completed: object | None = None
        raw_events: list[object] | None = [] if logger.isEnabledFor(logging.DEBUG) else None
        async for event in raw_stream:
            captured = capture_payload(logger, event)
            if raw_events is not None and captured is not None:
                raw_events.append(captured)
            event_type = _string(getattr(event, "type", None))
            if event_type == "response.output_text.delta":
                delta = _string(getattr(event, "delta", None))
                if delta:
                    yield LLMStreamEvent(LLMStreamEventKind.TEXT_DELTA, text=delta)
            elif "reasoning" in event_type and event_type.endswith(".delta"):
                delta = _string(getattr(event, "delta", None))
                if delta:
                    yield LLMStreamEvent(LLMStreamEventKind.THINKING_DELTA, text=delta)
            elif event_type == "response.completed":
                completed = getattr(event, "response", None)
        if completed is None:
            raise _malformed(self._profile, "provider stream ended without a completed response")
        log_raw_response(
            logger,
            request_id=request.request_id,
            protocol=self.protocol,
            provider=self.provider,
            body={
                "stream": True,
                "events": raw_events or [],
                "completed": capture_payload(logger, completed),
            },
        )
        yield LLMStreamEvent(
            LLMStreamEventKind.COMPLETED,
            response=_parse_responses(completed, request, self._profile),
        )

    async def _stream_chat(
        self,
        params: dict[str, Any],
        request: LLMRequest,
    ) -> AsyncIterator[LLMStreamEvent]:
        """Translate Chat Completions chunks and rebuild the normalized response."""
        raw_stream = await self._client.chat.completions.create(**params)
        text_parts: list[str] = []
        thinking_parts: list[str] = []
        tool_calls: dict[int, dict[str, str]] = {}
        response_id = ""
        model = request.model
        finish_reason: object | None = None
        usage: object | None = None
        content_buffer = ""
        content_is_thinking = False
        raw_chunks: list[object] | None = [] if logger.isEnabledFor(logging.DEBUG) else None
        async for chunk in raw_stream:
            captured = capture_payload(logger, chunk)
            if raw_chunks is not None and captured is not None:
                raw_chunks.append(captured)
            response_id = response_id or _string(getattr(chunk, "id", None))
            model = _string(getattr(chunk, "model", None)) or model
            usage = getattr(chunk, "usage", None) or usage
            choices = getattr(chunk, "choices", ()) or ()
            if not choices:
                continue
            choice = choices[0]
            finish_reason = getattr(choice, "finish_reason", None) or finish_reason
            delta = getattr(choice, "delta", None)
            if delta is None:
                continue
            text = _string(getattr(delta, "content", None))
            if text:
                content_events, content_buffer, content_is_thinking = _split_stream_content(
                    text,
                    content_buffer=content_buffer,
                    content_is_thinking=content_is_thinking,
                )
                for content_kind, fragment in content_events:
                    if content_kind == "thinking":
                        thinking_parts.append(fragment)
                        yield LLMStreamEvent(
                            LLMStreamEventKind.THINKING_DELTA,
                            text=fragment,
                        )
                    else:
                        text_parts.append(fragment)
                        yield LLMStreamEvent(
                            LLMStreamEventKind.TEXT_DELTA,
                            text=fragment,
                        )
            thinking = _thinking_value(delta, self._profile)
            if thinking:
                thinking_parts.append(thinking)
                yield LLMStreamEvent(LLMStreamEventKind.THINKING_DELTA, text=thinking)
            for call_delta in getattr(delta, "tool_calls", ()) or ():
                index = getattr(call_delta, "index", None)
                index = index if isinstance(index, int) else len(tool_calls)
                accumulated = tool_calls.setdefault(
                    index,
                    {"id": "", "name": "", "arguments": ""},
                )
                call_id = _string(getattr(call_delta, "id", None))
                if call_id:
                    accumulated["id"] = call_id
                function = getattr(call_delta, "function", None)
                name = _string(getattr(function, "name", None))
                arguments = _string(getattr(function, "arguments", None))
                if name:
                    accumulated["name"] = name
                if arguments:
                    accumulated["arguments"] += arguments
                    yield LLMStreamEvent(
                        LLMStreamEventKind.TOOL_CALL_DELTA,
                        tool_call_index=index,
                        tool_call_id=accumulated["id"] or None,
                        tool_name=accumulated["name"] or None,
                        arguments_delta=arguments,
                    )
        if content_buffer:
            if content_is_thinking:
                thinking_parts.append(content_buffer)
                yield LLMStreamEvent(
                    LLMStreamEventKind.THINKING_DELTA,
                    text=content_buffer,
                )
            else:
                text_parts.append(content_buffer)
                yield LLMStreamEvent(
                    LLMStreamEventKind.TEXT_DELTA,
                    text=content_buffer,
                )
        message = SimpleNamespace(
            content="".join(text_parts) or None,
            refusal=None,
            tool_calls=[
                SimpleNamespace(
                    id=call["id"],
                    type="function",
                    function=SimpleNamespace(
                        name=call["name"],
                        arguments=call["arguments"],
                    ),
                )
                for _index, call in sorted(tool_calls.items())
            ],
        )
        thinking_fields = self._profile.request_options.get(
            "thinking_response_fields",
            ("reasoning_content",),
        )
        thinking_field = (
            str(thinking_fields[0])
            if isinstance(thinking_fields, (tuple, list))
            else "reasoning_content"
        )
        setattr(message, thinking_field, "".join(thinking_parts) or None)
        raw = SimpleNamespace(
            id=response_id or None,
            model=model,
            choices=[SimpleNamespace(message=message, finish_reason=finish_reason)],
            usage=usage,
        )
        log_raw_response(
            logger,
            request_id=request.request_id,
            protocol=self.protocol,
            provider=self.provider,
            body={"stream": True, "chunks": raw_chunks or []},
        )
        yield LLMStreamEvent(
            LLMStreamEventKind.COMPLETED,
            response=_parse_chat(raw, request, self._profile),
        )

    async def aclose(self) -> None:
        """Close the underlying official SDK client."""
        await self._client.close()


def _headers(config: LLMConfig, profile: ProviderProfile) -> dict[str, str]:
    """Combine provider and user headers without exposing credentials."""
    headers = dict(profile.default_headers)
    headers.update(config.default_headers)
    return headers


def _effective_profile(config: LLMConfig, profile: ProviderProfile) -> ProviderProfile:
    """Derive endpoint-dependent capabilities without mutating the registry profile."""
    if profile.provider is not LLMProvider.DEEPSEEK:
        return profile
    endpoint = config.base_url or profile.default_base_url
    path = urlparse(endpoint).path.rstrip("/") if endpoint else ""
    if path != "/beta":
        return profile
    return replace(
        profile,
        capabilities=replace(profile.capabilities, strict_tool_calling=True),
    )


def _provider_options(
    config: LLMConfig,
    request: LLMRequest,
    profile: ProviderProfile,
) -> dict[str, Any]:
    """Validate and merge documented provider-native options."""
    merged = dict(config.provider_options)
    merged.update(request.provider_options)
    unknown = set(merged) - set(profile.allowed_provider_options)
    if unknown:
        names = ", ".join(sorted(unknown))
        raise LLMError(
            LLMErrorCode.INVALID_REQUEST,
            f"unsupported provider options for {profile.provider}: {names}",
            retryable=False,
            protocol=profile.protocol,
            provider=profile.provider,
        )
    return merged


def _request_params(
    request: LLMRequest,
    profile: ProviderProfile,
    provider_options: Mapping[str, Any],
) -> dict[str, Any]:
    """Build official SDK keyword arguments from canonical request fields."""
    if profile.wire_mode == "responses":
        return _responses_params(request, profile, provider_options)
    return _chat_params(request, profile, provider_options)


def _responses_params(
    request: LLMRequest,
    profile: ProviderProfile,
    provider_options: Mapping[str, Any],
) -> dict[str, Any]:
    """Map canonical fields to the OpenAI Responses SDK."""
    params: dict[str, Any] = {
        "model": request.model,
        "input": _responses_messages(request.messages),
        "store": False,
        "stream": request.stream,
    }
    if request.max_output_tokens is not None:
        params["max_output_tokens"] = request.max_output_tokens
    if request.temperature is not None:
        params["temperature"] = request.temperature
    if request.tools:
        params["tools"] = [_responses_tool(tool) for tool in request.tools]
    if request.tool_choice is not None:
        params["tool_choice"] = request.tool_choice
    if request.thinking.enabled:
        params["reasoning"] = {"effort": request.thinking.effort or "medium"}
    _apply_response_format(params, request, "responses")
    if request.metadata:
        params["metadata"] = request.metadata
    if provider_options:
        params["extra_body"] = dict(provider_options)
    return params


def _chat_params(
    request: LLMRequest,
    profile: ProviderProfile,
    provider_options: Mapping[str, Any],
) -> dict[str, Any]:
    """Map canonical fields to Chat Completions and provider extensions."""
    params: dict[str, Any] = {
        "model": request.model,
        "messages": [_chat_message(message, profile) for message in request.messages],
        "stream": request.stream,
    }
    max_tokens_field = str(profile.request_options.get("max_tokens_field", "max_completion_tokens"))
    if request.max_output_tokens is not None:
        params[max_tokens_field] = request.max_output_tokens
    if request.temperature is not None:
        params["temperature"] = request.temperature
    if request.tools:
        params["tools"] = [_chat_tool(tool, profile) for tool in request.tools]
    if request.tool_choice is not None:
        params["tool_choice"] = request.tool_choice

    extra_body = dict(provider_options)
    thinking_mode = str(profile.request_options.get("thinking_mode", "none"))
    if request.thinking.enabled:
        if thinking_mode == "reasoning_effort":
            params["reasoning_effort"] = request.thinking.effort or "medium"
        elif thinking_mode == "deepseek":
            params["reasoning_effort"] = _deepseek_effort(request)
            extra_body.setdefault("thinking", {"type": "enabled"})
        elif thinking_mode in {"qwen", "glm"}:
            extra_body["enable_thinking"] = True
            if request.thinking.budget_tokens is not None:
                extra_body["thinking_budget"] = request.thinking.budget_tokens
        elif thinking_mode == "minimax":
            extra_body.setdefault("reasoning_split", True)
    if request.response_format.type is ResponseFormatType.JSON_OBJECT:
        params["response_format"] = {"type": "json_object"}
    elif request.response_format.type is ResponseFormatType.JSON_SCHEMA:
        params["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": request.response_format.name or "pleiad_output",
                "strict": request.response_format.strict,
                "schema": request.response_format.json_schema,
            },
        }
    if request.metadata:
        params["metadata"] = request.metadata
    if extra_body:
        params["extra_body"] = extra_body
    return params


def _deepseek_effort(request: LLMRequest) -> str:
    """Map canonical effort to DeepSeek's supported values."""
    effort = request.thinking.effort or "high"
    return "max" if effort in {"max", "high"} else "high"


def _chat_message(message: Message, profile: ProviderProfile) -> dict[str, Any]:
    """Convert one message into Chat Completions shape, preserving thinking."""
    if message.role is MessageRole.TOOL:
        return {
            "role": "tool",
            "content": message.content or "",
            "tool_call_id": message.tool_call_id,
        }
    payload: dict[str, Any] = {
        "role": message.role.value,
        "content": message.content,
    }
    if message.name is not None:
        payload["name"] = message.name
    if message.role is MessageRole.ASSISTANT and message.tool_calls:
        payload["tool_calls"] = [_chat_tool_call(call) for call in message.tool_calls]
    if message.role is MessageRole.ASSISTANT and message.thinking:
        thinking_field = str(profile.request_options.get("thinking_field", "reasoning_content"))
        payload[thinking_field] = message.thinking
    request_fields = message.provider_data.get("request_fields")
    if isinstance(request_fields, dict):
        payload.update(request_fields)
    return payload


def _chat_tool(tool: ToolDefinition, profile: ProviderProfile) -> dict[str, Any]:
    """Convert one canonical tool definition into Chat Completions shape."""
    function: dict[str, Any] = {
        "name": tool.name,
        "description": tool.description,
        "parameters": tool.input_schema,
    }
    if tool.strict and profile.capabilities.strict_tool_calling:
        function["strict"] = True
    return {
        "type": "function",
        "function": function,
    }


def _chat_tool_call(call: object) -> dict[str, Any]:
    """Convert one replayed canonical tool call into SDK input shape."""
    call_id = getattr(call, "call_id", None)
    name = getattr(call, "name", None)
    raw_arguments = getattr(call, "raw_arguments", None)
    arguments = getattr(call, "arguments", None)
    encoded = raw_arguments or json.dumps(arguments or {}, separators=(",", ":"))
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": encoded},
    }


def _responses_tool(tool: ToolDefinition) -> dict[str, Any]:
    """Convert one canonical tool definition into Responses shape."""
    return {
        "type": "function",
        "name": tool.name,
        "description": tool.description,
        "parameters": tool.input_schema,
        "strict": tool.strict,
    }


def _apply_response_format(
    params: dict[str, Any],
    request: LLMRequest,
    wire_mode: str,
) -> None:
    """Apply the native Responses output format."""
    del wire_mode
    if request.response_format.type is ResponseFormatType.JSON_SCHEMA:
        params["text"] = {
            "format": {
                "type": "json_schema",
                "name": request.response_format.name or "pleiad_output",
                "strict": request.response_format.strict,
                "schema": request.response_format.json_schema,
            },
        }


def _responses_messages(messages: tuple[Message, ...]) -> list[dict[str, Any]]:
    """Convert neutral messages into Responses input items."""
    result: list[dict[str, Any]] = []
    for message in messages:
        raw_items = message.provider_data.get("raw_items")
        if message.role is MessageRole.ASSISTANT and isinstance(raw_items, list):
            result.extend(item for item in raw_items if isinstance(item, dict))
            continue
        if message.role is MessageRole.TOOL:
            result.append(
                {
                    "type": "function_call_output",
                    "call_id": message.tool_call_id,
                    "output": message.content or "",
                },
            )
            continue
        result.append({"role": message.role.value, "content": message.content or ""})
    return result


def _parse_responses(raw: object, request: LLMRequest, profile: ProviderProfile) -> LLMResponse:
    """Normalize a Responses SDK object into ordered response parts."""
    parts: list[ResponsePart] = []
    raw_items: list[RawResponseItem] = []
    output = getattr(raw, "output", ()) or ()
    for item in output:
        item_type = _string(getattr(item, "type", None))
        payload = _dump(item)
        raw_items.append(
            RawResponseItem(
                provider_type=item_type or "unknown",
                item_id=_string(getattr(item, "id", None)) or None,
                payload=payload,
            ),
        )
        if item_type == "reasoning":
            parts.append(ThinkingPart(text=_reasoning_text(item), provider_data=payload))
        elif item_type == "function_call":
            arguments, raw_arguments = _parse_arguments(getattr(item, "arguments", None))
            parts.append(
                ToolCallPart(
                    call_id=_required(getattr(item, "call_id", None), "call_id"),
                    name=_required(getattr(item, "name", None), "tool name"),
                    arguments=arguments,
                    raw_arguments=raw_arguments,
                    provider_data=payload,
                ),
            )
        elif item_type == "message":
            parts.extend(_response_message_parts(item))
        else:
            parts.append(UnknownPart(provider_type=item_type or "unknown", payload=payload))
    if not any(isinstance(part, TextPart) for part in parts):
        output_text = _string(getattr(raw, "output_text", None))
        if output_text:
            parts.append(TextPart(text=output_text))
    return LLMResponse(
        request_id=request.request_id,
        protocol=profile.protocol,
        provider=profile.provider,
        model=_string(getattr(raw, "model", None)) or request.model,
        response_id=_string(getattr(raw, "id", None)) or None,
        parts=tuple(parts),
        structured_output=_structured_output(request, parts, profile),
        usage=_usage(getattr(raw, "usage", None)),
        finish_reason=_finish(
            getattr(raw, "status", None),
            any(isinstance(part, ToolCallPart) for part in parts),
        ),
        raw_items=tuple(raw_items),
        provider_metadata={"response": _dump(raw)},
    )


def _parse_chat(raw: object, request: LLMRequest, profile: ProviderProfile) -> LLMResponse:
    """Normalize a Chat Completions SDK object into ordered response parts."""
    choices = getattr(raw, "choices", ()) or ()
    if not choices:
        raise _malformed(profile, "provider response has no choices")
    message = getattr(choices[0], "message", None)
    if message is None:
        raise _malformed(profile, "provider response has no message")
    parts: list[ResponsePart] = []
    text = _string(getattr(message, "content", None))
    thinking = _thinking_value(message, profile)
    if thinking:
        parts.append(ThinkingPart(text=thinking))
    tagged_thinking, tagged_text = _split_think_tags(text)
    if tagged_thinking:
        parts.append(ThinkingPart(text=tagged_thinking))
        text = tagged_text
    if text:
        parts.append(TextPart(text=text))
    refusal = _string(getattr(message, "refusal", None))
    if refusal:
        parts.append(RefusalPart(refusal=refusal))
    for call in getattr(message, "tool_calls", ()) or ():
        function = getattr(call, "function", None)
        arguments, raw_arguments = _parse_arguments(getattr(function, "arguments", None))
        parts.append(
            ToolCallPart(
                call_id=_required(getattr(call, "id", None), "tool call id"),
                name=_required(getattr(function, "name", None), "tool name"),
                arguments=arguments,
                raw_arguments=raw_arguments,
                provider_data=_dump(call),
            ),
        )
    raw_message = _dump(message)
    raw_items = (
        RawResponseItem(
            provider_type="chat.completion.message",
            item_id=_string(getattr(raw, "id", None)) or None,
            payload=raw_message,
        ),
    )
    return LLMResponse(
        request_id=request.request_id,
        protocol=profile.protocol,
        provider=profile.provider,
        model=_string(getattr(raw, "model", None)) or request.model,
        response_id=_string(getattr(raw, "id", None)) or None,
        parts=tuple(parts),
        structured_output=_structured_output(request, parts, profile),
        usage=_usage(getattr(raw, "usage", None)),
        finish_reason=_finish(
            getattr(choices[0], "finish_reason", None),
            any(isinstance(part, ToolCallPart) for part in parts),
        ),
        raw_items=raw_items,
        provider_metadata={"assistant_message": raw_message, "response": _dump(raw)},
    )


def _response_message_parts(message: object) -> list[ResponsePart]:
    """Normalize one Responses message item."""
    parts: list[ResponsePart] = []
    for content in getattr(message, "content", ()) or ():
        content_type = _string(getattr(content, "type", None))
        payload = _dump(content)
        if content_type == "output_text":
            parts.append(
                TextPart(text=_string(getattr(content, "text", None)), provider_data=payload),
            )
        elif content_type == "refusal":
            parts.append(RefusalPart(refusal=_string(getattr(content, "refusal", None))))
        else:
            parts.append(UnknownPart(provider_type=content_type or "unknown", payload=payload))
    return parts


def _thinking_value(message: object, profile: ProviderProfile) -> str:
    """Read a provider-specific reasoning field from a Chat message."""
    fields = profile.request_options.get("thinking_response_fields", ("reasoning_content",))
    if not isinstance(fields, (tuple, list)):
        fields = ("reasoning_content",)
    for field in fields:
        value = _string(getattr(message, str(field), None))
        if value:
            return value
    return ""


def _split_stream_content(
    value: str,
    *,
    content_buffer: str,
    content_is_thinking: bool,
) -> tuple[list[tuple[str, str]], str, bool]:
    """Route streamed ``<think>`` tags away from visible assistant text."""
    buffer = content_buffer + value
    events: list[tuple[str, str]] = []
    thinking = content_is_thinking
    while buffer:
        marker = "</think>" if thinking else "<think>"
        marker_index = buffer.find(marker)
        if marker_index >= 0:
            fragment = buffer[:marker_index]
            if fragment:
                events.append(("thinking" if thinking else "text", fragment))
            buffer = buffer[marker_index + len(marker) :]
            thinking = not thinking
            continue
        hold = _partial_marker_length(buffer, marker)
        safe = buffer[:-hold] if hold else buffer
        if safe:
            events.append(("thinking" if thinking else "text", safe))
            buffer = buffer[len(safe) :]
        break
    return events, buffer, thinking


def _partial_marker_length(value: str, marker: str) -> int:
    """Keep a marker prefix that may be completed by the next stream chunk."""
    upper_bound = min(len(value), len(marker) - 1)
    for length in range(upper_bound, 0, -1):
        if value.endswith(marker[:length]):
            return length
    return 0


def _split_think_tags(value: str) -> tuple[str, str]:
    """Separate MiniMax-style think tags when the provider embeds them in text."""
    start = value.find("<think>")
    end = value.find("</think>")
    if start < 0 or end < start:
        return "", value
    thinking = value[start + len("<think>") : end].strip()
    text = (value[:start] + value[end + len("</think>") :]).strip()
    return thinking, text


def _structured_output(
    request: LLMRequest,
    parts: list[ResponsePart],
    profile: ProviderProfile,
) -> object | None:
    """Decode JSON output only when the canonical request asked for it."""
    if request.response_format.type not in {
        ResponseFormatType.JSON_SCHEMA,
        ResponseFormatType.JSON_OBJECT,
    }:
        return None
    if any(isinstance(part, ToolCallPart) for part in parts):
        return None
    text = "".join(part.text for part in parts if isinstance(part, TextPart))
    if not text.strip():
        return None
    try:
        decoded: object = json.loads(text)
        return decoded
    except json.JSONDecodeError as error:
        raise _malformed(
            profile,
            "provider returned invalid structured JSON",
            details={
                "failure": "invalid_json",
                "line": error.lineno,
                "column": error.colno,
                "parser_message": error.msg,
            },
        ) from error


def _usage(value: object) -> Usage:
    """Normalize SDK usage and preserve provider-specific token details."""
    payload = _dump(value)
    input_tokens = _integer(payload.get("input_tokens", payload.get("prompt_tokens")))
    output_tokens = _integer(payload.get("output_tokens", payload.get("completion_tokens")))
    total_tokens = _integer(payload.get("total_tokens")) or input_tokens + output_tokens
    completion_details = payload.get("completion_tokens_details")
    prompt_details = payload.get("prompt_tokens_details")
    reasoning_tokens = _integer(
        completion_details.get("reasoning_tokens")
        if isinstance(completion_details, dict)
        else None,
    )
    cached_tokens = _integer(
        prompt_details.get("cached_tokens") if isinstance(prompt_details, dict) else None,
    )
    return Usage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        reasoning_tokens=reasoning_tokens,
        cached_input_tokens=cached_tokens,
        details=payload,
    )


def _finish(value: object, has_tool_calls: bool) -> FinishReason:
    """Normalize a provider finish reason."""
    if has_tool_calls:
        return FinishReason.TOOL_CALL
    return {
        "stop": FinishReason.STOP,
        "completed": FinishReason.STOP,
        "end_turn": FinishReason.STOP,
        "length": FinishReason.LENGTH,
        "max_tokens": FinishReason.LENGTH,
        "content_filter": FinishReason.CONTENT_FILTER,
    }.get(_string(value), FinishReason.UNKNOWN)


def _parse_arguments(value: object) -> tuple[JsonObject | None, str | None]:
    """Preserve invalid raw arguments while parsing valid JSON objects."""
    if isinstance(value, dict):
        return value, None
    if not isinstance(value, str):
        return None, None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return None, value
    return (parsed, value) if isinstance(parsed, dict) else (None, value)


def _reasoning_text(item: object) -> str | None:
    """Extract textual reasoning summaries when the SDK exposes them."""
    summary = getattr(item, "summary", None)
    if isinstance(summary, list):
        values = [_string(getattr(entry, "text", None)) for entry in summary]
        text = "".join(value for value in values if value)
        return text or None
    return _string(summary) or None


def _dump(value: object) -> dict[str, Any]:
    """Convert an SDK model or mapping to a JSON-compatible object."""
    if value is None:
        return {}
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump(mode="json", exclude_none=True)
        return dumped if isinstance(dumped, dict) else {}
    if isinstance(value, Mapping):
        return dict(value)
    return {}


def _required(value: object, label: str) -> str:
    """Require an identifier in a provider response."""
    result = _string(value)
    if not result:
        raise ValueError(f"provider response is missing {label}")
    return result


def _string(value: object) -> str:
    """Return a string value or an empty string."""
    return value if isinstance(value, str) else ""


def _integer(value: object) -> int:
    """Return a non-negative integer value or zero."""
    return value if isinstance(value, int) and value >= 0 else 0


def _normalize_error(error: OpenAIError, profile: ProviderProfile) -> LLMError:
    """Map official SDK errors to sanitized Pleiad errors."""
    status = getattr(error, "status_code", None)
    if status in {401, 403}:
        code, retryable = LLMErrorCode.AUTHENTICATION, False
    elif status == 429:
        code, retryable = LLMErrorCode.RATE_LIMIT, True
    elif status in {408, 504}:
        code, retryable = LLMErrorCode.TIMEOUT, True
    elif isinstance(status, int) and 400 <= status < 500:
        code, retryable = LLMErrorCode.INVALID_REQUEST, False
    elif status in {500, 502, 503}:
        code, retryable = LLMErrorCode.OVERLOADED, True
    else:
        code, retryable = LLMErrorCode.PROVIDER_ERROR, True
    return LLMError(
        code,
        "The LLM provider request failed.",
        retryable=retryable,
        protocol=profile.protocol,
        provider=profile.provider,
        details=_error_payload(error),
    )


def _error_payload(error: OpenAIError) -> dict[str, Any]:
    """Convert an SDK provider error into a structured diagnostic payload."""
    payload: dict[str, Any] = {
        "error_type": type(error).__name__,
        "status_code": getattr(error, "status_code", None),
    }
    body = getattr(error, "body", None)
    if body is not None:
        payload["body"] = body
    response = getattr(error, "response", None)
    if response is not None:
        response_body = getattr(response, "text", None)
        if isinstance(response_body, str) and response_body:
            payload["response_text"] = response_body
    return payload


def _malformed(
    profile: ProviderProfile,
    message: str,
    *,
    details: dict[str, Any] | None = None,
) -> LLMError:
    """Create one sanitized malformed-response error."""
    return LLMError(
        LLMErrorCode.MALFORMED_RESPONSE,
        message,
        retryable=False,
        protocol=profile.protocol,
        provider=profile.provider,
        details=details,
    )
