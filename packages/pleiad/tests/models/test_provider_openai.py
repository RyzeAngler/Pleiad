import asyncio
import json
from collections.abc import AsyncIterator, Callable, Mapping
from types import SimpleNamespace

import aiohttp
import httpx
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from dashscope.aigc.generation import AioGeneration
from openai import AsyncOpenAI
from pydantic import SecretStr, TypeAdapter
from zai import ZhipuAiClient

from pleiad.contracts import JsonObject, JsonValue
from pleiad.models.errors import (
    ModelConnectionError,
    ModelRequestError,
    ModelResponseError,
    ModelTimeoutError,
    ModelUnsupportedError,
)
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
        ("volcengine", openai_provider),
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


@pytest.mark.parametrize("streaming", [False, True], ids=["complete", "stream"])
@pytest.mark.parametrize("base_url", [None, "https://gateway.example/ark/v3"], ids=["default", "custom"])
def test_volcengine_chat(monkeypatch: pytest.MonkeyPatch, streaming: bool, base_url: str | None) -> None:
    model = "doubao-seed-2-0-lite-260428"
    endpoint = "https://ark.cn-beijing.volces.com/api/v3" if base_url is None else base_url
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert str(request.url) == f"{endpoint}/chat/completions"
        assert request.headers["authorization"] == "Bearer test-key"
        assert TypeAdapter(JsonObject).validate_json(request.content) == {
            "model": model,
            "messages": [{"role": "user", "content": "hello"}],
            "max_completion_tokens": 97,
            "stream": streaming,
            "thinking": {"type": "disabled"},
        }
        usage = {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3}
        if streaming:
            chunk = {
                "id": "ark-answer",
                "object": "chat.completion.chunk",
                "created": 123,
                "model": model,
                "choices": [{"index": 0, "delta": {"content": "done"}, "finish_reason": "stop"}],
                "usage": usage,
            }
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                text=f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n",
            )
        return httpx.Response(
            200,
            json={
                "id": "ark-answer",
                "object": "chat.completion",
                "created": 123,
                "model": model,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "done"}, "finish_reason": "stop"}],
                "usage": usage,
            },
        )

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

    monkeypatch.setattr(openai_provider, "AsyncOpenAI", create_client)

    async def scenario() -> None:
        client = OpenAIModelClient(
            provider="volcengine",
            model=model,
            api_key="test-key",
            base_url=base_url,
            max_retries=0,
            provider_options={"extra_body": {"thinking": {"type": "disabled"}}},
        )
        request = OpenAIRequest.model_validate(
            {"messages": [{"role": "user", "content": "hello"}], "max_completion_tokens": 97}
        )
        try:
            if streaming:
                chunks = [chunk async for chunk in client.stream(request)]
                assert len(chunks) == 1
                assert chunks[0].id == "ark-answer"
                assert chunks[0].model == model
                assert chunks[0].choices[0].delta.content == "done"
                assert chunks[0].choices[0].finish_reason == "stop"
                usage = chunks[0].usage
            else:
                response = await client.complete(request)
                assert response.id == "ark-answer"
                assert response.model == model
                assert response.choices[0].message.content == "done"
                assert response.choices[0].finish_reason == "stop"
                usage = response.usage
            assert usage is not None
            assert (usage.prompt_tokens, usage.completion_tokens, usage.total_tokens) == (2, 1, 3)
            assert len(requests) == 1
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


def test_glm_native_completion(monkeypatch: pytest.MonkeyPatch) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://open.bigmodel.cn/api/paas/v4/chat/completions"
        payload = OpenAIRequest.model_validate_json(request.content)
        assert payload.max_tokens == 32
        assert payload.max_completion_tokens is None
        assert payload.model == "test-model"
        assert payload.model_extra == {"reasoning_effort": "low", "thinking": None}
        return httpx.Response(
            200,
            json={
                "id": "response-1",
                "created": 123,
                "model": "test-model",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "done"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
            },
        )

    def create_client(
        *, api_key: str, base_url: str, timeout: float, max_retries: int, custom_headers: Mapping[str, str]
    ) -> ZhipuAiClient:
        return ZhipuAiClient(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
            max_retries=max_retries,
            custom_headers=custom_headers,
            http_client=httpx.Client(transport=httpx.MockTransport(respond)),
        )

    monkeypatch.setattr(glm_provider, "ZhipuAiClient", create_client)

    async def scenario() -> None:
        client = OpenAIModelClient(
            provider="glm",
            model="test-model",
            api_key="test-key",
            max_retries=0,
            provider_options={"reasoning_effort": "low"},
        )
        try:
            response = await client.complete(
                {
                    "messages": [{"role": "user", "content": "hello"}],
                    "max_completion_tokens": 32,
                }
            )
            assert response.id == "response-1"
            assert response.object == "chat.completion"
            assert response.choices[0].message.content == "done"
            assert response.usage is not None and response.usage.total_tokens == 5
        finally:
            await client.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("provider", ["glm", "qwen"])
@pytest.mark.parametrize("options", [{"include_usage": 1}, {"unknown": True}])
def test_native_rejects_stream_options(provider: str, options: JsonObject) -> None:
    async def scenario() -> None:
        client = OpenAIModelClient(
            provider=provider,
            model="host-model",
            api_key="test-key",
            max_retries=0,
            base_url="http://127.0.0.1:1",
        )
        try:
            with pytest.raises(ModelUnsupportedError):
                await client.complete({"messages": [{"role": "user", "content": "hello"}], "stream_options": options})
        finally:
            await client.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("provider", ["glm", "qwen"])
def test_native_rejects_token_conflict(provider: str) -> None:
    async def scenario() -> None:
        client = OpenAIModelClient(
            provider=provider,
            model="host-model",
            api_key="test-key",
            max_retries=0,
            base_url="http://127.0.0.1:1",
        )
        try:
            with pytest.raises(ModelRequestError, match="both max_tokens"):
                await client.complete(
                    {
                        "messages": [{"role": "user", "content": "hello"}],
                        "max_tokens": 16,
                        "max_completion_tokens": 32,
                    }
                )
        finally:
            await client.aclose()

    asyncio.run(scenario())


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


@pytest.mark.parametrize("streaming", [False, True], ids=["complete", "stream"])
@pytest.mark.parametrize("image_input", [False, True], ids=["text", "image"])
def test_qwen_multimodal_http(streaming: bool, image_input: bool) -> None:
    image_url = "data:image/png;base64,aW1hZ2U="
    content: list[JsonValue] = [{"type": "text", "text": "before"}]
    native_content: list[JsonValue] = [{"text": "before"}]
    if image_input:
        content.append({"type": "image_url", "image_url": {"url": image_url}})
        native_content.append({"image": image_url})
    content.append({"type": "text", "text": "after"})
    native_content.append({"text": "after"})
    tool_call: JsonObject = {
        "index": 0,
        "id": "call-1",
        "type": "function",
        "function": {"name": "inspect", "arguments": "{}"},
    }
    messages: list[JsonValue] = [
        {"role": "system", "content": "Inspect the image."},
        {"role": "user", "content": content if image_input else "beforeafter"},
        {"role": "assistant", "content": "checking", "tool_calls": [tool_call]},
        {"role": "tool", "content": "red", "tool_call_id": "call-1"},
        {"role": "user", "content": "confirm"},
    ]
    received: list[JsonObject] = []

    async def respond(request: web.Request) -> web.Response:
        assert request.path == "/api/v1/services/aigc/multimodal-generation/generation"
        payload: JsonObject = TypeAdapter(JsonObject).validate_json(await request.text())
        received.append(payload)
        assert payload["model"] == "host-vision"
        assert payload["input"] == {
            "messages": [
                {"role": "system", "content": [{"text": "Inspect the image."}]},
                {"role": "user", "content": native_content if image_input else [{"text": "beforeafter"}]},
                {"role": "assistant", "content": [{"text": "checking"}], "tool_calls": [tool_call]},
                {"role": "tool", "content": [{"text": "red"}], "tool_call_id": "call-1"},
                {"role": "user", "content": [{"text": "confirm"}]},
            ]
        }
        parameters = payload["parameters"]
        assert isinstance(parameters, dict)
        assert "multimodal" not in parameters
        assert "stream_options" not in parameters
        assert parameters["max_tokens"] == 32
        assert parameters["incremental_output"] is streaming
        body = {
            "request_id": "qwen-vision",
            "output": {
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": [{"text": "red"}, {"text": " shirt"}],
                            "tool_calls": [tool_call],
                        },
                    }
                ],
            },
            "usage": {"input_tokens": 12, "output_tokens": 3},
        }
        if streaming:
            return web.Response(text=f"data: {json.dumps(body)}\n\n", content_type="text/event-stream")
        return web.json_response(body)

    async def scenario() -> None:
        app = web.Application()
        app.router.add_post("/api/v1/services/aigc/multimodal-generation/generation", respond)
        async with TestServer(app) as server:
            client = OpenAIModelClient(
                provider="qwen",
                model="host-vision",
                api_key="test-key",
                base_url=str(server.make_url("/api/v1")),
                max_retries=0,
                provider_options={} if image_input else {"multimodal": True},
            )
            request = OpenAIRequest.model_validate(
                {"messages": messages, "max_completion_tokens": 32, "stream_options": {"include_usage": True}}
            )
            original = request.model_dump(mode="json")
            try:
                if streaming:
                    chunks = [chunk async for chunk in client.stream(request)]
                    assert len(chunks) == 1
                    delta = chunks[0].choices[0].delta
                    assert delta.content == "red shirt"
                    assert delta.tool_calls[0].id == "call-1"
                else:
                    response = await client.complete(request)
                    assert response.choices[0].message.content == "red shirt"
                    assert response.choices[0].message.tool_calls[0].id == "call-1"
                assert request.model_dump(mode="json") == original
                assert len(received) == 1
            finally:
                await client.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "block",
    [
        {"type": "image_url", "image_url": {"url": "https://example.com/image.png", "detail": "low"}},
        {"type": "image_url", "image_url": {"url": "file:///private/image.png"}},
        {"type": "video_url", "video_url": {"url": "https://example.com/video.mp4"}},
    ],
    ids=["detail", "local-file", "video"],
)
def test_qwen_rejects_unmapped_content(block: JsonObject) -> None:
    async def scenario() -> None:
        client = OpenAIModelClient(
            provider="qwen",
            model="host-vision",
            api_key="test-key",
            base_url="http://127.0.0.1:1/api/v1",
            max_retries=0,
            provider_options={"multimodal": True},
        )
        try:
            with pytest.raises(ModelUnsupportedError):
                await client.complete({"messages": [{"role": "user", "content": [block]}]})
        finally:
            await client.aclose()

    asyncio.run(scenario())


def test_qwen_rejects_image_output() -> None:
    async def respond(request: web.Request) -> web.Response:
        assert request.path.endswith("/multimodal-generation/generation")
        return web.json_response(
            {
                "request_id": "unexpected-image",
                "output": {
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {
                                "role": "assistant",
                                "content": [{"image": "https://example.com/generated.png"}],
                            },
                        }
                    ]
                },
            }
        )

    async def scenario() -> None:
        app = web.Application()
        app.router.add_post("/api/v1/services/aigc/multimodal-generation/generation", respond)
        async with TestServer(app) as server:
            client = OpenAIModelClient(
                provider="qwen",
                model="host-model",
                api_key="test-key",
                max_retries=0,
                base_url=str(server.make_url("/api/v1")),
                provider_options={"multimodal": True},
            )
            try:
                with pytest.raises(ModelResponseError, match="unsupported content"):
                    await client.complete({"messages": [{"role": "user", "content": "draw"}]})
            finally:
                await client.aclose()

    asyncio.run(scenario())


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
