import asyncio
from collections.abc import AsyncIterator
from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from pleiad.models.anthropic.client import AnthropicModelClient
from pleiad.models.anthropic.request import AnthropicRequest
from pleiad.models.anthropic.response import AnthropicContentBlock, AnthropicResponse, AnthropicStreamEvent
from pleiad.models.errors import ModelConfigurationError, ModelErrorCode, ModelRequestError
from pleiad.models.openai.client import OpenAIModelClient
from pleiad.models.openai.request import OpenAIRequest
from pleiad.models.openai.response import (
    OpenAIChoice,
    OpenAIResponse,
    OpenAIResponseMessage,
    OpenAIStreamChoice,
    OpenAIStreamChunk,
    OpenAIStreamDelta,
)


class RecordingOpenAIProvider:
    """Fake provider boundary for the concrete OpenAI protocol object."""

    complete_request: OpenAIRequest | None = None
    stream_request: OpenAIRequest | None = None
    closed = False

    async def complete(self, request: OpenAIRequest) -> OpenAIResponse:
        """Record a request and return a protocol response."""
        self.complete_request = request
        return OpenAIResponse(
            id="response-1",
            model="test-model",
            choices=(
                OpenAIChoice(
                    message=OpenAIResponseMessage(content="done"),
                    finish_reason="stop",
                ),
            ),
        )

    def stream(self, request: OpenAIRequest) -> AsyncIterator[OpenAIStreamChunk]:
        """Record a request and return one deterministic stream."""
        self.stream_request = request
        return self.stream_events()

    async def stream_events(self) -> AsyncIterator[OpenAIStreamChunk]:
        """Yield one text delta and one terminal chunk."""
        yield OpenAIStreamChunk(
            model="test-model",
            choices=(OpenAIStreamChoice(delta=OpenAIStreamDelta(content="done")),),
        )
        yield OpenAIStreamChunk(
            model="test-model",
            choices=(OpenAIStreamChoice(delta=OpenAIStreamDelta(), finish_reason="stop"),),
        )

    async def aclose(self) -> None:
        """Record provider cleanup."""
        self.closed = True


class RecordingAnthropicProvider:
    """Fake provider boundary for the concrete Anthropic protocol object."""

    complete_request: AnthropicRequest | None = None

    async def complete(self, request: AnthropicRequest) -> AnthropicResponse:
        """Record a request and return a protocol response."""
        self.complete_request = request
        return AnthropicResponse(
            id="message-1",
            model="test-model",
            content=(AnthropicContentBlock(type="text", text="done"),),
            stop_reason="end_turn",
        )

    def stream(self, request: AnthropicRequest) -> AsyncIterator[AnthropicStreamEvent]:
        """Return an empty provider stream for the unused test operation."""
        return self.stream_events(request)

    async def stream_events(self, request: AnthropicRequest) -> AsyncIterator[AnthropicStreamEvent]:
        """Yield no events."""
        if request.messages:
            return
        yield AnthropicStreamEvent(type="message_stop")

    async def aclose(self) -> None:
        """Release the fake provider."""


def test_openai_model_client_accepts_openai_body_and_returns_protocol_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The concrete OpenAI object validates the body and delegates it to its provider."""
    provider = RecordingOpenAIProvider()

    def provider_class(**options: object) -> RecordingOpenAIProvider:
        """Return the fake provider at the provider module boundary."""
        return provider

    def import_provider(module_name: str) -> object:
        """Return a fake provider module for the OpenAI provider name."""
        assert module_name == "pleiad.models.providers.openai"
        return SimpleNamespace(OpenAIProvider=provider_class)

    monkeypatch.setattr("pleiad.models.openai.client.import_module", import_provider)
    client = OpenAIModelClient(
        provider="openai",
        model="test-model",
        api_key="test-key",
    )

    response = asyncio.run(
        client.complete(
            {
                "messages": [{"role": "user", "content": "hello"}],
            },
        ),
    )

    assert response.id == "response-1"
    assert provider.complete_request is not None
    assert provider.complete_request.messages[0].content == "hello"


def test_openai_model_client_forces_streaming_on_the_protocol_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """The concrete OpenAI object marks a validated request as streaming before delegation."""
    provider = RecordingOpenAIProvider()

    def provider_class(**options: object) -> RecordingOpenAIProvider:
        """Return the fake provider at the provider module boundary."""
        return provider

    def import_provider(module_name: str) -> object:
        """Return a fake provider module for the OpenAI provider name."""
        assert module_name == "pleiad.models.providers.openai"
        return SimpleNamespace(OpenAIProvider=provider_class)

    monkeypatch.setattr("pleiad.models.openai.client.import_module", import_provider)
    client = OpenAIModelClient(
        provider="openai",
        model="test-model",
        api_key="test-key",
    )

    async def collect() -> list[OpenAIStreamChunk]:
        return [
            chunk
            async for chunk in client.stream(
                {
                    "messages": [{"role": "user", "content": "hello"}],
                },
            )
        ]

    chunks = asyncio.run(collect())

    assert [chunk.choices[0].delta.content for chunk in chunks] == ["done", None]
    assert provider.stream_request is not None
    assert provider.stream_request.stream is True


def test_openai_model_client_forces_non_streaming_on_complete_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """The concrete OpenAI object always uses its complete method for one response."""
    provider = RecordingOpenAIProvider()

    def provider_class(**options: object) -> RecordingOpenAIProvider:
        """Return the fake provider at the provider module boundary."""
        return provider

    def import_provider(module_name: str) -> object:
        """Return a fake provider module for the OpenAI provider name."""
        assert module_name == "pleiad.models.providers.openai"
        return SimpleNamespace(OpenAIProvider=provider_class)

    monkeypatch.setattr("pleiad.models.openai.client.import_module", import_provider)
    client = OpenAIModelClient(provider="openai", model="test-model", api_key="test-key")

    asyncio.run(
        client.complete(
            {
                "messages": [{"role": "user", "content": "hello"}],
                "stream": True,
            },
        ),
    )

    assert provider.complete_request is not None
    assert provider.complete_request.stream is False


def test_anthropic_model_client_accepts_anthropic_body_and_returns_protocol_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The concrete Anthropic object validates the Anthropic Messages body."""
    provider = RecordingAnthropicProvider()

    def provider_class(**options: object) -> RecordingAnthropicProvider:
        """Return the fake provider at the provider module boundary."""
        return provider

    def import_provider(module_name: str) -> object:
        """Return a fake provider module for the Anthropic provider name."""
        assert module_name == "pleiad.models.providers.deepseek"
        return SimpleNamespace(AnthropicProvider=provider_class)

    monkeypatch.setattr("pleiad.models.anthropic.client.import_module", import_provider)
    client = AnthropicModelClient(
        provider="deepseek",
        model="test-model",
        api_key=SecretStr("test-key"),
    )

    response = asyncio.run(
        client.complete(
            {
                "messages": [{"role": "user", "content": "hello"}],
                "max_tokens": 32,
            },
        ),
    )

    assert response.id == "message-1"
    assert provider.complete_request is not None
    assert provider.complete_request.max_tokens == 32


def test_anthropic_model_client_forces_non_streaming_on_complete_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The concrete Anthropic object always uses its complete method for one response."""
    provider = RecordingAnthropicProvider()

    def provider_class(**options: object) -> RecordingAnthropicProvider:
        """Return the fake provider at the provider module boundary."""
        return provider

    def import_provider(module_name: str) -> object:
        """Return a fake provider module for the Anthropic provider name."""
        assert module_name == "pleiad.models.providers.anthropic"
        return SimpleNamespace(AnthropicProvider=provider_class)

    monkeypatch.setattr("pleiad.models.anthropic.client.import_module", import_provider)
    client = AnthropicModelClient(provider="anthropic", model="test-model", api_key="test-key")

    asyncio.run(
        client.complete(
            {
                "messages": [{"role": "user", "content": "hello"}],
                "max_tokens": 32,
                "stream": True,
            },
        ),
    )

    assert provider.complete_request is not None
    assert provider.complete_request.stream is False


def test_openai_model_client_reports_invalid_request_as_specific_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """The concrete OpenAI object classifies malformed public request bodies."""
    provider = RecordingOpenAIProvider()

    def provider_class(**options: object) -> RecordingOpenAIProvider:
        """Return the fake provider at the provider module boundary."""
        return provider

    def import_provider(module_name: str) -> object:
        """Return a fake provider module for the OpenAI provider name."""
        assert module_name == "pleiad.models.providers.openai"
        return SimpleNamespace(OpenAIProvider=provider_class)

    monkeypatch.setattr("pleiad.models.openai.client.import_module", import_provider)
    client = OpenAIModelClient(
        provider="openai",
        model="test-model",
        api_key="test-key",
    )

    with pytest.raises(ModelRequestError) as caught:
        asyncio.run(client.complete({"messages": []}))

    assert caught.value.code == ModelErrorCode.INVALID_REQUEST


def test_openai_model_client_rejects_unsupported_provider() -> None:
    """A concrete protocol object refuses a provider with no binding module."""
    with pytest.raises(ModelConfigurationError):
        OpenAIModelClient(provider="unknown", model="test-model", api_key="test-key")
