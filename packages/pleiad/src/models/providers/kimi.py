from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from types import TracebackType
from typing import Protocol, runtime_checkable

from anthropic import AnthropicError, AsyncAnthropic
from httpx import TimeoutException, TransportError
from openai import AsyncOpenAI, OpenAIError
from pydantic import SecretStr

from pleiad.contracts import JsonObject, JsonValue
from pleiad.errors.base import SystemFault
from pleiad.models.anthropic.request import AnthropicRequest
from pleiad.models.anthropic.response import AnthropicResponse, AnthropicStreamEvent, parse_event
from pleiad.models.anthropic.response import parse_response as parse_anthropic_response
from pleiad.models.errors import (
    ModelConnectionError,
    ModelRequestError,
    ModelResponseError,
    ModelTimeoutError,
    classify_provider_error,
)
from pleiad.models.openai.request import OpenAIRequest
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
    """Call Kimi's OpenAI-compatible endpoint through the OpenAI SDK."""

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
        """Create a Kimi OpenAI protocol binding."""
        self.model = model
        self.provider_options = dict(provider_options)
        self.client = AsyncOpenAI(
            api_key=api_key.get_secret_value(),
            base_url=base_url if base_url is not None else "https://api.moonshot.ai/v1",
            timeout=timeout_seconds,
            max_retries=max_retries,
            default_headers=dict(headers),
        )

    async def complete(self, request: OpenAIRequest) -> OpenAIResponse:
        """Send one OpenAI request to Kimi."""
        payload = openai_request_payload(request, self.model, self.provider_options)
        try:
            create: Callable[..., Awaitable[object]] = self.client.chat.completions.create
            raw = await create(**payload)
            return parse_openai_response(raw)
        except TimeoutException as error:
            raise ModelTimeoutError() from error
        except TransportError as error:
            raise ModelConnectionError() from error
        except OpenAIError as error:
            raise classify_provider_error(error) from error
        except SystemFault:
            raise
        except (AttributeError, KeyError, TypeError, ValueError) as error:
            raise ModelResponseError(
                "The Kimi provider returned an invalid response.",
            ) from error

    def stream(self, request: OpenAIRequest) -> AsyncIterator[OpenAIStreamChunk]:
        """Stream OpenAI chunks from Kimi."""
        return self.stream_events(request)

    async def stream_events(self, request: OpenAIRequest) -> AsyncIterator[OpenAIStreamChunk]:
        """Convert Kimi OpenAI stream chunks into protocol chunks."""
        payload = openai_request_payload(request, self.model, self.provider_options)
        payload["stream"] = True
        raw_stream: OpenAIRawStream | None = None
        finished = False
        try:
            create: Callable[..., Awaitable[object]] = self.client.chat.completions.create
            raw_value = await create(**payload)
            if not isinstance(raw_value, OpenAIRawStream):
                raise ModelResponseError(
                    "The Kimi provider did not return a stream.",
                )
            raw_stream = raw_value
            async for raw in raw_stream:
                chunk = parse_stream_chunk(raw)
                finished = finished or any(choice.finish_reason is not None for choice in chunk.choices)
                yield chunk
            if not finished:
                raise ModelResponseError(
                    "The Kimi stream ended without a finish reason.",
                )
        except TimeoutException as error:
            raise ModelTimeoutError() from error
        except TransportError as error:
            raise ModelConnectionError() from error
        except OpenAIError as error:
            raise classify_provider_error(error) from error
        except SystemFault:
            raise
        except (AttributeError, KeyError, TypeError, ValueError) as error:
            raise ModelResponseError(
                "The Kimi provider returned an invalid stream.",
            ) from error
        finally:
            if raw_stream is not None:
                await raw_stream.close()

    async def aclose(self) -> None:
        """Close the Kimi OpenAI SDK client."""
        await self.client.close()


class AnthropicProvider:
    """Call Kimi's Anthropic-compatible endpoint through the Anthropic SDK."""

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
        """Create a Kimi Anthropic protocol binding."""
        self.model = model
        self.provider_options = dict(provider_options)
        self.client = AsyncAnthropic(
            api_key=api_key.get_secret_value(),
            base_url=base_url if base_url is not None else "https://api.moonshot.ai/anthropic",
            timeout=timeout_seconds,
            max_retries=max_retries,
            default_headers=dict(headers),
        )

    async def complete(self, request: AnthropicRequest) -> AnthropicResponse:
        """Send one Anthropic request to Kimi."""
        payload = anthropic_request_payload(request, self.model, self.provider_options)
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
                "The Kimi provider returned an invalid response.",
            ) from error

    def stream(self, request: AnthropicRequest) -> AsyncIterator[AnthropicStreamEvent]:
        """Stream Anthropic events from Kimi."""
        return self.stream_events(request)

    async def stream_events(self, request: AnthropicRequest) -> AsyncIterator[AnthropicStreamEvent]:
        """Convert Kimi Anthropic stream events into protocol events."""
        payload = anthropic_request_payload(request, self.model, self.provider_options)
        payload.pop("stream")
        completed = False
        try:
            stream: Callable[..., object] = self.client.messages.stream
            manager_value = stream(**payload)
            if not isinstance(manager_value, AnthropicRawStreamManager):
                raise ModelResponseError(
                    "The Kimi provider did not return a stream.",
                )
            async with manager_value as raw_stream:
                async for raw in raw_stream:
                    event = parse_event(raw)
                    completed = completed or event.type == "message_stop"
                    yield event
            if not completed:
                raise ModelResponseError(
                    "The Kimi stream ended without message_stop.",
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
                "The Kimi provider returned an invalid stream.",
            ) from error

    async def aclose(self) -> None:
        """Close the Kimi Anthropic SDK client."""
        await self.client.close()


def openai_request_payload(
    request: OpenAIRequest,
    model: str,
    provider_options: Mapping[str, JsonValue],
) -> JsonObject:
    """Build a Kimi OpenAI request payload."""
    options = dict(provider_options)
    thinking = options.pop("thinking", None)
    try:
        payload = request.payload(model=model, provider_options=options)
    except ValueError as error:
        raise ModelRequestError(
            "The model request cannot be converted for the provider.",
        ) from error
    if thinking is not None:
        payload["extra_body"] = {"thinking": thinking}
    return payload


def anthropic_request_payload(
    request: AnthropicRequest,
    model: str,
    provider_options: Mapping[str, JsonValue],
) -> JsonObject:
    """Build a Kimi Anthropic request payload."""
    try:
        return request.payload(model=model, provider_options=provider_options)
    except ValueError as error:
        raise ModelRequestError(
            "The model request cannot be converted for the provider.",
        ) from error
