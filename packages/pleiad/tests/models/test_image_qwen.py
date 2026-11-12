import asyncio
import base64
import hashlib
import json
from collections.abc import Callable

import httpx
import pytest

from pleiad.contracts import JsonObject
from pleiad.messages.images import ImageReadScope, ImageReference, ResolvedImage
from pleiad.models.images.client import ImageClient
from pleiad.models.images.contracts import ImageEditRequest, ImageGenerationRequest
from pleiad.models.images.errors import ImageErrorCode, ImageOperationError

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC")
SECOND_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGNgYPgPAAEDAQAIicLsAAAAAElFTkSuQmCC"
)
SCOPE = ImageReadScope(session_id="session", run_id="run")


def reference(name: str, data: bytes = PNG) -> ImageReference:
    return ImageReference(reference=name, sha256=hashlib.sha256(data).hexdigest(), mime="image/png", width=1, height=1)


def native_body() -> dict[str, object]:
    return {
        "request_id": "native-request",
        "output": {
            "choices": [{"message": {"role": "assistant", "content": [{"image": "https://images.example/result.png"}]}}]
        },
    }


def download(request: httpx.Request) -> httpx.Response:
    assert request.method == "GET"
    assert request.url == "https://images.example/result.png"
    assert "authorization" not in request.headers
    return httpx.Response(200, content=PNG)


def client_for(handler: Callable[[httpx.Request], httpx.Response], model: str = "qwen-image-3.0-pro") -> ImageClient:
    return ImageClient(
        provider="qwen",
        model=model,
        api_key="private-key",
        transport=httpx.MockTransport(handler),
        download_transport=httpx.MockTransport(download),
    )


@pytest.mark.parametrize(
    ("model", "usage", "image_count"),
    [
        ("qwen-image-2.0-pro", {"image_count": 1, "width": 1024, "height": 1024}, 1),
        ("qwen-image-2.1-turbo", {"image_count": 1}, 1),
        ("qwen-image-3.0", {"output_image_count": 1, "input_image_count": 3}, 1),
        ("custom-image-alias", {"input_image_count": 3}, None),
    ],
)
def test_qwen_native_generation(model: str, usage: dict[str, int], image_count: int | None) -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={**native_body(), "usage": usage})

    async def scenario() -> None:
        client = client_for(respond, model)
        try:
            result = await client.generate(ImageGenerationRequest(operation_id="op", prompt="A red mug"))
            assert requests[0].url == (
                "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation"
            )
            assert requests[0].headers["Authorization"] == "Bearer private-key"
            assert json.loads(requests[0].content) == {
                "model": model,
                "input": {"messages": [{"role": "user", "content": [{"text": "A red mug"}]}]},
                "parameters": {"n": 1},
            }
            assert result.provider_request_id == "native-request"
            assert result.images[0].data == PNG
            assert result.images[0].mime == "image/png"
            assert result.reported_model_name is None
            assert result.usage is not None
            assert result.usage.image_count == image_count
            assert result.usage.input_tokens is None
            assert result.usage.output_tokens is None
            assert result.usage.total_tokens is None
        finally:
            await client.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("seed", [0, 2_147_483_647])
def test_qwen_native_options(seed: int) -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=native_body())

    async def scenario() -> None:
        client = client_for(respond)
        try:
            result = await client.generate(
                ImageGenerationRequest(
                    operation_id="op",
                    prompt="A red mug",
                    count=6,
                    size="1024x1536",
                    output_format="png",
                    provider_options={
                        "negative_prompt": "blur",
                        "seed": seed,
                        "watermark": False,
                        "prompt_extend": True,
                        "prompt_extend_mode": "agent",
                        "enable_thinking": False,
                    },
                )
            )
            assert json.loads(requests[0].content)["parameters"] == {
                "n": 6,
                "size": "1024*1536",
                "negative_prompt": "blur",
                "seed": seed,
                "watermark": False,
                "prompt_extend": True,
                "prompt_extend_mode": "agent",
                "enable_thinking": False,
            }
            assert len(result.images) == 1
        finally:
            await client.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "model", ["qwen-image-3.0-pro", "qwen-image-2.0-pro", "qwen-image-edit", "qwen-image-edit-plus"]
)
def test_qwen_reference_order(model: str) -> None:
    requests: list[httpx.Request] = []
    reads: list[tuple[ImageReference, ImageReadScope]] = []
    first, second = reference("asset:first"), reference("asset:second", SECOND_PNG)

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=native_body())

    async def resolve(image: ImageReference, scope: ImageReadScope) -> ResolvedImage:
        reads.append((image, scope))
        return ResolvedImage(image=image, data=SECOND_PNG if image == second else PNG)

    async def scenario() -> None:
        client = client_for(respond, model)
        try:
            result = await client.edit(
                ImageEditRequest(
                    operation_id="edit", prompt="Combine these", references=(first, second, first), base_reference=first
                ),
                resolver=resolve,
                scope=SCOPE,
            )
            assert result.images[0].data == PNG
            assert reads == [(first, SCOPE), (second, SCOPE), (first, SCOPE)]
            assert len(requests) == 1
            assert json.loads(requests[0].content)["input"] == {
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"image": f"data:image/png;base64,{base64.b64encode(PNG).decode()}"},
                            {"image": f"data:image/png;base64,{base64.b64encode(SECOND_PNG).decode()}"},
                            {"image": f"data:image/png;base64,{base64.b64encode(PNG).decode()}"},
                            {"text": "Combine these"},
                        ],
                    }
                ]
            }
            assert b"asset:" not in requests[0].content
        finally:
            await client.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("size", ["512x512", "2048x2048", "256x2048", "2048x256"])
def test_qwen_size_limits(size: str) -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=native_body())

    async def scenario() -> None:
        client = client_for(respond)
        try:
            await client.generate(ImageGenerationRequest(operation_id="op", prompt="A mug", size=size))
            assert json.loads(requests[0].content)["parameters"]["size"] == size.replace("x", "*")
        finally:
            await client.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("model", "fields"),
    [
        ("qwen-image-3.0-pro", {"count": 7}),
        ("qwen-image-3.0-pro", {"size": "511x512"}),
        ("qwen-image-3.0-pro", {"size": "2048x2049"}),
        ("qwen-image-3.0-pro", {"size": "2049x256"}),
        ("qwen-image-3.0-pro", {"quality": "high"}),
        ("qwen-image-3.0-pro", {"output_format": "webp"}),
        ("qwen-image-3.0-pro", {"background": "opaque"}),
        ("qwen-image", {}),
        ("qwen-image-plus", {}),
        ("qwen-image-max-2026-01-23", {}),
        ("qwen-image-edit-plus", {}),
    ],
)
def test_qwen_rejects_request(model: str, fields: dict[str, object]) -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=native_body())

    async def scenario() -> None:
        client = client_for(respond, model)
        try:
            request = ImageGenerationRequest.model_validate({"operation_id": "op", "prompt": "A mug", **fields})
            with pytest.raises(ImageOperationError) as caught:
                await client.generate(request)
            assert caught.value.code == "invalid_request"
            assert requests == []
        finally:
            await client.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("model", "options"),
    [
        ("qwen-image-3.0", {"n": 6}),
        ("qwen-image-3.0", {"seed": -1}),
        ("qwen-image-3.0", {"seed": 2_147_483_648}),
        ("qwen-image-3.0", {"seed": True}),
        ("qwen-image-3.0", {"watermark": "false"}),
        ("qwen-image-3.0", {"negative_prompt": "x" * 501}),
        ("qwen-image-3.0", {"prompt_extend_mode": "invalid"}),
        ("qwen-image-3.0", {"enable_thinking": True, "prompt_extend": False}),
        ("qwen-image-2.1-turbo", {"negative_prompt": "blur"}),
        ("qwen-image-2.0-pro", {"enable_thinking": False}),
        ("qwen-image-2.0-pro", {"prompt_extend_mode": "direct"}),
    ],
)
def test_qwen_rejects_options(model: str, options: JsonObject) -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=native_body())

    async def scenario() -> None:
        client = client_for(respond, model)
        try:
            with pytest.raises(ImageOperationError) as caught:
                await client.generate(
                    ImageGenerationRequest(operation_id="op", prompt="A mug", provider_options=options)
                )
            assert caught.value.code == "invalid_request"
            assert requests == []
        finally:
            await client.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize(("model", "limit"), [("qwen-image-3.0", 3), ("qwen-image-2.1-pro", 10)])
def test_qwen_reference_limit(model: str, limit: int) -> None:
    requests: list[httpx.Request] = []
    reads: list[ImageReference] = []
    image = reference("asset:image")

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=native_body())

    async def resolve(image: ImageReference, scope: ImageReadScope) -> ResolvedImage:
        reads.append(image)
        return ResolvedImage(image=image, data=PNG)

    async def scenario() -> None:
        client = client_for(respond, model)
        try:
            await client.edit(
                ImageEditRequest(
                    operation_id="valid", prompt="edit", references=(image,) * limit, base_reference=image
                ),
                resolver=resolve,
                scope=SCOPE,
            )
            with pytest.raises(ImageOperationError) as caught:
                await client.edit(
                    ImageEditRequest(
                        operation_id="invalid", prompt="edit", references=(image,) * (limit + 1), base_reference=image
                    ),
                    resolver=resolve,
                    scope=SCOPE,
                )
            assert caught.value.code == "invalid_request"
            assert len(requests) == 1
            assert len(reads) == limit
            assert len(json.loads(requests[0].content)["input"]["messages"][0]["content"]) == limit + 1
        finally:
            await client.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("model", "fields", "code"),
    [
        ("qwen-image-edit", {"count": 2}, "invalid_request"),
        ("qwen-image-edit", {"size": "1024x1024"}, "invalid_request"),
        ("qwen-image-3.0", {"provider_options": {"prompt_extend_mode": "agent"}}, "invalid_request"),
        ("qwen-image-3.0", {"reference_kind": "character"}, "unsupported_capability"),
        ("qwen-image-3.0", {"background": "transparent"}, "unsupported_capability"),
        ("qwen-image-3.0", {"mask": reference("asset:mask")}, "unsupported_capability"),
    ],
)
def test_qwen_rejects_edit(model: str, fields: dict[str, object], code: ImageErrorCode) -> None:
    requests: list[httpx.Request] = []
    reads: list[ImageReference] = []
    image = reference("asset:image")

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=native_body())

    async def resolve(image: ImageReference, scope: ImageReadScope) -> ResolvedImage:
        reads.append(image)
        return ResolvedImage(image=image, data=PNG)

    async def scenario() -> None:
        client = client_for(respond, model)
        try:
            request = ImageEditRequest.model_validate(
                {"operation_id": "op", "prompt": "edit", "references": (image,), "base_reference": image, **fields}
            )
            with pytest.raises(ImageOperationError) as caught:
                await client.edit(request, resolver=resolve, scope=SCOPE)
            assert caught.value.code == code
            assert requests == []
            assert reads == []
        finally:
            await client.aclose()

    asyncio.run(scenario())


def test_qwen_partial_result() -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=native_body())

    async def scenario() -> None:
        client = client_for(respond)
        try:
            request = ImageGenerationRequest(operation_id="op", prompt="A mug", count=6)
            result = await client.generate(request)
            repeated = await client.generate(request)
            assert repeated is result
            assert len(result.images) == 1
            assert result.usage is None
            assert len(requests) == 1
        finally:
            await client.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "body",
    [
        {"output": None},
        {"output": {"choices": []}},
        {"output": {"choices": [{"message": {"role": "assistant", "content": []}}]}},
        {"output": {"choices": [{"message": {"role": "assistant", "content": [{"text": "No image"}]}}]}},
        {"output": {"choices": [{"message": {"role": "assistant", "content": [{"image": ""}]}}]}},
        {"output": {"choices": [{"message": {"role": "assistant", "content": [{}]}}]}},
        {**native_body(), "usage": {"image_count": "1"}},
        {**native_body(), "usage": {"output_image_count": -1}},
    ],
)
def test_qwen_invalid_response(body: dict[str, object]) -> None:
    async def scenario() -> None:
        client = client_for(lambda request: httpx.Response(200, json={**body, "request_id": "known-request"}))
        try:
            with pytest.raises(ImageOperationError) as caught:
                await client.generate(ImageGenerationRequest(operation_id="op", prompt="A mug"))
            assert caught.value.code == "invalid_response"
            assert caught.value.provider_request_id == "known-request"
            assert caught.value.__cause__ is not None
        finally:
            await client.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("status", "native_code", "error_code"),
    [
        (401, "InvalidApiKey", "authentication"),
        (403, "AccessDenied", "authentication"),
        (403, "Arrearage", "quota"),
        (429, "Throttling.AllocationQuota", "quota"),
        (429, "Throttling.RateQuota", "rate_limit"),
        (400, "InvalidParameter", "invalid_request"),
        (404, "ModelNotFound", "invalid_request"),
        (500, "InternalError", "acceptance_unknown"),
        (200, "InvalidParameter", "invalid_request"),
        (200, "UnexpectedError", "invalid_response"),
    ],
)
def test_qwen_safe_errors(status: int, native_code: str, error_code: ImageErrorCode) -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            status,
            json={"code": native_code, "message": "secret signed-url private-key", "request_id": "failure-request"},
        )

    async def scenario() -> None:
        client = client_for(respond)
        try:
            request = ImageGenerationRequest(operation_id="op", prompt="A mug")
            for attempt in range(2):
                with pytest.raises(ImageOperationError) as caught:
                    await client.generate(request)
                assert caught.value.code == error_code
                assert caught.value.provider_request_id == "failure-request"
                assert "secret" not in str(caught.value)
                assert "signed-url" not in str(caught.value)
                assert "private-key" not in str(caught.value)
                assert len(requests) == 1, attempt
        finally:
            await client.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("extra_bytes", [0, 1])
def test_qwen_input_byte_limit(extra_bytes: int) -> None:
    data = PNG + bytes(10 * 1024 * 1024 - len(PNG) + extra_bytes)
    image = reference("asset:large", data)
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=native_body())

    async def resolve(image: ImageReference, scope: ImageReadScope) -> ResolvedImage:
        return ResolvedImage(image=image, data=data)

    async def scenario() -> None:
        client = client_for(respond)
        try:
            request = ImageEditRequest(operation_id="edit", prompt="edit", references=(image,), base_reference=image)
            if extra_bytes:
                with pytest.raises(ImageOperationError) as caught:
                    await client.edit(request, resolver=resolve, scope=SCOPE)
                assert caught.value.code == "invalid_request"
                assert requests == []
            else:
                result = await client.edit(request, resolver=resolve, scope=SCOPE)
                assert result.images[0].data == PNG
                assert len(requests) == 1
        finally:
            await client.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize(("width", "height"), [(8001, 1000), (11, 1), (1, 11)])
def test_qwen_input_dimensions(width: int, height: int) -> None:
    image = ImageReference(
        reference="asset:dimensions",
        sha256=hashlib.sha256(PNG).hexdigest(),
        mime="image/png",
        width=width,
        height=height,
    )
    requests: list[httpx.Request] = []
    reads: list[ImageReference] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=native_body())

    async def resolve(image: ImageReference, scope: ImageReadScope) -> ResolvedImage:
        reads.append(image)
        return ResolvedImage(image=image, data=PNG)

    async def scenario() -> None:
        client = client_for(respond)
        try:
            with pytest.raises(ImageOperationError) as caught:
                await client.edit(
                    ImageEditRequest(operation_id="edit", prompt="edit", references=(image,), base_reference=image),
                    resolver=resolve,
                    scope=SCOPE,
                )
            assert caught.value.code == "invalid_request"
            assert requests == []
            assert reads == []
        finally:
            await client.aclose()

    asyncio.run(scenario())


def test_qwen_invalid_json_request_id() -> None:
    async def scenario() -> None:
        client = client_for(
            lambda request: httpx.Response(200, headers={"x-request-id": "header-request"}, content=b"not-json")
        )
        try:
            with pytest.raises(ImageOperationError) as caught:
                await client.generate(ImageGenerationRequest(operation_id="op", prompt="A mug"))
            assert caught.value.code == "invalid_response"
            assert caught.value.provider_request_id == "header-request"
        finally:
            await client.aclose()

    asyncio.run(scenario())
