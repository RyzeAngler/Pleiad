import asyncio
import base64
import hashlib
import io
import json
from collections.abc import Callable

import httpx
import pytest
from PIL import Image

from pleiad.contracts import JsonObject
from pleiad.messages.images import ImageLimits, ImageReadScope, ImageReference, ResolvedImage
from pleiad.models.images.client import ImageClient
from pleiad.models.images.contracts import ImageEditRequest, ImageGenerationRequest, ImageRecovery
from pleiad.models.images.errors import ImageErrorCode, ImageOperationError

FLASH = "doubao-seedream-5-0-flash-260915"
PRO = "doubao-seedream-5-0-pro-260628"
SCOPE = ImageReadScope(session_id="session", run_id="run")


def image_bytes(image_format: str = "PNG", color: int = 0) -> bytes:
    output = io.BytesIO()
    with Image.new("RGB", (16, 16), (color, 32, 64)) as image:
        image.save(output, format=image_format)
    return output.getvalue()


def reference(data: bytes, name: str = "asset:base", width: int = 16, height: int = 16) -> ImageReference:
    return ImageReference(
        reference=name, sha256=hashlib.sha256(data).hexdigest(), mime="image/png", width=width, height=height
    )


def native_body(data: bytes, output_format: str = "png") -> JsonObject:
    return {
        "model": FLASH,
        "data": [{"b64_json": base64.b64encode(data).decode(), "output_format": output_format}],
    }


def client_for(handler: Callable[[httpx.Request], httpx.Response], model: str = FLASH) -> ImageClient:
    return ImageClient(
        provider="volcengine", model=model, api_key="private-key", transport=httpx.MockTransport(handler)
    )


@pytest.mark.parametrize("output_format", [None, "png"])
def test_volcengine_generation(output_format: str | None) -> None:
    data = image_bytes("JPEG" if output_format is None else "PNG")
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            headers={"x-request-id": "ark-request"},
            json={
                **native_body(data, "jpeg" if output_format is None else "png"),
                "usage": {"generated_images": 1, "output_tokens": 42, "total_tokens": 42, "input_images": 0},
            },
        )

    async def scenario() -> None:
        client = client_for(respond)
        try:
            request = ImageGenerationRequest.model_validate(
                {
                    "operation_id": "generation",
                    "prompt": "A product",
                    "output_format": output_format,
                    "provider_options": {"response_format": "b64_json"},
                }
            )
            result = await client.generate(request)
            assert await client.generate(request) is result
            assert len(requests) == 1
            assert requests[0].url == "https://ark.cn-beijing.volces.com/api/v3/images/generations"
            assert requests[0].headers["authorization"] == "Bearer private-key"
            expected: JsonObject = {
                "model": FLASH,
                "prompt": "A product",
                "response_format": "b64_json",
                "watermark": True,
            }
            if output_format is not None:
                expected["output_format"] = output_format
            assert json.loads(requests[0].content) == expected
            assert result.images[0].data == data
            assert result.images[0].mime == ("image/jpeg" if output_format is None else "image/png")
            assert result.model_name == FLASH
            assert result.reported_model_name == FLASH
            assert result.provider_request_id == "ark-request"
            assert result.usage is not None
            assert result.usage.image_count == 1
            assert result.usage.output_tokens == 42
            assert result.usage.total_tokens == 42
            assert result.usage.input_tokens is None
            assert client.capabilities.image_generate == "unverified"
            assert client.capabilities.reference_edit == "unverified"
            assert client.capabilities.mask_edit == "unsupported"
        finally:
            await client.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("model", "size", "mode"),
    [
        (FLASH, "960x960", "standard"),
        (FLASH, "1634x2830", "standard"),
        (FLASH, "3840x240", "standard"),
        (PRO, "1536x1536", "fast"),
    ],
)
def test_volcengine_options(model: str, size: str, mode: str) -> None:
    data = image_bytes()

    def respond(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content) == {
            "model": model,
            "prompt": "A product",
            "size": size,
            "watermark": False,
            "response_format": "b64_json",
            "optimize_prompt_options": {"mode": mode},
        }
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(data).decode()}]})

    async def scenario() -> None:
        client = client_for(respond, model)
        try:
            result = await client.generate(
                ImageGenerationRequest(
                    operation_id="options",
                    prompt="A product",
                    size=size,
                    provider_options={
                        "response_format": "b64_json",
                        "watermark": False,
                        "optimize_prompt_options": {"mode": mode},
                    },
                )
            )
            assert result.images[0].data == data
            assert result.reported_model_name is None
            assert result.usage is None
        finally:
            await client.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"count": 2}, "invalid_request"),
        ({"size": "959x961"}, "invalid_request"),
        ({"size": "1634x2831"}, "invalid_request"),
        ({"size": "3841x240"}, "invalid_request"),
        ({"size": "240x3841"}, "invalid_request"),
        ({"provider_options": {"watermark": "false"}}, "invalid_request"),
        ({"provider_options": {"optimize_prompt_options": {"mode": "fast"}}}, "invalid_request"),
        ({"provider_options": {"sequential_image_generation": "auto"}}, "invalid_request"),
        ({"quality": "high"}, "unsupported_capability"),
        ({"background": "opaque"}, "unsupported_capability"),
        ({"output_format": "webp"}, "unsupported_capability"),
    ],
)
def test_volcengine_rejects_request(changes: JsonObject, expected: ImageErrorCode) -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=native_body(image_bytes()))

    async def scenario() -> None:
        client = client_for(respond)
        try:
            request = ImageGenerationRequest.model_validate({"operation_id": "invalid", "prompt": "product", **changes})
            with pytest.raises(ImageOperationError) as caught:
                await client.generate(request)
            assert caught.value.code == expected
            assert requests == []
        finally:
            await client.aclose()

    asyncio.run(scenario())


def test_volcengine_rejects_model() -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={})

    async def scenario() -> None:
        client = client_for(respond, "doubao-seedream-4-5-251128")
        try:
            with pytest.raises(ImageOperationError) as caught:
                await client.generate(ImageGenerationRequest(operation_id="model", prompt="product"))
            assert caught.value.code == "invalid_request"
            assert requests == []
        finally:
            await client.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("count", [1, 10])
def test_volcengine_edit(count: int) -> None:
    images = tuple(image_bytes(color=index) for index in range(count))
    references = tuple(reference(data, name=f"asset:{index}") for index, data in enumerate(images))
    reads: list[tuple[ImageReference, ImageReadScope]] = []
    requests: list[httpx.Request] = []

    async def resolve(image: ImageReference, scope: ImageReadScope) -> ResolvedImage:
        reads.append((image, scope))
        return ResolvedImage(image=image, data=images[references.index(image)])

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["content-type"] == "application/json"
        assert request.url.path == "/api/v3/images/generations"
        assert json.loads(request.content) == {
            "model": FLASH,
            "prompt": "Keep product, change scene",
            "response_format": "b64_json",
            "watermark": True,
            "image": [f"data:image/png;base64,{base64.b64encode(data).decode()}" for data in images],
        }
        assert b"asset:" not in request.content
        return httpx.Response(200, json=native_body(images[0]))

    async def scenario() -> None:
        client = client_for(respond)
        try:
            result = await client.edit(
                ImageEditRequest(
                    operation_id="edit",
                    prompt="Keep product, change scene",
                    references=references,
                    base_reference=references[0],
                    provider_options={"response_format": "b64_json"},
                ),
                resolver=resolve,
                scope=SCOPE,
            )
            assert result.images[0].data == images[0]
            assert reads == [(image, SCOPE) for image in references]
            assert len(requests) == 1
        finally:
            await client.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("case", ["mask", "references", "background", "quality"])
def test_volcengine_rejects_edit(case: str) -> None:
    data = image_bytes()
    source = reference(data)
    reads: list[ImageReference] = []
    requests: list[httpx.Request] = []

    async def resolve(image: ImageReference, scope: ImageReadScope) -> ResolvedImage:
        reads.append(image)
        return ResolvedImage(image=image, data=data)

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=native_body(data))

    async def scenario() -> None:
        client = client_for(respond)
        try:
            request = ImageEditRequest(
                operation_id="edit-invalid",
                prompt="product",
                base_reference=source,
                references=(source,) * (11 if case == "references" else 1),
                mask=source if case == "mask" else None,
                background="transparent" if case == "background" else "auto",
                quality="high" if case == "quality" else "auto",
            )
            with pytest.raises(ImageOperationError) as caught:
                await client.edit(request, resolver=resolve, scope=SCOPE)
            assert caught.value.code == ("invalid_request" if case == "references" else "unsupported_capability")
            assert reads == []
            assert requests == []
        finally:
            await client.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("width", "height", "valid"),
    [
        (15, 15, True),
        (14, 15, False),
        (15, 14, False),
        (240, 15, True),
        (241, 15, False),
        (15, 241, False),
        (6000, 6000, True),
        (6000, 6001, False),
    ],
)
def test_volcengine_reference_bounds(width: int, height: int, valid: bool) -> None:
    source = reference(image_bytes(), width=width, height=height)
    reads: list[ImageReference] = []

    async def deny(image: ImageReference, scope: ImageReadScope) -> ResolvedImage:
        reads.append(image)
        raise PermissionError("host denied asset access")

    def respond(request: httpx.Request) -> httpx.Response:
        pytest.fail("Invalid or unauthorized references must not reach HTTP")

    async def scenario() -> None:
        client = client_for(respond)
        try:
            request = ImageEditRequest(
                operation_id="bounds", prompt="product", references=(source,), base_reference=source
            )
            if valid:
                with pytest.raises(PermissionError):
                    await client.edit(request, resolver=deny, scope=SCOPE)
                assert reads == [source]
            else:
                with pytest.raises(ImageOperationError) as caught:
                    await client.edit(request, resolver=deny, scope=SCOPE)
                assert caught.value.code == "invalid_request"
                assert reads == []
        finally:
            await client.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("size", [30_000_000, 30_000_001])
def test_volcengine_reference_bytes(size: int) -> None:
    png = image_bytes()
    data = png + bytes(size - len(png))
    source = reference(data)
    requests: list[httpx.Request] = []

    async def resolve(image: ImageReference, scope: ImageReadScope) -> ResolvedImage:
        return ResolvedImage(image=image, data=data)

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=native_body(png))

    async def scenario() -> None:
        client = ImageClient(
            provider="volcengine",
            model=FLASH,
            api_key="key",
            transport=httpx.MockTransport(respond),
            image_limits=ImageLimits(max_image_bytes=30_000_001),
        )
        try:
            request = ImageEditRequest(
                operation_id="bytes",
                prompt="product",
                references=(source,),
                base_reference=source,
                provider_options={"response_format": "b64_json"},
            )
            if size == 30_000_000:
                result = await client.edit(request, resolver=resolve, scope=SCOPE)
                assert result.images[0].data == png
                assert len(requests) == 1
            else:
                with pytest.raises(ImageOperationError) as caught:
                    await client.edit(request, resolver=resolve, scope=SCOPE)
                assert caught.value.code == "invalid_request"
                assert requests == []
        finally:
            await client.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("status", "code", "expected"),
    [
        (200, "AuthenticationError", "authentication"),
        (403, "AccountOverdueError", "quota"),
        (429, "QuotaExceeded", "quota"),
        (200, "ModelAccountIpmRateLimitExceeded", "rate_limit"),
        (200, "OutputImageSensitiveContentDetected", "invalid_request"),
        (400, "InvalidParameter", "invalid_request"),
        (200, "InternalServiceError", "acceptance_unknown"),
        (503, "QuotaExceeded", "acceptance_unknown"),
        (200, "UnknownFutureError", "acceptance_unknown"),
    ],
)
def test_volcengine_error(status: int, code: str, expected: ImageErrorCode) -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            status,
            headers={"X-Request-Id": "ark-error"},
            json={
                "error": {
                    "code": code,
                    "message": "private-key private-prompt",
                }
            },
        )

    async def scenario() -> None:
        client = client_for(respond)
        try:
            request = ImageGenerationRequest(operation_id="error", prompt="product")
            for attempt in range(2):
                with pytest.raises(ImageOperationError) as caught:
                    await client.generate(request)
                assert caught.value.code == expected
                assert caught.value.provider_request_id == "ark-error"
                assert caught.value.operation_id == "error"
                assert "private-key" not in str(caught.value)
                assert "private-prompt" not in str(caught.value)
                if status >= 400:
                    assert isinstance(caught.value.__cause__, httpx.HTTPStatusError)
                assert len(requests) == 1, f"Attempt {attempt} resubmitted a paid operation"
        finally:
            await client.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "case", ["empty", "multiple", "format", "bytes", "representation", "usage", "error", "envelope"]
)
def test_volcengine_invalid_response(case: str) -> None:
    data = image_bytes()
    source: JsonObject = {"b64_json": base64.b64encode(data).decode(), "output_format": "png"}
    body: JsonObject = {"data": [source]}
    if case == "empty":
        body["data"] = []
    elif case == "multiple":
        body["data"] = [source, source]
    elif case in {"format", "bytes"}:
        source["output_format"] = "jpeg"
    elif case == "representation":
        body["data"] = [{"url": "https://assets.example/result.png"}]
    elif case == "usage":
        body["usage"] = {"generated_images": True}
    elif case == "error":
        source["error"] = {"code": "InternalServiceError"}
    elif case == "envelope":
        body["error"] = {"code": 99}

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"x-request-id": "malformed-id"}, json=body)

    async def scenario() -> None:
        client = client_for(respond)
        try:
            request = ImageGenerationRequest(
                operation_id="malformed",
                prompt="product",
                output_format="png" if case == "format" else None,
                provider_options={"response_format": "b64_json"},
            )
            with pytest.raises(ImageOperationError) as caught:
                await client.generate(request)
            assert caught.value.code == "invalid_response"
            assert caught.value.provider_request_id == "malformed-id"
        finally:
            await client.aclose()

    asyncio.run(scenario())


def test_volcengine_recovery() -> None:
    data = image_bytes()
    requests: list[httpx.Request] = []
    downloads: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url == "https://proxy.example/api/v3/images/generations"
        assert json.loads(request.content)["response_format"] == "url"
        return httpx.Response(
            200,
            headers={"x-request-id": "download-id"},
            json={
                "data": [{"url": "https://assets.example/result.png", "output_format": "png"}],
                "usage": {"generated_images": 1},
            },
        )

    def download(request: httpx.Request) -> httpx.Response:
        downloads.append(request)
        assert "authorization" not in request.headers
        return httpx.Response(503) if len(downloads) == 1 else httpx.Response(200, content=data)

    async def scenario() -> None:
        client = ImageClient(
            provider="volcengine",
            model=FLASH,
            api_key="key",
            base_url="https://proxy.example/api/v3/",
            transport=httpx.MockTransport(respond),
            download_transport=httpx.MockTransport(download),
        )
        try:
            with pytest.raises(ImageOperationError) as caught:
                await client.generate(ImageGenerationRequest(operation_id="recovery", prompt="product"))
            assert caught.value.code == "download_failed"
            assert caught.value.provider_request_id == "download-id"
            assert caught.value.recovery is not None
            recovery = ImageRecovery.model_validate_json(caught.value.recovery.model_dump_json())
        finally:
            await client.aclose()
        restarted = ImageClient(
            provider="volcengine",
            model=FLASH,
            api_key="rotated",
            base_url="https://proxy.example/api/v3/",
            transport=httpx.MockTransport(respond),
            download_transport=httpx.MockTransport(download),
        )
        try:
            result = await restarted.recover(recovery)
            assert await restarted.recover(recovery) is result
            assert result.images[0].data == data
            assert result.usage is not None
            assert result.usage.image_count == 1
            assert result.usage.output_tokens is None
            assert result.usage.total_tokens is None
            assert len(requests) == 1
            assert len(downloads) == 2
        finally:
            await restarted.aclose()

    asyncio.run(scenario())
