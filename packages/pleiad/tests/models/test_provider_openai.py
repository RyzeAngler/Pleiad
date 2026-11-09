import asyncio
import json
from collections.abc import AsyncIterator, Callable, Mapping
from types import SimpleNamespace

import aiohttp
import httpx
import pytest
from dashscope.aigc.generation import AioGeneration
from openai import AsyncOpenAI
from pydantic import SecretStr

from pleiad.models.errors import ModelConnectionError, ModelRequestError, ModelTimeoutError
from pleiad.models.openai.client import OpenAIModelClient, OpenAIProviderPort
from pleiad.models.openai.request import OpenAIRequest
from pleiad.models.openai.response import OpenAIStreamChunk
from pleiad.models.providers import deepseek as deepseek_provider
from pleiad.models.providers import glm as glm_provider
from pleiad.models.providers import kimi as kimi_provider
from pleiad.models.providers import minimax as minimax_provider
from pleiad.models.providers import openai as openai_provider
from pleiad.models.providers import qwen as qwen_provider
from pleiad.models.result import ModelStreamEventKind

from .support import (
    FakeOpenAIClient,
    FakeQwenGeneration,
    FakeZhipuClient,
    InterruptedByteStream,
    openai_raw_response,
    openai_raw_stream,
)


@pytest.mark.parametrize(
    ("provider_name", "provider_module"),
    (
        ("openai", openai_provider),
        ("deepseek", deepseek_provider),
        ("kimi", kimi_provider),
        ("minimax", minimax_provider),
    ),
)
@pytest.mark.parametrize(
    ("transport_error", "model_error"),
    ((httpx.RemoteProtocolError, ModelConnectionError), (httpx.ReadTimeout, ModelTimeoutError)),
)
def test_interrupted_openai_response_preserves_partial_output_and_closes_without_replay(
    monkeypatch: pytest.MonkeyPatch,
    provider_name: str,
    provider_module: object,
    transport_error: type[httpx.TransportError],
    model_error: type[ModelConnectionError | ModelTimeoutError],
) -> None:
    chunk = {
        "id": "partial",
        "object": "chat.completion.chunk",
        "created": 123,
        "model": "test-model",
        "choices": [{"index": 0, "delta": {"content": "partial"}, "finish_reason": None}],
    }
    failure = transport_error("Private provider transport detail")
    stream = InterruptedByteStream(f"data: {json.dumps(chunk)}\n\n".encode(), failure)
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)

    def create_client(
        *, api_key: str, base_url: str | None, timeout: float, max_retries: int, default_headers: Mapping[str, str]
    ) -> AsyncOpenAI:
        return AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
            max_retries=max_retries,
            default_headers=default_headers,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
        )

    monkeypatch.setattr(provider_module, "AsyncOpenAI", create_client)

    async def scenario() -> None:
        client = OpenAIModelClient(provider=provider_name, model="test-model", api_key="test-key")
        partial: list[OpenAIStreamChunk] = []
        try:
            with pytest.raises(model_error) as caught:
                async for value in client.stream({"messages": [{"role": "user", "content": "hello"}]}):
                    partial.append(value)
            assert [choice.delta.content for value in partial for choice in value.choices] == ["partial"]
            assert caught.value.__cause__ is failure
            assert "Private provider transport detail" not in str(caught.value)
            assert len(requests) == 1
            assert stream.closed
        finally:
            await client.aclose()

    asyncio.run(scenario())


def test_openai_provider_sends_protocol_body_to_the_official_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    """The OpenAI provider forwards the protocol body and provider option to the SDK."""
    raw_stream = openai_raw_stream()
    FakeOpenAIClient.response = openai_raw_response()
    FakeOpenAIClient.stream = raw_stream
    FakeOpenAIClient.instances.clear()
    monkeypatch.setattr(openai_provider, "AsyncOpenAI", FakeOpenAIClient)

    provider = openai_provider.OpenAIProvider(
        model="test-model",
        api_key=SecretStr("test-key"),
        base_url="https://provider.example/v1",
        timeout_seconds=10.0,
        max_retries=1,
        provider_options={"reasoning_effort": "low"},
        headers={"x-test": "header"},
    )

    response = asyncio.run(
        provider.complete(
            OpenAIRequest.model_validate(
                {
                    "messages": [{"role": "user", "content": "hello"}],
                    "temperature": 0.2,
                },
            ),
        ),
    )

    assert response.id == "response-1"
    assert FakeOpenAIClient.instances
    instance = FakeOpenAIClient.instances[0]
    request = instance.completions.requests[0]
    assert request["model"] == "test-model"
    assert request["reasoning_effort"] == "low"


def test_openai_provider_stream_projects_sdk_chunks_and_closes_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    """The OpenAI provider projects SDK stream chunks and closes the SDK stream."""
    raw_stream = openai_raw_stream()
    FakeOpenAIClient.response = openai_raw_response()
    FakeOpenAIClient.stream = raw_stream
    FakeOpenAIClient.instances.clear()
    monkeypatch.setattr(openai_provider, "AsyncOpenAI", FakeOpenAIClient)

    provider = openai_provider.OpenAIProvider(
        model="test-model",
        api_key=SecretStr("test-key"),
        base_url=None,
        timeout_seconds=10.0,
        max_retries=1,
        provider_options={},
        headers={},
    )

    async def collect() -> list[OpenAIStreamChunk]:
        return [
            chunk
            async for chunk in provider.stream(
                OpenAIRequest.model_validate({"messages": [{"role": "user", "content": "hello"}]}),
            )
        ]

    chunks = asyncio.run(collect())

    text_events = [
        event for chunk in chunks for event in chunk.events() if event.kind is ModelStreamEventKind.TEXT_DELTA
    ]
    assert [event.text for event in text_events] == ["done"]
    assert raw_stream.closed


@pytest.mark.parametrize(
    ("provider_module", "provider_factory", "expected_base_url"),
    [
        (deepseek_provider, deepseek_provider.OpenAIProvider, "https://api.deepseek.com"),
        (minimax_provider, minimax_provider.OpenAIProvider, "https://api.minimaxi.com/v1"),
    ],
    ids=["deepseek", "minimax"],
)
def test_openai_compatible_providers_send_chat_completions_through_openai_sdk(
    monkeypatch: pytest.MonkeyPatch,
    provider_module: object,
    provider_factory: Callable[..., object],
    expected_base_url: str,
) -> None:
    """OpenAI-compatible vendors use the official OpenAI SDK with converted fields."""
    FakeOpenAIClient.response = openai_raw_response()
    FakeOpenAIClient.stream = openai_raw_stream()
    FakeOpenAIClient.instances.clear()
    monkeypatch.setattr(provider_module, "AsyncOpenAI", FakeOpenAIClient)

    provider = provider_factory(
        model="test-model",
        api_key=SecretStr("test-key"),
        base_url=None,
        timeout_seconds=10.0,
        max_retries=1,
        provider_options={"reasoning_effort": "low"},
        headers={},
    )
    assert isinstance(provider, OpenAIProviderPort)

    response = asyncio.run(
        provider.complete(
            OpenAIRequest.model_validate(
                {
                    "messages": [{"role": "user", "content": "hello"}],
                    "temperature": 0.2,
                },
            ),
        ),
    )

    assert response.id == "response-1"
    assert FakeOpenAIClient.instances
    instance = FakeOpenAIClient.instances[0]
    assert instance.settings["base_url"] == expected_base_url
    assert instance.completions.requests[0]["model"] == "test-model"
    assert instance.completions.requests[0]["reasoning_effort"] == "low"


@pytest.mark.parametrize("streaming", [False, True], ids=["complete", "stream"])
def test_deepseek_structured_output_uses_json_mode_and_preserves_schema_instructions(
    monkeypatch: pytest.MonkeyPatch, streaming: bool
) -> None:
    FakeOpenAIClient.response = openai_raw_response()
    FakeOpenAIClient.stream = openai_raw_stream()
    FakeOpenAIClient.instances.clear()
    monkeypatch.setattr(deepseek_provider, "AsyncOpenAI", FakeOpenAIClient)
    provider = deepseek_provider.OpenAIProvider(
        model="test-model",
        api_key=SecretStr("test-key"),
        base_url=None,
        timeout_seconds=10.0,
        max_retries=0,
        provider_options={},
        headers={},
    )
    request = OpenAIRequest.model_validate(
        {
            "messages": [{"role": "system", "content": "Keep it concise."}, {"role": "user", "content": "Hello"}],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "agent_output",
                    "strict": True,
                    "schema": {
                        "type": "object",
                        "properties": {"text": {"type": "string"}},
                        "required": ["text"],
                        "additionalProperties": False,
                    },
                },
            },
        }
    )
    original = request.model_dump(mode="json")

    async def run() -> None:
        if streaming:
            chunks = [chunk async for chunk in provider.stream(request)]
            assert chunks[-1].choices[0].finish_reason == "stop"
        else:
            assert (await provider.complete(request)).id == "response-1"

    asyncio.run(run())

    sent = FakeOpenAIClient.instances[0].completions.requests
    assert len(sent) == 1
    assert sent[0]["response_format"] == {"type": "json_object"}
    messages = sent[0]["messages"]
    assert isinstance(messages, list)
    instruction = messages[0]
    assert isinstance(instruction, dict) and instruction["role"] == "system"
    assert "JSON object" in instruction["content"]
    assert json.loads(instruction["content"].split("\n", 1)[1]) == original["response_format"]["json_schema"]["schema"]
    assert messages[1:] == [{"role": "system", "content": "Keep it concise."}, {"role": "user", "content": "Hello"}]
    assert request.model_dump(mode="json") == original


@pytest.mark.parametrize("definition", [None, {"name": "output"}])
def test_deepseek_rejects_a_structured_request_without_its_schema(
    monkeypatch: pytest.MonkeyPatch, definition: dict[str, str] | None
) -> None:
    FakeOpenAIClient.response = openai_raw_response()
    FakeOpenAIClient.stream = openai_raw_stream()
    FakeOpenAIClient.instances.clear()
    monkeypatch.setattr(deepseek_provider, "AsyncOpenAI", FakeOpenAIClient)
    provider = deepseek_provider.OpenAIProvider(
        model="test-model",
        api_key=SecretStr("test-key"),
        base_url=None,
        timeout_seconds=10.0,
        max_retries=0,
        provider_options={},
        headers={},
    )
    request = OpenAIRequest.model_validate(
        {
            "messages": [{"role": "user", "content": "Hello"}],
            "response_format": {"type": "json_schema", "json_schema": definition},
        }
    )

    with pytest.raises(ModelRequestError, match="cannot be converted"):
        asyncio.run(provider.complete(request))

    assert FakeOpenAIClient.instances[0].completions.requests == []


def test_glm_openai_protocol_uses_zhipu_sdk_and_restores_openai_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The GLM OpenAI adapter uses zai and projects its response to OpenAI shape."""
    FakeZhipuClient.response = openai_raw_response()
    FakeZhipuClient.instances.clear()
    monkeypatch.setattr(glm_provider, "ZhipuAiClient", FakeZhipuClient)

    provider = glm_provider.OpenAIProvider(
        model="test-model",
        api_key=SecretStr("test-key"),
        base_url=None,
        timeout_seconds=10.0,
        max_retries=1,
        provider_options={"reasoning_effort": "low"},
        headers={},
    )

    response = asyncio.run(
        provider.complete(
            OpenAIRequest.model_validate({"messages": [{"role": "user", "content": "hello"}]}),
        ),
    )

    assert response.id == "response-1"
    assert FakeZhipuClient.instances
    instance = FakeZhipuClient.instances[0]
    assert instance.settings["base_url"] == "https://open.bigmodel.cn/api/paas/v4/"
    assert instance.chat.completions.requests[0]["model"] == "test-model"
    assert instance.chat.completions.requests[0]["reasoning_effort"] == "low"


def test_qwen_openai_protocol_maps_request_and_dashscope_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Qwen adapter maps OpenAI fields to DashScope and back to OpenAI shape."""
    FakeQwenGeneration.response = SimpleNamespace(
        status_code=200,
        request_id="qwen-response-1",
        output=SimpleNamespace(text="done", finish_reason="stop"),
        usage=SimpleNamespace(input_tokens=2, output_tokens=3),
    )
    FakeQwenGeneration.requests.clear()
    monkeypatch.setattr(qwen_provider, "AioGeneration", FakeQwenGeneration)

    provider = qwen_provider.OpenAIProvider(
        model="qwen-plus",
        api_key=SecretStr("test-key"),
        base_url=None,
        timeout_seconds=10.0,
        max_retries=1,
        provider_options={},
        headers={"x-test": "header"},
    )

    response = asyncio.run(
        provider.complete(
            OpenAIRequest.model_validate(
                {
                    "messages": [{"role": "user", "content": "hello"}],
                    "max_completion_tokens": 32,
                },
            ),
        ),
    )

    assert response.id == "qwen-response-1"
    assert response.choices[0].message.content == "done"
    assert response.usage is not None
    assert response.usage.total_tokens == 5
    request = FakeQwenGeneration.requests[0]
    assert request["model"] == "qwen-plus"
    assert request["max_tokens"] == 32
    assert request["base_address"] == "https://dashscope.aliyuncs.com/api/v1"
    assert request["api_key"] == "test-key"


@pytest.mark.parametrize(
    ("transport_error", "model_error"),
    ((aiohttp.ClientPayloadError, ModelConnectionError), (TimeoutError, ModelTimeoutError)),
)
def test_interrupted_qwen_stream_keeps_partial_output_without_repeating_the_request(
    monkeypatch: pytest.MonkeyPatch,
    transport_error: type[aiohttp.ClientPayloadError | TimeoutError],
    model_error: type[ModelConnectionError | ModelTimeoutError],
) -> None:
    """A broken consumed stream is closed and its original typed failure is retained."""
    failure = transport_error("Private provider transport detail")
    closed = False
    requests: list[dict[str, object]] = []

    async def interrupted() -> AsyncIterator[object]:
        nonlocal closed
        try:
            yield SimpleNamespace(
                status_code=200,
                request_id="qwen-partial",
                output=SimpleNamespace(text="partial", finish_reason=None),
                usage=SimpleNamespace(input_tokens=2, output_tokens=1),
            )
            raise failure
        finally:
            closed = True

    async def generate(**kwargs: object) -> object:
        requests.append(kwargs)
        return interrupted()

    monkeypatch.setattr(AioGeneration, "call", generate)

    async def scenario() -> None:
        provider = qwen_provider.OpenAIProvider(
            model="qwen-plus",
            api_key=SecretStr("test-key"),
            base_url=None,
            timeout_seconds=10.0,
            max_retries=2,
            provider_options={},
            headers={},
        )
        partial: list[OpenAIStreamChunk] = []
        with pytest.raises(model_error) as caught:
            async for chunk in provider.stream(
                OpenAIRequest.model_validate({"messages": [{"role": "user", "content": "hello"}], "stream": True})
            ):
                partial.append(chunk)
        assert [choice.delta.content for chunk in partial for choice in chunk.choices] == ["partial"]
        assert len(requests) == 1 and closed
        assert caught.value.__cause__ is failure
        assert "Private provider transport detail" not in str(caught.value)

    asyncio.run(scenario())


def test_kimi_openai_protocol_uses_openai_sdk_and_preserves_developer_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Kimi adapter forwards the OpenAI body to its compatible endpoint."""
    FakeOpenAIClient.response = openai_raw_response()
    FakeOpenAIClient.stream = openai_raw_stream()
    FakeOpenAIClient.instances.clear()
    monkeypatch.setattr(kimi_provider, "AsyncOpenAI", FakeOpenAIClient)
    provider = kimi_provider.OpenAIProvider(
        model="kimi-k2",
        api_key=SecretStr("test-key"),
        base_url=None,
        timeout_seconds=10.0,
        max_retries=1,
        provider_options={"thinking": "medium"},
        headers={},
    )

    response = asyncio.run(
        provider.complete(
            OpenAIRequest.model_validate(
                {
                    "messages": [
                        {"role": "developer", "content": "rules"},
                        {"role": "user", "content": "hello"},
                    ],
                    "max_completion_tokens": 32,
                    "temperature": 0.2,
                    "top_p": 0.7,
                    "stop": ["END"],
                },
            ),
        ),
    )

    assert response.id == "response-1"
    assert FakeOpenAIClient.instances
    instance = FakeOpenAIClient.instances[0]
    assert instance.settings["base_url"] == "https://api.moonshot.ai/v1"
    request = instance.completions.requests[0]
    assert request["max_completion_tokens"] == 32
    assert request["temperature"] == 0.2
    assert request["top_p"] == 0.7
    assert request["stop"] == ["END"]
    assert request["extra_body"] == {"thinking": "medium"}
    messages = request["messages"]
    assert isinstance(messages, list)
    assert messages[0]["role"] == "developer"
