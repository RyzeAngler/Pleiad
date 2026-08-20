"""Anthropic SDK protocol with native thinking and structured output support."""

import json
import logging
from collections.abc import AsyncIterator, Mapping
from typing import Any

from anthropic import AnthropicError, AsyncAnthropic

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
    ResponsePart,
    TextPart,
    ThinkingPart,
    ToolCallPart,
    UnknownPart,
)

logger = logging.getLogger(__name__)


class AnthropicClient:
    """Call Anthropic and Anthropic-compatible providers through `AsyncAnthropic`."""

    def __init__(
        self,
        config: LLMConfig,
        profile: ProviderProfile,
        sdk_client: AsyncAnthropic | None = None,
    ) -> None:
        """Create an SDK-backed Anthropic protocol client."""
        if profile.protocol is not LLMProtocol.ANTHROPIC:
            raise ValueError("AnthropicClient requires an anthropic protocol profile")
        self._config = config
        self._profile = profile
        self._client = sdk_client or AsyncAnthropic(
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
        params = _request_params(request, self._config, self._profile)
        log_raw_request(
            logger,
            request_id=request.request_id,
            protocol=self.protocol,
            provider=self.provider,
            body=params,
        )
        try:
            raw = await self._client.messages.create(**params)
            log_raw_response(
                logger,
                request_id=request.request_id,
                protocol=self.protocol,
                provider=self.provider,
                body=raw,
            )
            return _parse_response(raw, request, self._profile)
        except AnthropicError as error:
            raise _normalize_error(error, self._profile) from error
        except (TypeError, ValueError) as error:
            raise _malformed(self._profile, "provider returned an invalid response") from error

    async def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        """Stream native Anthropic blocks and normalize the final Message."""
        if not request.stream:
            request = request.model_copy(update={"stream": True})
        require_capabilities(
            request,
            self.capabilities,
            protocol=self.protocol,
            provider=self.provider,
        )
        params = _request_params(request, self._config, self._profile)
        params.pop("extra_body", None)
        log_raw_request(
            logger,
            request_id=request.request_id,
            protocol=self.protocol,
            provider=self.provider,
            body=params,
        )
        try:
            raw_events: list[object] | None = [] if logger.isEnabledFor(logging.DEBUG) else None
            async with self._client.messages.stream(**params) as stream:
                async for event in stream:
                    captured = capture_payload(logger, event)
                    if raw_events is not None and captured is not None:
                        raw_events.append(captured)
                    event_type = _string(getattr(event, "type", None))
                    if event_type != "content_block_delta":
                        continue
                    delta = getattr(event, "delta", None)
                    delta_type = _string(getattr(delta, "type", None))
                    if delta_type == "text_delta":
                        text = _string(getattr(delta, "text", None))
                        if text:
                            yield LLMStreamEvent(LLMStreamEventKind.TEXT_DELTA, text=text)
                    elif delta_type == "thinking_delta":
                        thinking = _string(getattr(delta, "thinking", None))
                        if thinking:
                            yield LLMStreamEvent(
                                LLMStreamEventKind.THINKING_DELTA,
                                text=thinking,
                            )
                    elif delta_type == "input_json_delta":
                        arguments = _string(getattr(delta, "partial_json", None))
                        if arguments:
                            yield LLMStreamEvent(
                                LLMStreamEventKind.TOOL_CALL_DELTA,
                                arguments_delta=arguments,
                            )
                final = await stream.get_final_message()
            log_raw_response(
                logger,
                request_id=request.request_id,
                protocol=self.protocol,
                provider=self.provider,
                body={
                    "stream": True,
                    "events": raw_events or [],
                    "final": capture_payload(logger, final),
                },
            )
            yield LLMStreamEvent(
                LLMStreamEventKind.COMPLETED,
                response=_parse_response(final, request, self._profile),
            )
        except AnthropicError as error:
            raise _normalize_error(error, self._profile) from error
        except (TypeError, ValueError) as error:
            raise _malformed(self._profile, "provider returned an invalid stream") from error

    async def aclose(self) -> None:
        """Close the underlying official SDK client."""
        await self._client.close()


def _headers(config: LLMConfig, profile: ProviderProfile) -> dict[str, str]:
    """Combine provider and user headers."""
    headers = dict(profile.default_headers)
    headers.update(config.default_headers)
    return headers


def _request_params(
    request: LLMRequest,
    config: LLMConfig,
    profile: ProviderProfile,
) -> dict[str, Any]:
    """Map canonical request fields to Anthropic SDK keyword arguments."""
    options = dict(config.provider_options)
    options.update(request.provider_options)
    unknown = set(options) - set(profile.allowed_provider_options)
    if unknown:
        names = ", ".join(sorted(unknown))
        raise LLMError(
            LLMErrorCode.INVALID_REQUEST,
            f"unsupported provider options for {profile.provider}: {names}",
            retryable=False,
            protocol=profile.protocol,
            provider=profile.provider,
        )
    max_tokens = request.max_output_tokens or int(
        profile.request_options.get("default_max_tokens", 4096),
    )
    params: dict[str, Any] = {
        "model": request.model,
        "messages": [
            _message(message)
            for message in request.messages
            if message.role is not MessageRole.SYSTEM
        ],
        "max_tokens": max_tokens,
    }
    system = [
        message.content or "" for message in request.messages if message.role is MessageRole.SYSTEM
    ]
    if system:
        params["system"] = "\n\n".join(system)
    if request.tools:
        params["tools"] = [_tool(tool) for tool in request.tools]
    if request.tool_choice is not None:
        params["tool_choice"] = _tool_choice(request.tool_choice)
    if request.thinking.enabled:
        budget = request.thinking.budget_tokens or max(1024, max_tokens // 2)
        params["thinking"] = {"type": "enabled", "budget_tokens": budget}
        params["max_tokens"] = max(max_tokens, budget + 1024)
    if request.temperature is not None and not request.thinking.enabled:
        params["temperature"] = request.temperature
    if request.response_format.type is ResponseFormatType.JSON_SCHEMA:
        params["output_config"] = {
            "format": {"type": "json_schema", "schema": request.response_format.json_schema},
        }
    elif request.response_format.type is ResponseFormatType.JSON_OBJECT:
        params["output_config"] = {
            "format": {"type": "json_schema", "schema": {"type": "object"}},
        }
    if request.metadata:
        params["metadata"] = request.metadata
    if options:
        params["extra_body"] = options
    return params


def _message(message: Message) -> dict[str, Any]:
    """Convert one neutral message into an Anthropic message."""
    if message.role is MessageRole.TOOL:
        return {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": message.tool_call_id,
                    "content": message.content or "",
                },
            ],
        }
    raw_items = message.provider_data.get("raw_items")
    if message.role is MessageRole.ASSISTANT and isinstance(raw_items, list):
        replay_blocks = [item for item in raw_items if isinstance(item, dict)]
        if replay_blocks:
            return {"role": "assistant", "content": replay_blocks}
    if message.content_blocks:
        return {"role": message.role.value, "content": list(message.content_blocks)}
    blocks: list[dict[str, Any]] = []
    if message.thinking:
        blocks.append({"type": "thinking", "thinking": message.thinking})
    if message.content:
        blocks.append({"type": "text", "text": message.content})
    blocks.extend(
        {
            "type": "tool_use",
            "id": call.call_id,
            "name": call.name,
            "input": call.arguments or {},
        }
        for call in message.tool_calls
    )
    return {"role": message.role.value, "content": blocks or message.content or ""}


def _tool(tool: ToolDefinition) -> dict[str, Any]:
    """Convert one canonical tool into Anthropic's input_schema shape."""
    return {
        "name": tool.name,
        "description": tool.description or "",
        "input_schema": tool.input_schema,
    }


def _tool_choice(value: str) -> dict[str, str]:
    """Map canonical tool choice values to Anthropic values."""
    if value == "required":
        return {"type": "any"}
    if value == "none":
        return {"type": "none"}
    if value == "auto":
        return {"type": "auto"}
    return {"type": "tool", "name": value}


def _parse_response(raw: object, request: LLMRequest, profile: ProviderProfile) -> LLMResponse:
    """Normalize Anthropic content blocks into ordered response parts."""
    content = getattr(raw, "content", ()) or ()
    parts: list[ResponsePart] = []
    raw_items: list[RawResponseItem] = []
    for block in content:
        block_type = _string(getattr(block, "type", None))
        payload = _dump(block)
        raw_items.append(
            RawResponseItem(
                provider_type=block_type or "unknown",
                item_id=_string(getattr(block, "id", None)) or None,
                payload=payload,
            ),
        )
        if block_type == "text":
            parts.append(
                TextPart(text=_string(getattr(block, "text", None)), provider_data=payload),
            )
        elif block_type == "thinking":
            parts.append(
                ThinkingPart(
                    text=_string(getattr(block, "thinking", None)) or None,
                    signature=_string(getattr(block, "signature", None)) or None,
                    provider_data=payload,
                ),
            )
        elif block_type == "redacted_thinking":
            parts.append(ThinkingPart(redacted=True, replayable=True, provider_data=payload))
        elif block_type == "tool_use":
            parts.append(
                ToolCallPart(
                    call_id=_required(getattr(block, "id", None), "tool call id"),
                    name=_required(getattr(block, "name", None), "tool name"),
                    arguments=_object(getattr(block, "input", None)),
                    provider_data=payload,
                ),
            )
        else:
            parts.append(UnknownPart(provider_type=block_type or "unknown", payload=payload))
    if not content:
        raise _malformed(profile, "provider response has no content blocks")
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
            getattr(raw, "stop_reason", None),
            any(isinstance(part, ToolCallPart) for part in parts),
        ),
        raw_items=tuple(raw_items),
        provider_metadata={"response": _dump(raw)},
    )


def _structured_output(
    request: LLMRequest,
    parts: list[ResponsePart],
    profile: ProviderProfile,
) -> object | None:
    """Decode structured text returned by Anthropic's output configuration."""
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
    """Normalize Anthropic usage, including prompt cache details."""
    payload = _dump(value)
    input_tokens = _integer(payload.get("input_tokens"))
    output_tokens = _integer(payload.get("output_tokens"))
    cached = _integer(payload.get("cache_read_input_tokens"))
    total = input_tokens + output_tokens
    return Usage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total,
        cached_input_tokens=cached,
        details=payload,
    )


def _finish(value: object, has_tool_calls: bool) -> FinishReason:
    """Normalize Anthropic stop reasons."""
    if has_tool_calls:
        return FinishReason.TOOL_CALL
    return {
        "end_turn": FinishReason.STOP,
        "stop_sequence": FinishReason.STOP,
        "max_tokens": FinishReason.LENGTH,
        "pause_turn": FinishReason.STOP,
    }.get(_string(value), FinishReason.UNKNOWN)


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


def _object(value: object) -> JsonObject:
    """Require object-shaped tool input."""
    if isinstance(value, dict):
        return value
    raise ValueError("provider returned non-object tool input")


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


def _normalize_error(error: AnthropicError, profile: ProviderProfile) -> LLMError:
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
    )


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
