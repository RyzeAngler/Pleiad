from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from types import TracebackType
from typing import Protocol, runtime_checkable

from anthropic import AnthropicError, AsyncAnthropic
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


@runtime_checkable
class AnthropicRawStream(Protocol):
    """Minimal stream surface used from the official Anthropic SDK."""

    def __aiter__(self) -> AsyncIterator[object]:
        """Iterate over vendor response events."""


@runtime_checkable
class AnthropicRawStreamManager(Protocol):
    """Minimal async context manager surface for an Anthropic stream."""

    async def __aenter__(self) -> AnthropicRawStream:
        """Open the vendor response stream."""

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        """Close the vendor response stream."""


class AnthropicProvider:
    """Call the Anthropic official SDK for the Anthropic protocol."""

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
        """Create an Anthropic SDK operation binding."""
        self.model = model
        self.provider_options = dict(provider_options)
        self.client = AsyncAnthropic(
            api_key=api_key.get_secret_value(),
            base_url=base_url,
            timeout=timeout_seconds,
            max_retries=max_retries,
            default_headers=dict(headers),
        )

    async def complete(self, request: AnthropicRequest) -> AnthropicResponse:
        """Convert and send one Anthropic request body."""
        payload = request_payload(request, self.model, self.provider_options)
        try:
            create: Callable[..., Awaitable[object]] = self.client.messages.create
            raw = await create(**payload)
            return parse_response(raw)
        except AnthropicError as error:
            raise classify_provider_error(error) from error
        except (AttributeError, KeyError, TypeError, ValueError) as error:
            raise ModelResponseError(
                "The Anthropic provider returned an invalid response.",
            ) from error

    async def stream(self, request: AnthropicRequest) -> AsyncIterator[AnthropicStreamEvent]:
        """Convert and stream Anthropic message events."""
        payload = request_payload(request, self.model, self.provider_options)
        payload.pop("stream")
        completed = False
        try:
            stream: Callable[..., object] = self.client.messages.stream
            manager_value = stream(**payload)
            if not isinstance(manager_value, AnthropicRawStreamManager):
                raise ModelResponseError(
                    "The Anthropic provider did not return a stream.",
                )
            async with manager_value as raw_stream:
                async for raw in raw_stream:
                    event = parse_event(raw)
                    if event.type == "message_stop":
                        completed = True
                    yield event
            if not completed:
                raise ModelResponseError(
                    "The Anthropic stream ended without message_stop.",
                )
        except AnthropicError as error:
            raise classify_provider_error(error) from error
        except SystemFault:
            raise
        except (AttributeError, KeyError, TypeError, ValueError) as error:
            raise ModelResponseError(
                "The Anthropic provider returned an invalid stream.",
            ) from error

    async def aclose(self) -> None:
        """Close the Anthropic SDK client owned by this binding."""
        await self.client.close()


def request_payload(
    request: AnthropicRequest,
    model: str,
    provider_options: Mapping[str, JsonValue],
) -> JsonObject:
    """Build an Anthropic request payload and classify conversion failures."""
    try:
        return request.payload(model=model, provider_options=provider_options)
    except ValueError as error:
        raise ModelRequestError(
            "The model request cannot be converted for the provider.",
        ) from error
