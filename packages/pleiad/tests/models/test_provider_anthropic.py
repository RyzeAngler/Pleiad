import asyncio
import json
from collections.abc import Callable, Mapping

import httpx
import pytest
from anthropic import AsyncAnthropic
from pydantic import SecretStr

from pleiad.models.anthropic.client import AnthropicModelClient, AnthropicProviderPort
from pleiad.models.anthropic.request import AnthropicRequest
from pleiad.models.anthropic.response import AnthropicStreamEvent
from pleiad.models.providers import anthropic as anthropic_provider
from pleiad.models.providers import deepseek as deepseek_provider
from pleiad.models.providers import glm as glm_provider
from pleiad.models.providers import kimi as kimi_provider
from pleiad.models.providers import minimax as minimax_provider
from pleiad.models.providers import qwen as qwen_provider

from .support import FakeAnthropicClient, anthropic_raw_response


@pytest.mark.parametrize(
    ("provider_name", "provider_module"),
    [
        ("anthropic", anthropic_provider),
        ("deepseek", deepseek_provider),
        ("glm", glm_provider),
        ("qwen", qwen_provider),
        ("kimi", kimi_provider),
        ("minimax", minimax_provider),
    ],
)
def test_anthropic_stream_reaches_the_official_sdk_and_returns_text(
    monkeypatch: pytest.MonkeyPatch,
    provider_name: str,
    provider_module: object,
) -> None:
    events = (
        {
            "type": "message_start",
            "message": {
                "id": "message-stream-1",
                "type": "message",
                "role": "assistant",
                "content": [],
                "model": "test-model",
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 2, "output_tokens": 0},
            },
        },
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "done"}},
        {"type": "content_block_stop", "index": 0},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn", "stop_sequence": None},
            "usage": {"output_tokens": 1},
        },
        {"type": "message_stop"},
    )
    body = "".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events)

    def handle_request(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/messages")
        payload = AnthropicRequest.model_validate_json(request.content)
        assert payload.stream is True
        assert payload.model == "test-model"
        assert payload.max_tokens == 32
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    def create_client(
        *,
        api_key: str,
        base_url: str | None,
        timeout: float,
        max_retries: int,
        default_headers: Mapping[str, str],
    ) -> AsyncAnthropic:
        return AsyncAnthropic(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
            max_retries=max_retries,
            default_headers=default_headers,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle_request)),
        )

    monkeypatch.setattr(provider_module, "AsyncAnthropic", create_client)

    async def scenario() -> list[AnthropicStreamEvent]:
        client = AnthropicModelClient(provider=provider_name, model="test-model", api_key="test-key")
        try:
            return [
                event
                async for event in client.stream(
                    {"messages": [{"role": "user", "content": "hello"}], "max_tokens": 32},
                )
            ]
        finally:
            await client.aclose()

    response = asyncio.run(scenario())

    assert any(event.delta == {"type": "text_delta", "text": "done"} for event in response)
    assert response[-1].type == "message_stop"


def test_anthropic_provider_sends_protocol_body_to_the_official_sdk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Anthropic provider forwards the protocol body to the official SDK."""
    FakeAnthropicClient.response = anthropic_raw_response()
    FakeAnthropicClient.instances.clear()
    monkeypatch.setattr(anthropic_provider, "AsyncAnthropic", FakeAnthropicClient)

    provider = anthropic_provider.AnthropicProvider(
        model="test-model",
        api_key=SecretStr("test-key"),
        base_url="https://provider.example",
        timeout_seconds=10.0,
        max_retries=1,
        provider_options={},
        headers={"x-test": "header"},
    )

    response = asyncio.run(
        provider.complete(
            AnthropicRequest.model_validate(
                {
                    "messages": [{"role": "user", "content": "hello"}],
                    "max_tokens": 32,
                    "top_k": 2,
                },
            ),
        ),
    )

    assert response.id == "message-1"
    assert FakeAnthropicClient.instances
    request = FakeAnthropicClient.instances[0].messages.requests[0]
    assert request["model"] == "test-model"
    assert request["max_tokens"] == 32
    assert request["top_k"] == 2


@pytest.mark.parametrize(
    ("provider_module", "provider_factory", "expected_base_url"),
    [
        (deepseek_provider, deepseek_provider.AnthropicProvider, "https://api.deepseek.com/anthropic"),
        (glm_provider, glm_provider.AnthropicProvider, "https://open.bigmodel.cn/api/anthropic"),
        (qwen_provider, qwen_provider.AnthropicProvider, "https://dashscope.aliyuncs.com/apps/anthropic"),
        (kimi_provider, kimi_provider.AnthropicProvider, "https://api.moonshot.ai/anthropic"),
        (minimax_provider, minimax_provider.AnthropicProvider, "https://api.minimaxi.com/anthropic"),
    ],
    ids=["deepseek", "glm", "qwen", "kimi", "minimax"],
)
def test_anthropic_compatible_providers_send_messages_through_anthropic_sdk(
    monkeypatch: pytest.MonkeyPatch,
    provider_module: object,
    provider_factory: Callable[..., object],
    expected_base_url: str,
) -> None:
    """Every provider with an Anthropic endpoint uses the official Anthropic SDK."""
    FakeAnthropicClient.response = anthropic_raw_response()
    FakeAnthropicClient.instances.clear()
    monkeypatch.setattr(provider_module, "AsyncAnthropic", FakeAnthropicClient)

    provider = provider_factory(
        model="test-model",
        api_key=SecretStr("test-key"),
        base_url=None,
        timeout_seconds=10.0,
        max_retries=1,
        provider_options={},
        headers={},
    )
    assert isinstance(provider, AnthropicProviderPort)

    response = asyncio.run(
        provider.complete(
            AnthropicRequest.model_validate(
                {
                    "messages": [{"role": "user", "content": "hello"}],
                    "max_tokens": 32,
                    "top_k": 2,
                },
            ),
        ),
    )

    assert response.id == "message-1"
    assert FakeAnthropicClient.instances
    instance = FakeAnthropicClient.instances[0]
    assert instance.settings["base_url"] == expected_base_url
    assert instance.messages.requests[0]["model"] == "test-model"
    assert instance.messages.requests[0]["top_k"] == 2
