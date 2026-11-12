import asyncio
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Mapping
from contextlib import aclosing
from types import TracebackType
from typing import Protocol, runtime_checkable

import aiohttp
from anthropic import AnthropicError, AsyncAnthropic
from dashscope.aigc.generation import AioGeneration
from dashscope.aigc.multimodal_conversation import AioMultiModalConversation
from dashscope.common.error import DashScopeException
from httpx import TimeoutException, TransportError
from pydantic import SecretStr

from pleiad.contracts import JsonObject, JsonValue
from pleiad.errors.base import SystemFault
from pleiad.models.anthropic.request import AnthropicRequest
from pleiad.models.anthropic.response import (
    AnthropicResponse,
    AnthropicStreamEvent,
    parse_event,
)
from pleiad.models.anthropic.response import (
    parse_response as parse_anthropic_response,
)
from pleiad.models.errors import (
    ModelConnectionError,
    ModelProviderUnavailableError,
    ModelRequestError,
    ModelResponseError,
    ModelTimeoutError,
    ModelUnsupportedError,
    classify_provider_error,
)
from pleiad.models.openai.request import OpenAIRequest
from pleiad.models.openai.response import OpenAIResponse, OpenAIStreamChunk, parse_response, parse_stream_chunk


@runtime_checkable
class QwenRawStream(Protocol):
    """Minimal stream surface returned by DashScope's async SDK."""

    def __aiter__(self) -> AsyncIterator[object]:
        """Iterate over DashScope response envelopes."""

    async def aclose(self) -> None:
        """Close the DashScope response stream."""


@runtime_checkable
class AnthropicRawStream(Protocol):
    """Minimal Anthropic SDK stream surface."""

    def __aiter__(self) -> AsyncIterator[object]:
        """Iterate over provider events."""


@runtime_checkable
class AnthropicRawStreamManager(Protocol):
    """Minimal async context manager surface for an Anthropic stream."""

    async def __aenter__(self) -> AnthropicRawStream:
        """Open the provider stream."""

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        """Close the provider stream."""


class OpenAIProvider:
    """Call DashScope's official asynchronous SDK using the OpenAI body contract."""

    def __init__(
        self,
        *,
        model: str,
        api_key: SecretStr,
        base_url: str | None,
        timeout_seconds: float,
        max_retries: int,
        provider_options: Mapping[str, JsonValue],
        headers: Mapping[str, str],
    ) -> None:
        """Create a Qwen SDK operation binding."""
        self.model = model
        self.api_key = api_key
        self.base_url = base_url if base_url is not None else "https://dashscope.aliyuncs.com/api/v1"
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.provider_options = dict(provider_options)
        multimodal = self.provider_options.pop("multimodal", False)
        if not isinstance(multimodal, bool):
            raise ValueError("Qwen multimodal must be a boolean")
        self.multimodal = multimodal
        self.headers = dict(headers)

    async def complete(self, request: OpenAIRequest) -> OpenAIResponse:
        """Convert and send one OpenAI request through DashScope."""
        try:
            async with aclosing(self.responses(request)) as responses:
                value = await anext(responses)
                if not isinstance(value, OpenAIResponse):
                    raise ModelResponseError(
                        "The Qwen provider returned a stream for a complete request.",
                    )
                return value
        except SystemFault:
            raise
        except (AttributeError, KeyError, TypeError, ValueError) as error:
            raise ModelResponseError(
                "The Qwen provider returned an invalid response.",
            ) from error

    async def stream(self, request: OpenAIRequest) -> AsyncIterator[OpenAIStreamChunk]:
        """Convert and stream OpenAI chunks through DashScope."""
        try:
            async with aclosing(self.responses(request)) as responses:
                async for value in responses:
                    if not isinstance(value, OpenAIStreamChunk):
                        raise ModelResponseError(
                            "The Qwen provider returned a complete response in a stream.",
                        )
                    yield value
        except SystemFault:
            raise
        except (AttributeError, KeyError, TypeError, ValueError) as error:
            raise ModelResponseError(
                "The Qwen provider returned an invalid stream.",
            ) from error

    async def responses(self, request: OpenAIRequest) -> AsyncGenerator[OpenAIResponse | OpenAIStreamChunk]:
        """Call DashScope and project its envelope into OpenAI response shapes."""
        payload = dashscope_payload(request, self.model, self.provider_options)
        multimodal = self.multimodal or has_images(request)
        if multimodal:
            payload["messages"] = multimodal_messages(payload["messages"])
        payload["result_format"] = "message"
        payload["incremental_output"] = request.stream
        payload["base_address"] = self.base_url
        payload["request_timeout"] = self.timeout_seconds
        payload["api_key"] = self.api_key.get_secret_value()
        headers: JsonObject = {}
        for key, value in self.headers.items():
            headers[key] = value
        payload["headers"] = headers
        emitted = False
        for attempt in range(self.max_retries + 1):
            try:
                async with aiohttp.ClientSession() as session:
                    call: Callable[..., Awaitable[object]] = (
                        AioMultiModalConversation.call if multimodal else AioGeneration.call
                    )
                    raw = await call(**payload, session=session)
                    if request.stream:
                        if not isinstance(raw, QwenRawStream):
                            raise ModelResponseError(
                                "The Qwen provider did not return a stream.",
                            )
                        async with aclosing(raw) as stream:
                            async for chunk in stream:
                                projected = parse_stream_chunk(
                                    normalize_envelope(chunk, model=self.model, streaming=True),
                                )
                                emitted = True
                                yield projected
                    else:
                        yield parse_response(
                            normalize_envelope(raw, model=self.model, streaming=False),
                        )
                return
            except (aiohttp.ClientError, DashScopeException, TimeoutError) as error:
                normalized = (
                    ModelConnectionError()
                    if isinstance(error, aiohttp.ClientConnectionError | aiohttp.ClientPayloadError)
                    and not isinstance(error, TimeoutError)
                    else classify_provider_error(error)
                )
                if emitted or attempt == self.max_retries:
                    raise normalized from error
                await asyncio.sleep(min(0.25 * 2**attempt, 2.0))

    async def aclose(self) -> None:
        """Release Qwen resources owned by a completed request."""


class AnthropicProvider:
    """Call Qwen's Anthropic-compatible endpoint through the Anthropic SDK."""

    def __init__(
        self,
        *,
        model: str,
        api_key: SecretStr,
        base_url: str | None,
        timeout_seconds: float,
        max_retries: int,
        provider_options: Mapping[str, JsonValue],
        headers: Mapping[str, str],
    ) -> None:
        """Create a Qwen Anthropic protocol binding."""
        self.model = model
        self.provider_options = dict(provider_options)
        self.client = AsyncAnthropic(
            api_key=api_key.get_secret_value(),
            base_url=base_url if base_url is not None else "https://dashscope.aliyuncs.com/apps/anthropic",
            timeout=timeout_seconds,
            max_retries=max_retries,
            default_headers=dict(headers),
        )

    async def complete(self, request: AnthropicRequest) -> AnthropicResponse:
        """Send one Anthropic request to Qwen."""
        payload = request_payload(request, self.model, self.provider_options)
        try:
            create: Callable[..., Awaitable[object]] = self.client.messages.create
            raw = await create(**payload)
            return parse_anthropic_response(raw)
        except TimeoutException as error:
            raise ModelTimeoutError() from error
        except TransportError as error:
            raise ModelConnectionError() from error
        except AnthropicError as error:
            raise classify_provider_error(error) from error
        except SystemFault:
            raise
        except (AttributeError, KeyError, TypeError, ValueError) as error:
            raise ModelResponseError(
                "The Qwen provider returned an invalid response.",
            ) from error

    def stream(self, request: AnthropicRequest) -> AsyncIterator[AnthropicStreamEvent]:
        """Stream Anthropic events from Qwen."""
        return self.stream_events(request)

    async def stream_events(self, request: AnthropicRequest) -> AsyncIterator[AnthropicStreamEvent]:
        """Convert Qwen Anthropic stream events into protocol events."""
        payload = request_payload(request, self.model, self.provider_options)
        payload.pop("stream")
        completed = False
        try:
            stream: Callable[..., object] = self.client.messages.stream
            manager_value = stream(**payload)
            if not isinstance(manager_value, AnthropicRawStreamManager):
                raise ModelResponseError(
                    "The Qwen provider did not return a stream.",
                )
            async with manager_value as raw_stream:
                async for raw in raw_stream:
                    event = parse_event(raw)
                    completed = completed or event.type == "message_stop"
                    yield event
            if not completed:
                raise ModelResponseError(
                    "The Qwen stream ended without message_stop.",
                )
        except TimeoutException as error:
            raise ModelTimeoutError() from error
        except TransportError as error:
            raise ModelConnectionError() from error
        except AnthropicError as error:
            raise classify_provider_error(error) from error
        except SystemFault:
            raise
        except (AttributeError, KeyError, TypeError, ValueError) as error:
            raise ModelResponseError(
                "The Qwen provider returned an invalid stream.",
            ) from error

    async def aclose(self) -> None:
        """Close the Qwen Anthropic SDK client."""
        await self.client.close()


def request_payload(
    request: AnthropicRequest,
    model: str,
    provider_options: Mapping[str, JsonValue],
) -> JsonObject:
    """Build a Qwen Anthropic request payload."""
    try:
        return request.payload(model=model, provider_options=provider_options)
    except ValueError as error:
        raise ModelRequestError(
            "The model request cannot be converted for the provider.",
        ) from error


def normalize_envelope(raw: object, *, model: str, streaming: bool) -> Mapping[str, object]:
    """Convert one DashScope response envelope into an OpenAI-shaped mapping."""
    status = read_field(raw, "status_code")
    if status != 200:
        raise ModelProviderUnavailableError(
            "The Qwen provider returned a failed response.",
        )
    output = read_field(raw, "output")
    if output is None:
        raise ValueError("Qwen response output must be present")
    text = read_field(output, "text")
    finish = read_field(output, "finish_reason")
    choices = read_field(output, "choices")
    projected_choices: list[dict[str, object]] = []
    if isinstance(text, str):
        content = {"role": "assistant", "content": text}
        projected_choices.append(
            {
                "index": 0,
                "finish_reason": finish if isinstance(finish, str) else None,
                "delta" if streaming else "message": content,
            },
        )
    elif isinstance(choices, list | tuple):
        for choice in choices:
            message = read_field(choice, "message")
            if message is None:
                raise ValueError("Qwen response choice is missing message")
            projected_choices.append(
                {
                    "index": read_field(choice, "index") if read_field(choice, "index") is not None else 0,
                    "finish_reason": read_field(choice, "finish_reason"),
                    "delta" if streaming else "message": normalize_message(message),
                },
            )
    else:
        raise ValueError("Qwen response must contain text or choices")
    usage = read_field(raw, "usage")
    projected_usage: dict[str, object] | None = None
    if usage is not None:
        input_tokens = read_field(usage, "input_tokens")
        output_tokens = read_field(usage, "output_tokens")
        if not isinstance(input_tokens, int) or not isinstance(output_tokens, int):
            raise ValueError("Qwen response usage must contain token counts")
        projected_usage = {
            "prompt_tokens": input_tokens,
            "completion_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        }
    return {
        "id": read_field(raw, "request_id"),
        "object": "chat.completion.chunk" if streaming else "chat.completion",
        "model": model,
        "choices": projected_choices,
        "usage": projected_usage,
    }


def dashscope_payload(
    request: OpenAIRequest,
    model: str,
    provider_options: Mapping[str, JsonValue],
) -> JsonObject:
    """Convert an OpenAI request into DashScope generation arguments."""
    try:
        payload = request.payload(model=model, provider_options=provider_options)
    except ValueError as error:
        raise ModelRequestError(
            "The model request cannot be converted for the provider.",
        ) from error
    if request.max_tokens is not None and request.max_completion_tokens is not None:
        raise ModelRequestError(
            "The request cannot set both max_tokens and max_completion_tokens.",
        )
    if request.max_completion_tokens is not None:
        payload["max_tokens"] = request.max_completion_tokens
    # DashScope includes native usage without an OpenAI stream_options argument.
    options = payload.pop("stream_options", None)
    if options is not None and (
        not isinstance(options, dict)
        or set(options) != {"include_usage"}
        or not isinstance(options["include_usage"], bool)
    ):
        raise ModelUnsupportedError("Qwen supports only include_usage in stream_options.")
    for name in ("user", "metadata"):
        if name in payload:
            raise ModelUnsupportedError(
                f"Qwen does not support the OpenAI request field: {name}.",
            )
    payload.pop("max_completion_tokens", None)
    return payload


def has_images(request: OpenAIRequest) -> bool:
    """Identify image input without guessing capabilities from model names."""
    for message in request.messages:
        if isinstance(message.content, list):
            for block in message.content:
                if isinstance(block, dict) and block.get("type") == "image_url":
                    return True
    return False


def multimodal_messages(messages: JsonValue) -> list[JsonValue]:
    """Convert validated OpenAI messages to DashScope's native image blocks.

    Args:
        messages: Serialized OpenAI messages with ordered text and image parts.

    Returns:
        Detached native messages retaining tool-call and result identities.

    Raises:
        ModelRequestError: If the messages are malformed.
        ModelUnsupportedError: If a block has no supported native equivalent.
    """
    if not isinstance(messages, list):
        raise ModelRequestError("Qwen messages must be a list.")
    projected: list[JsonValue] = []
    for message in messages:
        if not isinstance(message, dict):
            raise ModelRequestError("Qwen messages must contain objects.")
        native = dict(message)
        content = native.get("content")
        if isinstance(content, str):
            native["content"] = [{"text": content}]
        elif isinstance(content, list):
            native["content"] = [multimodal_block(block) for block in content]
        projected.append(native)
    return projected


def multimodal_block(block: JsonValue) -> JsonObject:
    """Translate one image or text block, rejecting unmapped input semantics."""
    if not isinstance(block, dict):
        raise ModelRequestError("Qwen content blocks must be objects.")
    if block.get("type") == "text" and set(block) == {"type", "text"}:
        text = block["text"]
        if not isinstance(text, str):
            raise ModelRequestError("Qwen text content must be a string.")
        return {"text": text}
    if block.get("type") == "image_url" and set(block) == {"type", "image_url"}:
        image = block["image_url"]
        if isinstance(image, dict) and set(image) == {"url"}:
            url = image["url"]
            if isinstance(url, str) and url.startswith(("https://", "http://", "data:image/")):
                return {"image": url}
    raise ModelUnsupportedError("Qwen native image input supports only text and image_url.url blocks.")


def normalize_message(message: object) -> Mapping[str, object]:
    """Project DashScope message content and incremental tool calls to OpenAI."""
    projected: dict[str, object] = {}
    for name in ("role", "reasoning_content", "tool_calls"):
        value = read_field(message, name)
        if value is not None:
            projected[name] = value
    content = read_field(message, "content")
    if isinstance(content, list | tuple):
        parts: list[str] = []
        for block in content:
            if not isinstance(block, Mapping) or set(block) != {"text"}:
                raise ModelResponseError("The Qwen conversation returned unsupported content.")
            text = block["text"]
            if not isinstance(text, str):
                raise ModelResponseError("The Qwen conversation returned invalid text.")
            parts.append(text)
        projected["content"] = "".join(parts)
    else:
        projected["content"] = content
    return projected


def read_field(value: object, name: str) -> object:
    """Read one field from a DashScope mapping or response object."""
    if isinstance(value, Mapping):
        return value.get(name)
    try:
        attributes = vars(value)
    except TypeError:
        return None
    return attributes.get(name)
