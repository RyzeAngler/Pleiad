import asyncio
import base64
import hashlib
import json
from collections.abc import Callable

import httpx
import pytest

from pleiad.contracts import JsonObject, JsonValue
from pleiad.messages.images import ImageReadScope, ImageReference, ResolvedImage
from pleiad.models.images.client import ImageClient
from pleiad.models.images.contracts import ImageEditRequest, ImageGenerationRequest
from pleiad.models.images.errors import ImageErrorCode, ImageOperationError

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC")
SCOPE = ImageReadScope(session_id="session", run_id="run")


def image_reference(data: bytes = PNG, mime: str = "image/png") -> ImageReference:
    return ImageReference(
        reference="asset:portrait", sha256=hashlib.sha256(data).hexdigest(), mime=mime, width=1, height=1
    )


def response_body() -> JsonObject:
    return {
        "id": "minimax-trace",
        "base_resp": {"status_code": 0, "status_msg": "success"},
        "data": {"image_base64": [base64.b64encode(PNG).decode()]},
    }


def client_for(handler: Callable[[httpx.Request], httpx.Response], model: str = "image-01") -> ImageClient:
    return ImageClient(provider="minimax", model=model, api_key="private-key", transport=httpx.MockTransport(handler))


@pytest.mark.parametrize(
    ("base_url", "endpoint"),
    [
        (None, "https://api.minimax.io/v1/image_generation"),
        ("https://proxy.example/native/v1/", "https://proxy.example/native/v1/image_generation"),
    ],
)
def test_minimax_native_generation(base_url: str | None, endpoint: str) -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=response_body())

    async def scenario() -> None:
        client = ImageClient(
            provider="minimax",
            model="image-01",
            api_key="private-key",
            base_url=base_url,
            transport=httpx.MockTransport(respond),
        )
        try:
            request = ImageGenerationRequest(
                operation_id="generation", prompt="A portrait", count=9, provider_options={"response_format": "base64"}
            )
            result = await client.generate(request)
            assert await client.generate(request) is result
            assert len(requests) == 1
            assert str(requests[0].url) == endpoint
            assert requests[0].headers["authorization"] == "Bearer private-key"
            assert json.loads(requests[0].content) == {
                "model": "image-01",
                "prompt": "A portrait",
                "n": 9,
                "response_format": "base64",
                "prompt_optimizer": False,
            }
            assert result.provider_request_id == "minimax-trace"
            assert result.model_name == "image-01"
            assert result.reported_model_name is None
            assert result.usage is None
            assert len(result.images) == 1
            assert result.images[0].data == PNG
            assert result.images[0].mime == "image/png"
            assert result.images[0].revised_prompt is None
            assert client.capabilities.character_reference == "unverified"
            assert client.capabilities.reference_edit == "unsupported"
            assert client.capabilities.mask_edit == "unsupported"
        finally:
            await client.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("size", ["512x512", "1248x832", "2048x2048"])
def test_minimax_native_options(size: str) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        width, height = (int(value) for value in size.split("x"))
        assert payload == {
            "model": "image-01",
            "prompt": "a" * 1500,
            "n": 1,
            "width": width,
            "height": height,
            "response_format": "base64",
            "seed": -(2**63),
            "prompt_optimizer": True,
        }
        return httpx.Response(200, json=response_body())

    async def scenario() -> None:
        client = client_for(respond)
        try:
            result = await client.generate(
                ImageGenerationRequest(
                    operation_id="options",
                    prompt="a" * 1500,
                    size=size,
                    provider_options={"response_format": "base64", "seed": -(2**63), "prompt_optimizer": True},
                )
            )
            assert result.images[0].data == PNG
        finally:
            await client.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "changes",
    [
        {"count": 10},
        {"prompt": "a" * 1501},
        {"size": "504x512"},
        {"size": "512x504"},
        {"size": "2056x512"},
        {"size": "512x2056"},
        {"size": "513x512"},
        {"size": "512x513"},
        {"size": "512x512", "provider_options": {"aspect_ratio": "1:1"}},
        {"provider_options": {"aspect_ratio": "5:4"}},
        {"provider_options": {"seed": True}},
        {"provider_options": {"seed": 2**63}},
        {"provider_options": {"seed": -(2**63) - 1}},
        {"provider_options": {"prompt_optimizer": "false"}},
        {"provider_options": {"unknown": 1}},
        {"provider_options": {"response_format": "png"}},
    ],
)
def test_minimax_rejects_request(changes: JsonObject) -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=response_body())

    async def scenario() -> None:
        client = client_for(respond)
        try:
            request = ImageGenerationRequest.model_validate(
                {"operation_id": "invalid", "prompt": "portrait", **changes}
            )
            with pytest.raises(ImageOperationError) as caught:
                await client.generate(request)
            assert caught.value.code == "invalid_request"
            assert requests == []
        finally:
            await client.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("model", ["image-01", "image-01-live"])
def test_minimax_character_edit(model: str) -> None:
    reference = image_reference()
    reads: list[tuple[ImageReference, ImageReadScope]] = []

    async def resolve(image: ImageReference, scope: ImageReadScope) -> ResolvedImage:
        reads.append((image, scope))
        return ResolvedImage(image=image, data=PNG)

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload == {
            "model": model,
            "prompt": "In a library",
            "n": 1,
            "response_format": "base64",
            "aspect_ratio": "16:9",
            "prompt_optimizer": False,
            "seed": 2**63 - 1,
            "subject_reference": [
                {"type": "character", "image_file": f"data:image/png;base64,{base64.b64encode(PNG).decode()}"}
            ],
        }
        assert b"asset:portrait" not in request.content
        return httpx.Response(200, json=response_body())

    async def scenario() -> None:
        client = client_for(respond, model=model)
        try:
            result = await client.edit(
                ImageEditRequest(
                    operation_id="edit",
                    prompt="In a library",
                    references=(reference,),
                    base_reference=reference,
                    reference_kind="character",
                    provider_options={"aspect_ratio": "16:9", "response_format": "base64", "seed": 2**63 - 1},
                ),
                resolver=resolve,
                scope=SCOPE,
            )
            assert result.images[0].data == PNG
            assert reads == [(reference, SCOPE)]
        finally:
            await client.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "case",
    [
        "general",
        "mask",
        "multiple",
        "webp",
        "output_format",
        "quality",
        "background",
        "unknown_model",
        "live_dimensions",
    ],
)
def test_minimax_rejects_edit(case: str) -> None:
    reference = image_reference(mime="image/webp" if case == "webp" else "image/png")
    reads: list[ImageReference] = []
    requests: list[httpx.Request] = []

    async def resolve(image: ImageReference, scope: ImageReadScope) -> ResolvedImage:
        reads.append(image)
        return ResolvedImage(image=image, data=PNG)

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=response_body())

    async def scenario() -> None:
        model = "unknown" if case == "unknown_model" else "image-01-live" if case == "live_dimensions" else "image-01"
        client = client_for(respond, model=model)
        try:
            request = ImageEditRequest(
                operation_id="invalid-edit",
                prompt="portrait",
                base_reference=reference,
                references=(reference, reference) if case == "multiple" else (reference,),
                reference_kind="general" if case == "general" else "character",
                mask=reference if case == "mask" else None,
                output_format="jpeg" if case == "output_format" else None,
                quality="high" if case == "quality" else "auto",
                background="opaque" if case == "background" else "auto",
                size="512x512" if case == "live_dimensions" else "auto",
            )
            with pytest.raises(ImageOperationError) as caught:
                await client.edit(request, resolver=resolve, scope=SCOPE)
            assert caught.value.code == ("invalid_request" if case == "live_dimensions" else "unsupported_capability")
            assert reads == []
            assert requests == []
        finally:
            await client.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("size", [9_999_999, 10_000_000])
def test_minimax_reference_bytes(size: int) -> None:
    data = PNG + bytes(size - len(PNG))
    reference = image_reference(data)
    requests: list[httpx.Request] = []

    async def resolve(image: ImageReference, scope: ImageReadScope) -> ResolvedImage:
        return ResolvedImage(image=image, data=data)

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=response_body())

    async def scenario() -> None:
        client = client_for(respond)
        try:
            request = ImageEditRequest(
                operation_id="size",
                prompt="portrait",
                references=(reference,),
                base_reference=reference,
                reference_kind="character",
                provider_options={"response_format": "base64"},
            )
            if size < 10_000_000:
                result = await client.edit(request, resolver=resolve, scope=SCOPE)
                assert result.images[0].data == PNG
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
    ("status", "expected"),
    [
        (1004, "authentication"),
        (2049, "authentication"),
        (1008, "quota"),
        (2053, "quota"),
        (1002, "rate_limit"),
        (1045, "rate_limit"),
        (1026, "invalid_request"),
        (2013, "invalid_request"),
        (1001, "acceptance_unknown"),
        (1024, "acceptance_unknown"),
        (9999, "acceptance_unknown"),
    ],
)
def test_minimax_business_error(status: int, expected: ImageErrorCode) -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "id": "failed-trace",
                "base_resp": {"status_code": status, "status_msg": "secret prompt private-key"},
            },
        )

    async def scenario() -> None:
        client = client_for(respond)
        try:
            request = ImageGenerationRequest(operation_id="failure", prompt="portrait")
            for attempt in range(2):
                with pytest.raises(ImageOperationError) as caught:
                    await client.generate(request)
                assert caught.value.code == expected
                assert caught.value.provider_request_id == "failed-trace"
                assert caught.value.operation_id == "failure"
                assert "secret prompt" not in str(caught.value)
                assert "private-key" not in str(caught.value)
                assert len(requests) == 1, f"Attempt {attempt} repeated a paid submission"
        finally:
            await client.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "replacement",
    [
        {"data": {}},
        {"data": None},
        {"base_resp": None},
        {"data": {"image_base64": []}},
        {"data": {"image_base64": [""]}},
        {"data": {"image_base64": ["invalid base64"]}},
        {"data": {"image_base64": [base64.b64encode(PNG).decode()], "image_urls": ["https://images.example/output"]}},
        {"data": {"image_urls": ["https://images.example/output"]}},
        {"base_resp": {"status_code": "0", "status_msg": "success"}},
        {"metadata": {"success_count": True}},
        {"metadata": {"success_count": -1}},
        {"metadata": {"success_count": "01"}},
        {"metadata": {"success_count": 1.5}},
    ],
)
def test_minimax_invalid_response(replacement: JsonObject) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={**response_body(), **replacement})

    async def scenario() -> None:
        client = client_for(respond)
        try:
            with pytest.raises(ImageOperationError) as caught:
                await client.generate(
                    ImageGenerationRequest(
                        operation_id="malformed", prompt="portrait", provider_options={"response_format": "base64"}
                    )
                )
            assert caught.value.code == "invalid_response"
            assert caught.value.provider_request_id == "minimax-trace"
        finally:
            await client.aclose()

    asyncio.run(scenario())


def test_minimax_http_quota() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            json={"id": "quota-trace", "base_resp": {"status_code": 1008, "status_msg": "private detail"}},
        )

    async def scenario() -> None:
        client = client_for(respond)
        try:
            with pytest.raises(ImageOperationError) as caught:
                await client.generate(ImageGenerationRequest(operation_id="quota", prompt="portrait"))
            assert caught.value.code == "quota"
            assert caught.value.provider_request_id == "quota-trace"
            assert "private detail" not in str(caught.value)
            assert isinstance(caught.value.__cause__, httpx.HTTPStatusError)
        finally:
            await client.aclose()

    asyncio.run(scenario())


def test_minimax_missing_envelope() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"x-request-id": "header-trace"}, json={"data": {"image_urls": []}})

    async def scenario() -> None:
        client = client_for(respond)
        try:
            with pytest.raises(ImageOperationError) as caught:
                await client.generate(ImageGenerationRequest(operation_id="missing", prompt="portrait"))
            assert caught.value.code == "invalid_response"
            assert caught.value.provider_request_id == "header-trace"
        finally:
            await client.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("model", ["image-01-live", "unverified-model"])
def test_minimax_rejects_model(model: str) -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=response_body())

    async def scenario() -> None:
        client = client_for(respond, model=model)
        try:
            with pytest.raises(ImageOperationError) as caught:
                await client.generate(ImageGenerationRequest(operation_id="model", prompt="portrait"))
            assert caught.value.code == "unsupported_capability"
            assert requests == []
        finally:
            await client.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("count", [1, "1", None])
def test_minimax_usage(count: JsonValue) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={**response_body(), "metadata": {"success_count": count, "failed_count": "2"}})

    async def scenario() -> None:
        client = client_for(respond)
        try:
            result = await client.generate(
                ImageGenerationRequest(
                    operation_id="metadata", prompt="portrait", count=3, provider_options={"response_format": "base64"}
                )
            )
            assert len(result.images) == 1
            if count is None:
                assert result.usage is None
            else:
                assert result.usage is not None
                assert result.usage.image_count == 1
                assert result.usage.input_tokens is None
                assert result.usage.output_tokens is None
                assert result.usage.total_tokens is None
            assert result.reported_model_name is None
        finally:
            await client.aclose()

    asyncio.run(scenario())
