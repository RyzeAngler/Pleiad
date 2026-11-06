import json
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from types import TracebackType
from typing import Protocol, runtime_checkable

from anthropic import AnthropicError, AsyncAnthropic
from openai import AsyncOpenAI, OpenAIError
from pydantic import SecretStr

from pleiad.contracts import JsonObject, JsonValue
from pleiad.errors.base import SystemFault
from pleiad.models.anthropic.request import AnthropicRequest
from pleiad.models.anthropic.response import AnthropicResponse, AnthropicStreamEvent, parse_event, parse_response
from pleiad.models.errors import (
    ModelRequestError,
    ModelResponseError,
    classify_provider_error,
)
from pleiad.models.openai.request import OpenAIMessage, OpenAIRequest
from pleiad.models.openai.response import OpenAIResponse, OpenAIStreamChunk, parse_stream_chunk
from pleiad.models.openai.response import parse_response as parse_openai_response


@runtime_checkable
class OpenAIRawStream(Protocol):
    """Minimal OpenAI SDK stream surface."""

    def __aiter__(self) -> AsyncIterator[object]:
        """Iterate over provider chunks."""

    async def close(self) -> None:
        """Close the provider stream."""


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
    """Call DeepSeek's OpenAI-compatible endpoint through the OpenAI SDK."""

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
        """Create a DeepSeek OpenAI protocol binding."""
        self.model = model
        self.provider_options = dict(provider_options)
        self.client = AsyncOpenAI(
            api_key=api_key.get_secret_value(),
            base_url=base_url if base_url is not None else "https://api.deepseek.com",
            timeout=timeout_seconds,
            max_retries=max_retries,
            default_headers=dict(headers),
        )

    async def complete(self, request: OpenAIRequest) -> OpenAIResponse:
        """Send one OpenAI request to DeepSeek."""
        payload = request_payload(request, self.model, self.provider_options, "deepseek", "openai")
        try:
            create: Callable[..., Awaitable[object]] = self.client.chat.completions.create
            raw = await create(**payload)
            return parse_openai_response(raw)
        except OpenAIError as error:
            raise classify_provider_error(error) from error
        except SystemFault:
            raise
        except (AttributeError, KeyError, TypeError, ValueError) as error:
            raise ModelResponseError(
                "The DeepSeek provider returned an invalid response.",
            ) from error

    def stream(self, request: OpenAIRequest) -> AsyncIterator[OpenAIStreamChunk]:
        """Stream OpenAI chunks from DeepSeek."""
        return self.stream_events(request)

    async def stream_events(self, request: OpenAIRequest) -> AsyncIterator[OpenAIStreamChunk]:
        """Convert DeepSeek OpenAI stream chunks into protocol chunks."""
        payload = request_payload(request, self.model, self.provider_options, "deepseek", "openai")
        payload["stream"] = True
        raw_stream: OpenAIRawStream | None = None
        finished = False
        try:
            create: Callable[..., Awaitable[object]] = self.client.chat.completions.create
            raw_value = await create(**payload)
            if not isinstance(raw_value, OpenAIRawStream):
                raise ModelResponseError(
                    "The DeepSeek provider did not return a stream.",
                )
            raw_stream = raw_value
            async for raw in raw_stream:
                chunk = parse_stream_chunk(raw)
                finished = finished or any(choice.finish_reason is not None for choice in chunk.choices)
                yield chunk
            if not finished:
                raise ModelResponseError(
                    "The DeepSeek stream ended without a finish reason.",
                )
        except OpenAIError as error:
            raise classify_provider_error(error) from error
        except SystemFault:
            raise
        except (AttributeError, KeyError, TypeError, ValueError) as error:
            raise ModelResponseError(
                "The DeepSeek provider returned an invalid stream.",
            ) from error
        finally:
            if raw_stream is not None:
                await raw_stream.close()

    async def aclose(self) -> None:
        """Close the DeepSeek SDK client."""
        await self.client.close()


class AnthropicProvider:
    """Call DeepSeek's Anthropic-compatible endpoint through the Anthropic SDK."""

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
        """Create a DeepSeek Anthropic protocol binding."""
        self.model = model
        self.provider_options = dict(provider_options)
        self.client = AsyncAnthropic(
            api_key=api_key.get_secret_value(),
            base_url=base_url if base_url is not None else "https://api.deepseek.com/anthropic",
            timeout=timeout_seconds,
            max_retries=max_retries,
            default_headers=dict(headers),
        )

    async def complete(self, request: AnthropicRequest) -> AnthropicResponse:
        """Send one Anthropic request to DeepSeek."""
        payload = request_payload(request, self.model, self.provider_options, "deepseek", "anthropic")
        try:
            create: Callable[..., Awaitable[object]] = self.client.messages.create
            raw = await create(**payload)
            return parse_response(raw)
        except AnthropicError as error:
            raise classify_provider_error(error) from error
        except SystemFault:
            raise
        except (AttributeError, KeyError, TypeError, ValueError) as error:
            raise ModelResponseError(
                "The DeepSeek provider returned an invalid response.",
            ) from error

    def stream(self, request: AnthropicRequest) -> AsyncIterator[AnthropicStreamEvent]:
        """Stream Anthropic events from DeepSeek."""
        return self.stream_events(request)

    async def stream_events(self, request: AnthropicRequest) -> AsyncIterator[AnthropicStreamEvent]:
        """Convert DeepSeek Anthropic stream events into protocol events."""
        payload = request_payload(request, self.model, self.provider_options, "deepseek", "anthropic")
        payload.pop("stream")
        completed = False
        try:
            stream: Callable[..., object] = self.client.messages.stream
            manager_value = stream(**payload)
            if not isinstance(manager_value, AnthropicRawStreamManager):
                raise ModelResponseError(
                    "The DeepSeek provider did not return a stream.",
                )
            async with manager_value as raw_stream:
                async for raw in raw_stream:
                    event = parse_event(raw)
                    completed = completed or event.type == "message_stop"
                    yield event
            if not completed:
                raise ModelResponseError(
                    "The DeepSeek stream ended without message_stop.",
                )
        except AnthropicError as error:
            raise classify_provider_error(error) from error
        except SystemFault:
            raise
        except (AttributeError, KeyError, TypeError, ValueError) as error:
            raise ModelResponseError(
                "The DeepSeek provider returned an invalid stream.",
            ) from error

    async def aclose(self) -> None:
        """Close the DeepSeek SDK client."""
        await self.client.close()


def request_payload(
    request: OpenAIRequest | AnthropicRequest,
    model: str,
    provider_options: Mapping[str, JsonValue],
    provider: str,
    protocol: str,
) -> JsonObject:
    """Build a protocol payload and classify request conversion failures."""
    try:
        if isinstance(request, OpenAIRequest):
            request = json_mode_request(request)
        return request.payload(model=model, provider_options=provider_options)
    except ValueError as error:
        raise ModelRequestError(
            "The model request cannot be converted for the provider.",
        ) from error


def json_mode_request(request: OpenAIRequest) -> OpenAIRequest:
    """Map structured output to DeepSeek Chat Completions JSON mode.

    DeepSeek Chat Completions does not accept the OpenAI ``json_schema``
    response format. Send JSON mode and the schema as model instructions on
    the first request; Runtime still validates the final Agent output. This
    conversion does not claim server-side strict-schema enforcement.

    Args:
        request: Validated OpenAI protocol request.

    Returns:
        The unchanged request, or a detached JSON-mode request with its schema.

    Raises:
        ValueError: If a structured-output envelope has no object schema.
    """
    response_format = request.response_format
    if response_format is None or response_format.get("type") != "json_schema":
        return request
    definition = response_format.get("json_schema")
    if not isinstance(definition, dict):
        raise ValueError("json_schema response format requires a schema definition")
    schema = definition.get("schema")
    if not isinstance(schema, dict):
        raise ValueError("json_schema response format requires an object schema")
    instruction = OpenAIMessage(
        role="system",
        content=(
            "Return the final answer as a JSON object matching this JSON Schema. "
            "Do not wrap the JSON in markdown or add explanatory text.\n"
            f"{json.dumps(schema, ensure_ascii=False)}"
        ),
    )
    return request.model_copy(
        update={"messages": [instruction, *request.messages], "response_format": {"type": "json_object"}}
    )
