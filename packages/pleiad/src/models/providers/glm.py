import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from types import TracebackType
from typing import Protocol, runtime_checkable

from anthropic import AnthropicError, AsyncAnthropic
from pydantic import SecretStr
from zai import ZhipuAiClient
from zai.core import StreamResponse, ZaiError
from zai.types.chat import ChatCompletionChunk

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
    ModelRequestError,
    ModelResponseError,
    classify_provider_error,
)
from pleiad.models.openai.request import OpenAIRequest
from pleiad.models.openai.response import (
    OpenAIResponse,
    OpenAIStreamChunk,
    parse_stream_chunk,
)
from pleiad.models.openai.response import (
    parse_response as parse_openai_response,
)


async def offload[ResultT](call: Callable[[], ResultT]) -> ResultT:
    """Run one blocking official SDK operation without abandoning its transport."""
    task = asyncio.create_task(asyncio.to_thread(call))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await asyncio.gather(task, return_exceptions=True)
        raise


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
    """Call the Zhipu official SDK using the OpenAI protocol contract."""

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
        """Create a GLM SDK operation binding."""
        self.model = model
        self.provider_options = dict(provider_options)
        endpoint = base_url if base_url is not None else "https://open.bigmodel.cn/api/paas/v4/"
        self.client = ZhipuAiClient(
            api_key=api_key.get_secret_value(),
            base_url=endpoint,
            timeout=timeout_seconds,
            max_retries=max_retries,
            custom_headers=dict(headers),
        )

    async def complete(self, request: OpenAIRequest) -> OpenAIResponse:
        """Convert and send one OpenAI request through zai."""
        payload = openai_payload(request, self.model, self.provider_options)
        try:
            create: Callable[..., object] = self.client.chat.completions.create
            raw = await offload(lambda: create(**payload))
            return parse_openai_response(raw)
        except ZaiError as error:
            raise classify_provider_error(error) from error
        except (AttributeError, KeyError, TypeError, ValueError) as error:
            raise ModelResponseError(
                "The GLM provider returned an invalid response.",
            ) from error

    async def stream(self, request: OpenAIRequest) -> AsyncIterator[OpenAIStreamChunk]:
        """Convert and stream OpenAI chunks through zai."""
        payload = openai_payload(request, self.model, self.provider_options)
        payload["stream"] = True
        raw_stream: StreamResponse[ChatCompletionChunk] | None = None
        finished = False
        try:
            create: Callable[..., object] = self.client.chat.completions.create
            raw_value = await offload(lambda: create(**payload))
            if not isinstance(raw_value, StreamResponse):
                raise ModelResponseError(
                    "The GLM provider did not return a stream.",
                )
            raw_stream = raw_value
            iterator = iter(raw_stream)
            while True:
                item = await offload(lambda: next(iterator, None))
                if item is None:
                    break
                chunk = parse_stream_chunk(item)
                for choice in chunk.choices:
                    if choice.finish_reason is not None:
                        finished = True
                yield chunk
            if not finished:
                raise ModelResponseError(
                    "The GLM stream ended without a finish reason.",
                )
        except ZaiError as error:
            raise classify_provider_error(error) from error
        except SystemFault:
            raise
        except (AttributeError, KeyError, TypeError, ValueError) as error:
            raise ModelResponseError(
                "The GLM provider returned an invalid stream.",
            ) from error
        finally:
            if raw_stream is not None:
                await offload(raw_stream.response.close)

    async def aclose(self) -> None:
        """Close the synchronous zai SDK client owned by this binding."""
        await offload(self.client.close)


class AnthropicProvider:
    """Call GLM's Anthropic-compatible endpoint through the Anthropic SDK."""

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
        """Create a GLM Anthropic protocol binding."""
        self.model = model
        self.provider_options = dict(provider_options)
        self.client = AsyncAnthropic(
            api_key=api_key.get_secret_value(),
            base_url=base_url if base_url is not None else "https://open.bigmodel.cn/api/anthropic",
            timeout=timeout_seconds,
            max_retries=max_retries,
            default_headers=dict(headers),
        )

    async def complete(self, request: AnthropicRequest) -> AnthropicResponse:
        """Send one Anthropic request to GLM."""
        payload = request_payload(request, self.model, self.provider_options)
        try:
            create: Callable[..., Awaitable[object]] = self.client.messages.create
            raw = await create(**payload)
            return parse_anthropic_response(raw)
        except AnthropicError as error:
            raise classify_provider_error(error) from error
        except SystemFault:
            raise
        except (AttributeError, KeyError, TypeError, ValueError) as error:
            raise ModelResponseError(
                "The GLM provider returned an invalid response.",
            ) from error

    def stream(self, request: AnthropicRequest) -> AsyncIterator[AnthropicStreamEvent]:
        """Stream Anthropic events from GLM."""
        return self.stream_events(request)

    async def stream_events(self, request: AnthropicRequest) -> AsyncIterator[AnthropicStreamEvent]:
        """Convert GLM Anthropic stream events into protocol events."""
        payload = request_payload(request, self.model, self.provider_options)
        payload["stream"] = True
        completed = False
        try:
            stream: Callable[..., object] = self.client.messages.stream
            manager_value = stream(**payload)
            if not isinstance(manager_value, AnthropicRawStreamManager):
                raise ModelResponseError(
                    "The GLM provider did not return a stream.",
                )
            async with manager_value as raw_stream:
                async for raw in raw_stream:
                    event = parse_event(raw)
                    completed = completed or event.type == "message_stop"
                    yield event
            if not completed:
                raise ModelResponseError(
                    "The GLM stream ended without message_stop.",
                )
        except AnthropicError as error:
            raise classify_provider_error(error) from error
        except SystemFault:
            raise
        except (AttributeError, KeyError, TypeError, ValueError) as error:
            raise ModelResponseError(
                "The GLM provider returned an invalid stream.",
            ) from error

    async def aclose(self) -> None:
        """Close the GLM Anthropic SDK client."""
        await self.client.close()


def request_payload(
    request: AnthropicRequest,
    model: str,
    provider_options: Mapping[str, JsonValue],
) -> dict[str, JsonValue]:
    """Build a GLM Anthropic request payload."""
    try:
        return request.payload(model=model, provider_options=provider_options)
    except ValueError as error:
        raise ModelRequestError(
            "The model request cannot be converted for the provider.",
        ) from error


def openai_payload(
    request: OpenAIRequest,
    model: str,
    provider_options: Mapping[str, JsonValue],
) -> JsonObject:
    """Build a GLM OpenAI request payload and classify conversion failures."""
    try:
        return request.payload(model=model, provider_options=provider_options)
    except ValueError as error:
        raise ModelRequestError(
            "The model request cannot be converted for the provider.",
        ) from error
