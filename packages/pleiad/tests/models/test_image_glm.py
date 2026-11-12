import asyncio
import base64
import hashlib
import json
from typing import Literal

import httpx
import pytest

from pleiad.messages.images import ImageReadScope, ImageReference, ResolvedImage
from pleiad.models.images.client import ImageClient
from pleiad.models.images.contracts import ImageCapabilities, ImageEditRequest, ImageGenerationRequest
from pleiad.models.images.errors import ImageOperationError

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC")


@pytest.mark.parametrize(
    "model,quality,user_id",
    [("glm-image", "hd", "user-1"), ("cogview-4-250304", "standard", "user-1"), ("cogview-4", "hd", "u" * 128)],
)
def test_glm_native_generation(model: str, quality: str, user_id: str) -> None:
    requests: list[httpx.Request] = []
    downloads: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            headers={"x-request-id": "glm-request"},
            json={"created": 1, "data": [{"url": "https://images.example/original.png"}]},
        )

    def download(request: httpx.Request) -> httpx.Response:
        downloads.append(request)
        return httpx.Response(200, content=PNG, headers={"content-type": "image/png"})

    async def scenario() -> None:
        client = ImageClient(
            provider="glm",
            model=model,
            api_key="private-key",
            transport=httpx.MockTransport(respond),
            download_transport=httpx.MockTransport(download),
        )
        try:
            request = ImageGenerationRequest(
                operation_id="product",
                prompt="A product",
                size="1024x1024",
                provider_options={"quality": quality, "watermark_enabled": False, "user_id": user_id},
            )
            result = await client.generate(request)
            assert await client.generate(request) is result
            assert len(requests) == 1
            assert str(requests[0].url) == "https://open.bigmodel.cn/api/paas/v4/images/generations"
            assert requests[0].headers["authorization"] == "Bearer private-key"
            assert json.loads(requests[0].content) == {
                "model": model,
                "prompt": "A product",
                "size": "1024x1024",
                "quality": quality,
                "watermark_enabled": False,
                "user_id": user_id,
            }
            assert len(downloads) == 1
            assert str(downloads[0].url) == "https://images.example/original.png"
            assert "authorization" not in downloads[0].headers
            assert result.model_name == model
            assert result.provider_request_id == "glm-request"
            assert result.reported_model_name is None
            assert result.usage is None
            assert len(result.images) == 1
            assert result.images[0].data == PNG
            assert result.images[0].mime == "image/png"
            assert result.images[0].revised_prompt is None
        finally:
            await client.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("model", ["glm-image", "cogview-4-250304", "cogview-4", "cogview-3-flash"])
def test_glm_defaults(model: str) -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"data": [{"url": "https://images.example/image"}], "request_id": "body-id"})

    async def scenario() -> None:
        client = ImageClient(
            provider="glm",
            model=model,
            api_key="key",
            base_url="https://gateway.example/native/v4",
            transport=httpx.MockTransport(respond),
            download_transport=httpx.MockTransport(lambda request: httpx.Response(200, content=PNG)),
        )
        try:
            result = await client.generate(ImageGenerationRequest(operation_id="op", prompt="A product"))
            assert len(requests) == 1
            assert str(requests[0].url) == "https://gateway.example/native/v4/images/generations"
            assert json.loads(requests[0].content) == {"model": model, "prompt": "A product"}
            assert result.provider_request_id == "body-id"
            assert result.images[0].data == PNG
        finally:
            await client.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "model,size",
    [("glm-image", "512x512"), ("glm-image", "2048x2048"), ("cogview-4", "512x512"), ("cogview-4", "2048x1024")],
)
def test_glm_size_limits(model: str, size: str) -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"data": [{"url": "https://images.example/image"}]})

    async def scenario() -> None:
        client = ImageClient(
            provider="glm",
            model=model,
            api_key="key",
            transport=httpx.MockTransport(respond),
            download_transport=httpx.MockTransport(lambda request: httpx.Response(200, content=PNG)),
        )
        try:
            result = await client.generate(ImageGenerationRequest(operation_id="op", prompt="A product", size=size))
            assert json.loads(requests[0].content)["size"] == size
            assert result.images[0].data == PNG
        finally:
            await client.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "model,changes",
    [
        ("glm-5", {}),
        ("glm-image", {"count": 2}),
        ("glm-image", {"size": "480x512"}),
        ("glm-image", {"size": "512x480"}),
        ("glm-image", {"size": "2080x512"}),
        ("glm-image", {"size": "528x512"}),
        ("cogview-4", {"size": "496x512"}),
        ("cogview-4", {"size": "2064x512"}),
        ("cogview-4", {"size": "512x2064"}),
        ("cogview-4", {"size": "520x512"}),
        ("cogview-4", {"size": "2048x1040"}),
        ("glm-image", {"quality": "high"}),
        ("glm-image", {"background": "opaque"}),
        ("glm-image", {"output_format": "png"}),
        ("glm-image", {"provider_options": {"quality": "standard"}}),
        ("glm-image", {"provider_options": {"watermark_enabled": "false"}}),
        ("glm-image", {"provider_options": {"user_id": "short"}}),
        ("glm-image", {"provider_options": {"user_id": "u" * 129}}),
        ("glm-image", {"provider_options": {"n": 2}}),
    ],
)
def test_glm_rejects_options(model: str, changes: dict[str, object]) -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        pytest.fail("invalid options must not reach the provider")

    async def scenario() -> None:
        client = ImageClient(provider="glm", model=model, api_key="key", transport=httpx.MockTransport(respond))
        try:
            request = ImageGenerationRequest.model_validate({"operation_id": "op", "prompt": "A product", **changes})
            with pytest.raises(ImageOperationError) as caught:
                await client.generate(request)
            assert caught.value.code == "invalid_request"
            assert requests == []
        finally:
            await client.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "model,length,accepted", [("glm-image", 1000, True), ("glm-image", 1001, False), ("cogview-4", 1001, True)]
)
def test_glm_prompt_limit(model: str, length: int, accepted: bool) -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"data": [{"url": "https://images.example/image"}]})

    async def scenario() -> None:
        client = ImageClient(
            provider="glm",
            model=model,
            api_key="key",
            transport=httpx.MockTransport(respond),
            download_transport=httpx.MockTransport(lambda request: httpx.Response(200, content=PNG)),
        )
        try:
            request = ImageGenerationRequest(operation_id="op", prompt="猫" * length)
            if accepted:
                result = await client.generate(request)
                assert result.images[0].data == PNG
                assert json.loads(requests[0].content)["prompt"] == "猫" * length
            else:
                with pytest.raises(ImageOperationError) as caught:
                    await client.generate(request)
                assert caught.value.code == "invalid_request"
                assert requests == []
        finally:
            await client.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("kind,masked", [("general", False), ("character", False), ("general", True)])
def test_glm_rejects_edit(kind: Literal["general", "character"], masked: bool) -> None:
    async def resolve(image: ImageReference, scope: ImageReadScope) -> ResolvedImage:
        pytest.fail("unsupported editing must not access reference images")

    def respond(request: httpx.Request) -> httpx.Response:
        pytest.fail("unsupported editing must not submit a paid request")

    async def scenario() -> None:
        client = ImageClient(
            provider="glm",
            model="glm-image",
            api_key="key",
            transport=httpx.MockTransport(respond),
            capabilities=ImageCapabilities(
                reference_edit="supported", character_reference="supported", mask_edit="supported"
            ),
        )
        image = ImageReference(
            reference="asset:product", sha256=hashlib.sha256(PNG).hexdigest(), mime="image/png", width=1, height=1
        )
        try:
            with pytest.raises(ImageOperationError) as caught:
                await client.edit(
                    ImageEditRequest(
                        operation_id="op",
                        prompt="Edit",
                        references=(image,),
                        base_reference=image,
                        mask=image if masked else None,
                        reference_kind=kind,
                    ),
                    resolver=resolve,
                    scope=ImageReadScope(session_id="session", run_id="run"),
                )
            assert caught.value.code == "unsupported_capability"
        finally:
            await client.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"data": []},
        {"data": None},
        {"data": [{}]},
        {"data": [{"url": ""}]},
        {"data": [{"b64_json": "unexpected"}]},
        {"data": [{"url": "https://images.example/1"}, {"url": "https://images.example/2"}]},
    ],
)
def test_glm_invalid_response(body: dict[str, object]) -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=body, headers={"x-request-id": "malformed"})

    async def scenario() -> None:
        client = ImageClient(provider="glm", model="glm-image", api_key="key", transport=httpx.MockTransport(respond))
        try:
            request = ImageGenerationRequest(operation_id="op", prompt="A product")
            with pytest.raises(ImageOperationError) as caught:
                await client.generate(request)
            with pytest.raises(ImageOperationError) as repeated:
                await client.generate(request)
            assert caught.value.code == "invalid_response"
            assert caught.value.provider_request_id == "malformed"
            assert repeated.value is caught.value
            assert len(requests) == 1
        finally:
            await client.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "status,code,expected",
    [
        (401, "1000", "authentication"),
        (429, "1113", "quota"),
        (429, "1302", "rate_limit"),
        (400, "1301", "invalid_request"),
        (500, "1210", "acceptance_unknown"),
        (408, "1210", "acceptance_unknown"),
        (200, "1301", "invalid_request"),
        (200, "unknown", "acceptance_unknown"),
    ],
)
def test_glm_business_error(status: int, code: str, expected: str) -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            status,
            json={"error": {"code": code, "message": "private prompt and secret"}},
            headers={"x-request-id": "failed"},
        )

    async def scenario() -> None:
        client = ImageClient(provider="glm", model="glm-image", api_key="key", transport=httpx.MockTransport(respond))
        try:
            request = ImageGenerationRequest(operation_id="op", prompt="A product")
            with pytest.raises(ImageOperationError) as caught:
                await client.generate(request)
            assert caught.value.code == expected
            assert caught.value.provider_request_id == "failed"
            assert "private" not in str(caught.value)
            assert "secret" not in str(caught.value)
            if status >= 400:
                assert isinstance(caught.value.__cause__, httpx.HTTPStatusError)
            with pytest.raises(ImageOperationError) as repeated:
                await client.generate(request)
            assert repeated.value is caught.value
            assert len(requests) == 1
        finally:
            await client.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("status,expected", [(200, "invalid_response"), (503, "acceptance_unknown")])
def test_glm_http_error(status: int, expected: str) -> None:
    async def scenario() -> None:
        client = ImageClient(
            provider="glm",
            model="glm-image",
            api_key="key",
            transport=httpx.MockTransport(lambda request: httpx.Response(status, text="not JSON")),
        )
        try:
            with pytest.raises(ImageOperationError) as caught:
                await client.generate(ImageGenerationRequest(operation_id="op", prompt="A product"))
            assert caught.value.code == expected
            assert "not JSON" not in str(caught.value)
        finally:
            await client.aclose()

    asyncio.run(scenario())
