from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from typing import Protocol, runtime_checkable

from openai import AsyncOpenAI, OpenAIError
from pydantic import SecretStr

from pleiad.contracts import JsonObject, JsonValue
from pleiad.errors.base import SystemFault
from pleiad.models.errors import (
    ModelRequestError,
    ModelResponseError,
    classify_provider_error,
)
from pleiad.models.openai.request import OpenAIRequest
from pleiad.models.openai.response import OpenAIResponse, OpenAIStreamChunk, parse_response, parse_stream_chunk


@runtime_checkable
class OpenAIRawStream(Protocol):
    """Minimal stream surface used from the official OpenAI SDK."""

    def __aiter__(self) -> AsyncIterator[object]:
        """Iterate over vendor response chunks."""

    async def close(self) -> None:
        """Close the vendor response stream."""


class OpenAIProvider:
    """Call the OpenAI official SDK for the OpenAI protocol."""

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
        """Create an OpenAI SDK operation binding."""
        self.model = model
        self.provider_options = dict(provider_options)
        self.client = AsyncOpenAI(
            api_key=api_key.get_secret_value(),
            base_url=base_url,
            timeout=timeout_seconds,
            max_retries=max_retries,
            default_headers=dict(headers),
        )

    async def complete(self, request: OpenAIRequest) -> OpenAIResponse:
        """Convert and send one OpenAI request body."""
        payload = request_payload(request, self.model, self.provider_options)
        try:
            create: Callable[..., Awaitable[object]] = self.client.chat.completions.create
            raw = await create(**payload)
            return parse_response(raw)
        except OpenAIError as error:
            raise classify_provider_error(error) from error
        except (AttributeError, KeyError, TypeError, ValueError) as error:
            raise ModelResponseError(
                "The OpenAI provider returned an invalid response.",
            ) from error

    async def stream(self, request: OpenAIRequest) -> AsyncIterator[OpenAIStreamChunk]:
        """Convert and stream OpenAI response chunks."""
        payload = request_payload(request, self.model, self.provider_options)
        payload["stream"] = True
        raw_stream: OpenAIRawStream | None = None
        finished = False
        try:
            create: Callable[..., Awaitable[object]] = self.client.chat.completions.create
            raw_value = await create(**payload)
            if not isinstance(raw_value, OpenAIRawStream):
                raise ModelResponseError(
                    "The OpenAI provider did not return a stream.",
                )
            raw_stream = raw_value
            async for raw in raw_stream:
                chunk = parse_stream_chunk(raw)
                for choice in chunk.choices:
                    if choice.finish_reason is not None:
                        finished = True
                yield chunk
            if not finished:
                raise ModelResponseError(
                    "The OpenAI stream ended without a finish reason.",
                )
        except OpenAIError as error:
            raise classify_provider_error(error) from error
        except SystemFault:
            raise
        except (AttributeError, KeyError, TypeError, ValueError) as error:
            raise ModelResponseError(
                "The OpenAI provider returned an invalid stream.",
            ) from error
        finally:
            if raw_stream is not None:
                await raw_stream.close()

    async def aclose(self) -> None:
        """Close the OpenAI SDK client owned by this binding."""
        await self.client.close()


def request_payload(
    request: OpenAIRequest,
    model: str,
    provider_options: Mapping[str, JsonValue],
) -> JsonObject:
    """Build an OpenAI request payload and classify conversion failures."""
    try:
        return request.payload(model=model, provider_options=provider_options)
    except ValueError as error:
        raise ModelRequestError(
            "The model request cannot be converted for the provider.",
        ) from error
