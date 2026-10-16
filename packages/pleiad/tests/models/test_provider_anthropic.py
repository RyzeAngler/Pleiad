import asyncio
from collections.abc import Callable

import pytest
from pydantic import SecretStr

from pleiad.models.anthropic.client import AnthropicProviderPort
from pleiad.models.anthropic.request import AnthropicRequest
from pleiad.models.providers import anthropic as anthropic_provider
from pleiad.models.providers import deepseek as deepseek_provider
from pleiad.models.providers import glm as glm_provider
from pleiad.models.providers import kimi as kimi_provider
from pleiad.models.providers import minimax as minimax_provider
from pleiad.models.providers import qwen as qwen_provider

from .support import FakeAnthropicClient, anthropic_raw_response


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
