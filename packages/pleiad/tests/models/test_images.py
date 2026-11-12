import asyncio
import base64
import hashlib
import json
from collections.abc import Callable

import httpx
import pytest
from pydantic import ValidationError

from pleiad.contracts import JsonObject
from pleiad.messages.images import ImageLimits, ImageReadScope, ImageReference, ResolvedImage
from pleiad.models.images.client import ImageClient
from pleiad.models.images.contracts import (
    ImageCapabilities,
    ImageEditRequest,
    ImageGenerationRequest,
    ImageProvider,
    ImageRecovery,
)
from pleiad.models.images.errors import ImageOperationError

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC")
SECOND_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGNgYPgPAAEDAQAIicLsAAAAAElFTkSuQmCC"
)
SCOPE = ImageReadScope(session_id="session", run_id="run")


def reference(name: str, data: bytes = PNG) -> ImageReference:
    return ImageReference(reference=name, sha256=hashlib.sha256(data).hexdigest(), mime="image/png", width=1, height=1)


def response_body() -> dict[str, object]:
    return {"created": 1, "data": [{"b64_json": base64.b64encode(PNG).decode(), "revised_prompt": "actual revision"}]}


def client_for(handler: Callable[[httpx.Request], httpx.Response]) -> ImageClient:
    return ImageClient(
        provider="openai", model="host-model", api_key="private-key", transport=httpx.MockTransport(handler)
    )


def test_generation_partial_result() -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200, headers={"x-request-id": "request-1"}, json={**response_body(), "model": "actual-model"}
        )

    async def scenario() -> None:
        client = client_for(respond)
        try:
            request = ImageGenerationRequest(operation_id="op", prompt="A product", count=3)
            result = await client.generate(request)
            repeated = await client.generate(request)
            assert repeated is result
            assert len(requests) == 1
            assert json.loads(requests[0].content) == {
                "model": "host-model",
                "prompt": "A product",
                "n": 3,
                "size": "auto",
                "quality": "auto",
                "output_format": "png",
                "background": "auto",
            }
            assert result.operation_id == "op"
            assert result.provider_request_id == "request-1"
            assert result.model_name == "host-model"
            assert result.reported_model_name == "actual-model"
            assert result.usage is None
            assert len(result.images) == 1
            assert result.images[0].data == PNG
            assert result.images[0].mime == "image/png"
            assert result.images[0].revised_prompt == "actual revision"
            assert result.completed_at.utcoffset() is not None
            assert "PNG" not in repr(result)
            assert client.capabilities.image_generate == "unverified"
            assert client.capabilities.idempotent_submission == "unsupported"
        finally:
            await client.aclose()
        assert client.client.is_closed
        assert client.downloader.is_closed

    asyncio.run(scenario())


def test_usage_preserves_missing_counts() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={**response_body(), "usage": {"input_tokens": 12, "input_tokens_details": {"image_tokens": 8}}}
        )

    async def scenario() -> None:
        client = client_for(respond)
        try:
            result = await client.generate(ImageGenerationRequest(operation_id="op", prompt="product"))
            assert result.usage is not None
            assert result.usage.input_tokens == 12
            assert result.usage.output_tokens is None
            assert result.usage.total_tokens is None
            assert result.usage.input_tokens_details is not None
            assert result.usage.input_tokens_details.image_tokens == 8
            assert result.usage.input_tokens_details.text_tokens is None
        finally:
            await client.aclose()

    asyncio.run(scenario())


def test_edit_reference_order_and_mask() -> None:
    requests: list[httpx.Request] = []
    reads: list[tuple[ImageReference, ImageReadScope]] = []
    first, second, mask = reference("asset:first"), reference("asset:second", SECOND_PNG), reference("asset:mask")

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=response_body())

    async def resolve(image: ImageReference, scope: ImageReadScope) -> ResolvedImage:
        reads.append((image, scope))
        return ResolvedImage(image=image, data=SECOND_PNG if image == second else PNG)

    async def scenario() -> None:
        client = client_for(respond)
        try:
            result = await client.edit(
                ImageEditRequest(
                    operation_id="edit",
                    prompt="Change background",
                    references=(first, second),
                    base_reference=first,
                    mask=mask,
                ),
                resolver=resolve,
                scope=SCOPE,
            )
            assert result.images[0].data == PNG
            assert reads == [(first, SCOPE), (second, SCOPE), (mask, SCOPE)]
            assert len(requests) == 1
            assert requests[0].url.path == "/v1/images/edits"
            assert requests[0].content.index(PNG) < requests[0].content.index(SECOND_PNG)
            assert b'name="mask"' in requests[0].content
            assert b'name="image[]"' in requests[0].content
            assert b"asset:" not in requests[0].content
        finally:
            await client.aclose()

    asyncio.run(scenario())


def test_edit_rejects_unauthorized_image() -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=response_body())

    async def deny(image: ImageReference, scope: ImageReadScope) -> ResolvedImage:
        raise PermissionError("image is outside session scope")

    async def scenario() -> None:
        client = client_for(respond)
        image = reference("asset:first")
        try:
            with pytest.raises(PermissionError, match="outside session"):
                await client.edit(
                    ImageEditRequest(operation_id="op", prompt="edit", references=(image,), base_reference=image),
                    resolver=deny,
                    scope=SCOPE,
                )
            assert requests == []
        finally:
            await client.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "payload",
    [
        {"count": 0},
        {"count": 11},
        {"count": True},
        {"prompt": " "},
        {"size": "tiny"},
        {"quality": "best"},
        {"output_format": "gif"},
        {"background": "transparent", "output_format": "jpeg"},
    ],
)
def test_generation_rejects_invalid_input(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ImageGenerationRequest.model_validate({"operation_id": "op", "prompt": "product", **payload})


@pytest.mark.parametrize("fault", ["missing_base", "mask_size", "mask_format", "mask_not_first", "empty"])
def test_edit_rejects_invalid_references(fault: str) -> None:
    first, second = reference("asset:first"), reference("asset:second")
    payload: dict[str, object] = {
        "operation_id": "op",
        "prompt": "edit",
        "references": (first, second),
        "base_reference": first,
    }
    if fault == "missing_base":
        payload["base_reference"] = reference("asset:absent")
    elif fault == "mask_size":
        payload["mask"] = {**first.model_dump(), "width": 2}
    elif fault == "mask_format":
        payload["mask"] = {**first.model_dump(), "mime": "image/jpeg"}
    elif fault == "mask_not_first":
        payload["base_reference"] = second
        payload["mask"] = first
    else:
        payload["references"] = ()
    with pytest.raises(ValidationError):
        ImageEditRequest.model_validate(payload)


def test_edit_rejects_unsupported_mask() -> None:
    async def resolve(image: ImageReference, scope: ImageReadScope) -> ResolvedImage:
        pytest.fail("unsupported mask must fail before image access")

    async def scenario() -> None:
        client = ImageClient(
            provider="openai",
            model="host-model",
            api_key="key",
            capabilities=ImageCapabilities(mask_edit="unsupported"),
        )
        image = reference("asset:image")
        try:
            with pytest.raises(ImageOperationError) as caught:
                await client.edit(
                    ImageEditRequest(
                        operation_id="op", prompt="edit", references=(image,), base_reference=image, mask=image
                    ),
                    resolver=resolve,
                    scope=SCOPE,
                )
            assert caught.value.code == "unsupported_capability"
        finally:
            await client.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "body",
    [
        {"created": 1, "data": []},
        {"created": 1, "data": [{"b64_json": "!invalid!"}]},
        {"created": 1, "data": [{"b64_json": base64.b64encode(b"not an image").decode()}]},
        {**response_body(), "usage": {"input_tokens": -1}},
        {**response_body(), "output_format": "jpeg"},
    ],
)
def test_invalid_response_blocks_replay(body: dict[str, object]) -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, headers={"x-request-id": "invalid-id"}, json=body)

    async def scenario() -> None:
        client = client_for(respond)
        request = ImageGenerationRequest(operation_id="op", prompt="product")
        try:
            for submitted_request in (request, request):
                with pytest.raises(ImageOperationError) as caught:
                    await client.generate(submitted_request)
                assert caught.value.code == "invalid_response"
                assert caught.value.provider_request_id == "invalid-id"
                assert caught.value.operation_id == "op"
                assert caught.value.__cause__ is not None
            assert len(requests) == 1
        finally:
            await client.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("status", "body_code", "expected"),
    [
        (401, "bad_key", "authentication"),
        (429, "rate_limit_exceeded", "rate_limit"),
        (429, "insufficient_quota", "quota"),
        (500, "server_error", "acceptance_unknown"),
        (400, "invalid", "invalid_request"),
    ],
)
def test_provider_errors_are_sanitized(status: int, body_code: str, expected: str) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status,
            headers={"x-request-id": "error-id"},
            json={"error": {"message": "private-key private-prompt", "code": body_code}},
        )

    async def scenario() -> None:
        client = client_for(respond)
        try:
            with pytest.raises(ImageOperationError) as caught:
                await client.generate(ImageGenerationRequest(operation_id="op", prompt="private-prompt"))
            assert caught.value.code == expected
            assert caught.value.provider_request_id == "error-id"
            assert "private" not in str(caught.value)
            assert caught.value.__cause__ is not None
        finally:
            await client.aclose()

    asyncio.run(scenario())


def test_response_loss_blocks_replay() -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        raise httpx.ReadTimeout("response lost after provider admission")

    async def scenario() -> None:
        client = client_for(respond)
        request = ImageGenerationRequest(operation_id="op", prompt="product")
        try:
            for submitted_request in (request, request):
                with pytest.raises(ImageOperationError) as caught:
                    await client.generate(submitted_request)
                assert caught.value.code == "acceptance_unknown"
                assert caught.value.operation_id == "op"
            assert len(requests) == 1
        finally:
            await client.aclose()

    asyncio.run(scenario())


def test_generation_deduplicates() -> None:
    async def scenario() -> None:
        entered = asyncio.Event()
        release = asyncio.Event()
        requests: list[httpx.Request] = []

        async def respond(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            entered.set()
            await release.wait()
            return httpx.Response(200, json=response_body())

        client = ImageClient(
            provider="openai", model="host-model", api_key="key", transport=httpx.MockTransport(respond)
        )
        request = ImageGenerationRequest(operation_id="op", prompt="product")
        try:
            async with asyncio.TaskGroup() as group:
                first = group.create_task(client.generate(request))
                await entered.wait()
                second = group.create_task(client.generate(request))
                with pytest.raises(ImageOperationError) as caught:
                    await client.generate(ImageGenerationRequest(operation_id="op", prompt="changed"))
                assert caught.value.code == "operation_conflict"
                release.set()
            assert first.result() is second.result()
            assert len(requests) == 1
        finally:
            await client.aclose()

    asyncio.run(scenario())


def test_cancellation_keeps_unknown() -> None:
    async def scenario() -> None:
        entered = asyncio.Event()
        requests: list[httpx.Request] = []

        async def respond(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            entered.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        client = ImageClient(
            provider="openai", model="host-model", api_key="key", transport=httpx.MockTransport(respond)
        )
        request = ImageGenerationRequest(operation_id="op", prompt="product")
        try:
            async with asyncio.TaskGroup() as group:
                pending = group.create_task(client.generate(request))
                await entered.wait()
                pending.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await pending
            with pytest.raises(ImageOperationError) as caught:
                await client.generate(request)
            assert caught.value.code == "acceptance_unknown"
            assert len(requests) == 1
        finally:
            await client.aclose()

    asyncio.run(scenario())


def test_download_recovers_after_restart() -> None:
    posts: list[httpx.Request] = []
    downloads: list[httpx.Request] = []

    def generate(request: httpx.Request) -> httpx.Response:
        posts.append(request)
        return httpx.Response(
            200,
            headers={"x-request-id": "original-id"},
            json={
                "created": 1,
                "data": [
                    {"url": "https://images.example/original?signature=private", "revised_prompt": "original revision"}
                ],
            },
        )

    def download(request: httpx.Request) -> httpx.Response:
        downloads.append(request)
        if len(downloads) == 1:
            return httpx.Response(503)
        return httpx.Response(200, content=PNG)

    async def scenario() -> None:
        client = ImageClient(
            provider="openai",
            model="host-model",
            api_key="key",
            transport=httpx.MockTransport(generate),
            download_transport=httpx.MockTransport(download),
        )
        request = ImageGenerationRequest(operation_id="op", prompt="product")
        try:
            with pytest.raises(ImageOperationError) as caught:
                await client.generate(request)
            assert caught.value.code == "download_failed"
            assert "signature" not in str(caught.value)
            assert caught.value.recovery is not None
            stored = caught.value.recovery.model_dump_json()
            with pytest.raises(ImageOperationError, match="downloaded"):
                await client.generate(request)
        finally:
            await client.aclose()
        restarted = ImageClient(
            provider="openai",
            model="host-model",
            api_key="rotated",
            transport=httpx.MockTransport(generate),
            download_transport=httpx.MockTransport(download),
        )
        try:
            result = await restarted.recover(ImageRecovery.model_validate_json(stored))
            assert result.provider_request_id == "original-id"
            assert result.images[0].data == PNG
            assert result.images[0].revised_prompt == "original revision"
            assert len(posts) == 1
            assert len(downloads) == 2
            assert all("authorization" not in item.headers for item in downloads)
            assert await restarted.generate(request) is result
        finally:
            await restarted.aclose()

    asyncio.run(scenario())


def test_image_binding_identity() -> None:
    async def scenario() -> None:
        first = ImageClient(provider="openai", model="host-model", api_key="one")
        rotated = ImageClient(
            provider="openai", model="host-model", api_key="two", base_url="https://api.openai.com:443/v1/"
        )
        changed = ImageClient(
            provider="openai", model="host-model", api_key="one", base_url="https://another.example/v1"
        )
        try:
            assert first.binding_fingerprint == rotated.binding_fingerprint
            assert first.binding_fingerprint != changed.binding_fingerprint
        finally:
            await first.aclose()
            await rotated.aclose()
            await changed.aclose()

    asyncio.run(scenario())


def test_edit_rejects_oversized_reference() -> None:
    async def resolve(image: ImageReference, scope: ImageReadScope) -> ResolvedImage:
        return ResolvedImage(image=image, data=PNG)

    async def scenario() -> None:
        client = ImageClient(
            provider="openai", model="host-model", api_key="key", image_limits=ImageLimits(max_image_bytes=len(PNG) - 1)
        )
        image = reference("asset:image")
        try:
            with pytest.raises(ValueError, match="individual image limit"):
                await client.edit(
                    ImageEditRequest(operation_id="op", prompt="edit", references=(image,), base_reference=image),
                    resolver=resolve,
                    scope=SCOPE,
                )
        finally:
            await client.aclose()

    asyncio.run(scenario())


def test_edit_deduplication_scope() -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=response_body())

    async def resolve(image: ImageReference, scope: ImageReadScope) -> ResolvedImage:
        return ResolvedImage(image=image, data=PNG)

    async def scenario() -> None:
        client = client_for(respond)
        image = reference("asset:image")
        request = ImageEditRequest(operation_id="op", prompt="edit", references=(image,), base_reference=image)
        try:
            result = await client.edit(request, resolver=resolve, scope=SCOPE)
            assert await client.edit(request, resolver=resolve, scope=SCOPE) is result
            with pytest.raises(ImageOperationError) as caught:
                await client.edit(request, resolver=resolve, scope=ImageReadScope(session_id="another", run_id="run"))
            assert caught.value.code == "operation_conflict"
            assert len(requests) == 1
            assert b'name="mask"' not in requests[0].content
            assert PNG in requests[0].content
        finally:
            await client.aclose()

    asyncio.run(scenario())


def test_mask_shares_byte_budget() -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=response_body())

    async def resolve(image: ImageReference, scope: ImageReadScope) -> ResolvedImage:
        return ResolvedImage(image=image, data=PNG)

    async def scenario() -> None:
        image = reference("asset:image")
        request = ImageEditRequest(
            operation_id="op", prompt="edit", references=(image,), base_reference=image, mask=image
        )
        allowed = ImageClient(
            provider="openai",
            model="host-model",
            api_key="key",
            image_limits=ImageLimits(max_images=1, max_total_bytes=2 * len(PNG)),
            transport=httpx.MockTransport(respond),
        )
        limited = ImageClient(
            provider="openai",
            model="host-model",
            api_key="key",
            image_limits=ImageLimits(max_images=1, max_total_bytes=2 * len(PNG) - 1),
            transport=httpx.MockTransport(respond),
        )
        try:
            result = await allowed.edit(request, resolver=resolve, scope=SCOPE)
            assert result.images[0].data == PNG
            with pytest.raises(ValueError, match="total request limit"):
                await limited.edit(request, resolver=resolve, scope=SCOPE)
            assert len(requests) == 1
        finally:
            await allowed.aclose()
            await limited.aclose()

    asyncio.run(scenario())


def test_recovery_rechecks_response() -> None:
    async def scenario() -> None:
        entered = asyncio.Event()
        release = asyncio.Event()
        downloads: list[httpx.Request] = []
        posts: list[httpx.Request] = []

        async def respond(request: httpx.Request) -> httpx.Response:
            posts.append(request)
            entered.set()
            await release.wait()
            return httpx.Response(200, json=response_body())

        def download(request: httpx.Request) -> httpx.Response:
            downloads.append(request)
            return httpx.Response(200, content=PNG)

        client = ImageClient(
            provider="openai",
            model="host-model",
            api_key="key",
            transport=httpx.MockTransport(respond),
            download_transport=httpx.MockTransport(download),
        )
        request = ImageGenerationRequest(operation_id="op", prompt="product")
        conflicting = ImageRecovery.model_validate(
            {
                "operation_id": "op",
                "request_digest": request.payload_digest(),
                "binding_fingerprint": client.binding_fingerprint,
                "provider_request_id": "different",
                "model_name": "host-model",
                "response": {"data": [{"url": "https://images.example/unrelated"}]},
            }
        )

        async def recover() -> ImageOperationError:
            with pytest.raises(ImageOperationError) as caught:
                await client.recover(conflicting)
            return caught.value

        try:
            async with asyncio.TaskGroup() as group:
                generated = group.create_task(client.generate(request))
                await entered.wait()
                recovering = group.create_task(recover())
                release.set()
            assert generated.result().images[0].data == PNG
            assert recovering.result().code == "operation_conflict"
            assert len(posts) == 1
            assert downloads == []
        finally:
            await client.aclose()

    asyncio.run(scenario())


def test_recovery_rejects_binding_change() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        pytest.fail("recovery with another binding must not access the network")

    async def scenario() -> None:
        client = client_for(respond)
        request = ImageGenerationRequest(operation_id="op", prompt="product")
        recovery = ImageRecovery.model_validate(
            {
                "operation_id": "op",
                "request_digest": request.payload_digest(),
                "binding_fingerprint": "0" * 64,
                "provider_request_id": "original",
                "model_name": "host-model",
                "response": {"data": [{"url": "https://images.example/original"}]},
            }
        )
        try:
            with pytest.raises(ImageOperationError) as caught:
                await client.recover(recovery)
            assert caught.value.code == "invalid_configuration"
        finally:
            await client.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("mode", ["base64", "download"])
def test_oversized_output_blocks_replay(mode: str) -> None:
    posts: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        posts.append(request)
        body = (
            response_body() if mode == "base64" else {"created": 1, "data": [{"url": "https://images.example/output"}]}
        )
        return httpx.Response(200, json=body)

    def download(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=PNG)

    async def scenario() -> None:
        client = ImageClient(
            provider="openai",
            model="host-model",
            api_key="key",
            max_output_bytes=len(PNG) - 1,
            transport=httpx.MockTransport(respond),
            download_transport=httpx.MockTransport(download),
        )
        request = ImageGenerationRequest(operation_id="op", prompt="product")
        try:
            with pytest.raises(ImageOperationError) as caught:
                await client.generate(request)
            assert caught.value.code == "invalid_response"
            with pytest.raises(ImageOperationError):
                await client.generate(request)
            assert len(posts) == 1
        finally:
            await client.aclose()

    asyncio.run(scenario())


def test_closed_client_rejects_generation() -> None:
    async def scenario() -> None:
        client = ImageClient(provider="openai", model="host-model", api_key="key")
        await client.aclose()
        with pytest.raises(ImageOperationError) as caught:
            await client.generate(ImageGenerationRequest(operation_id="op", prompt="product"))
        assert caught.value.code == "invalid_configuration"

    asyncio.run(scenario())


@pytest.mark.parametrize("timeout", [0.0, -1.0, float("nan"), float("inf")])
def test_client_rejects_invalid_timeout(timeout: float) -> None:
    with pytest.raises(ImageOperationError) as caught:
        ImageClient(provider="openai", model="host-model", api_key="key", timeout_seconds=timeout)
    assert caught.value.code == "invalid_configuration"


@pytest.mark.parametrize(
    ("body", "content_type"), [(b"not-json", "application/json"), (b"unexpected text", "text/plain")]
)
def test_client_rejects_non_image_output(body: bytes, content_type: str) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": content_type, "x-request-id": "malformed"}, content=body)

    async def scenario() -> None:
        client = client_for(respond)
        try:
            with pytest.raises(ImageOperationError) as caught:
                await client.generate(ImageGenerationRequest(operation_id="op", prompt="product"))
            assert caught.value.code == "invalid_response"
            assert caught.value.provider_request_id == "malformed"
            assert caught.value.__cause__ is not None
        finally:
            await client.aclose()

    asyncio.run(scenario())


def test_cancelled_download_is_recoverable() -> None:
    async def scenario() -> None:
        entered = asyncio.Event()
        downloads: list[httpx.Request] = []
        posts: list[httpx.Request] = []

        def generate(request: httpx.Request) -> httpx.Response:
            posts.append(request)
            return httpx.Response(
                200,
                headers={"x-request-id": "accepted"},
                json={"created": 1, "data": [{"url": "https://images.example/original"}]},
            )

        async def download(request: httpx.Request) -> httpx.Response:
            downloads.append(request)
            if len(downloads) == 1:
                entered.set()
                await asyncio.Event().wait()
            return httpx.Response(200, content=PNG)

        client = ImageClient(
            provider="openai",
            model="host-model",
            api_key="key",
            transport=httpx.MockTransport(generate),
            download_transport=httpx.MockTransport(download),
        )
        request = ImageGenerationRequest(operation_id="op", prompt="product")
        try:
            async with asyncio.TaskGroup() as group:
                task = group.create_task(client.generate(request))
                await entered.wait()
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            with pytest.raises(ImageOperationError) as caught:
                await client.generate(request)
            assert caught.value.code == "download_failed"
            assert caught.value.provider_request_id == "accepted"
            assert caught.value.recovery is not None
            result = await client.recover(caught.value.recovery)
            assert result.images[0].data == PNG
            assert len(posts) == 1
            assert len(downloads) == 2
        finally:
            await client.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("provider", ["anthropic", "kimi", "deepseek"])
def test_no_generation_api(provider: ImageProvider) -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=response_body())

    with pytest.raises(ImageOperationError) as caught:
        ImageClient(provider=provider, model="vision-model", api_key="key", transport=httpx.MockTransport(respond))
    assert caught.value.code == "unsupported_capability"
    assert requests == []


@pytest.mark.parametrize(
    ("provider", "model"),
    [("qwen", "qwen-image-2.1-pro"), ("glm", "glm-image"), ("minimax", "image-01")],
)
def test_native_response_loss(provider: ImageProvider, model: str) -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        raise httpx.ReadTimeout("response lost after native provider admission")

    async def scenario() -> None:
        client = ImageClient(provider=provider, model=model, api_key="key", transport=httpx.MockTransport(respond))
        request = ImageGenerationRequest(operation_id="native-op", prompt="product")
        try:
            for attempt in range(2):
                with pytest.raises(ImageOperationError) as caught:
                    await client.generate(request)
                assert caught.value.code == "acceptance_unknown", attempt
                assert caught.value.__cause__ is not None
            assert len(requests) == 1
        finally:
            await client.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("provider", "model", "body"),
    [
        (
            "qwen",
            "qwen-image-2.1-pro",
            {
                "request_id": "native-id",
                "output": {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": [{"image": "https://assets.example/result.png"}],
                            }
                        }
                    ]
                },
                "usage": {"image_count": 1},
            },
        ),
        ("glm", "glm-image", {"created": 1, "data": [{"url": "https://assets.example/result.png"}]}),
        (
            "minimax",
            "image-01",
            {
                "id": "native-id",
                "base_resp": {"status_code": 0, "status_msg": "success"},
                "data": {"image_urls": ["https://assets.example/result.png"]},
            },
        ),
    ],
)
def test_native_download_recovery(provider: ImageProvider, model: str, body: dict[str, object]) -> None:
    submissions: list[httpx.Request] = []
    downloads: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        submissions.append(request)
        return httpx.Response(200, json=body, headers={"x-request-id": "native-id"})

    def download(request: httpx.Request) -> httpx.Response:
        downloads.append(request)
        if len(downloads) == 1:
            return httpx.Response(503)
        return httpx.Response(200, content=PNG)

    async def scenario() -> None:
        client = ImageClient(
            provider=provider,
            model=model,
            api_key="key",
            transport=httpx.MockTransport(respond),
            download_transport=httpx.MockTransport(download),
        )
        try:
            with pytest.raises(ImageOperationError) as caught:
                await client.generate(ImageGenerationRequest(operation_id="op", prompt="product"))
            assert caught.value.code == "download_failed"
            assert caught.value.recovery is not None
            recovery = ImageRecovery.model_validate_json(caught.value.recovery.model_dump_json())
        finally:
            await client.aclose()
        restarted = ImageClient(
            provider=provider,
            model=model,
            api_key="rotated-key",
            transport=httpx.MockTransport(respond),
            download_transport=httpx.MockTransport(download),
        )
        try:
            result = await restarted.recover(recovery)
            repeated = await restarted.recover(recovery)
            assert result is repeated
            assert result.images[0].data == PNG
            assert len(submissions) == 1
            assert len(downloads) == 2
            assert all("authorization" not in request.headers for request in downloads)
        finally:
            await restarted.aclose()

    asyncio.run(scenario())


def test_openai_native_options() -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=response_body())

    async def scenario() -> None:
        client = client_for(respond)
        try:
            await client.generate(
                ImageGenerationRequest(
                    operation_id="op",
                    prompt="product",
                    size="1536x864",
                    output_format="webp",
                    provider_options={"output_compression": 50, "moderation": "auto", "user": "app-user"},
                )
            )
            payload = json.loads(requests[0].content)
            assert payload["size"] == "1536x864"
            assert payload["output_compression"] == 50
            assert payload["moderation"] == "auto"
            assert payload["user"] == "app-user"
            assert "provider_options" not in payload
        finally:
            await client.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "options",
    [
        {"unknown": True},
        {"input_fidelity": "high"},
        {"output_compression": 50},
        {"output_compression": 101},
    ],
)
def test_openai_rejects_options(options: JsonObject) -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=response_body())

    async def scenario() -> None:
        client = client_for(respond)
        try:
            with pytest.raises(ImageOperationError) as caught:
                await client.generate(
                    ImageGenerationRequest(operation_id="op", prompt="product", provider_options=options)
                )
            assert caught.value.code == "invalid_request"
            assert requests == []
        finally:
            await client.aclose()

    asyncio.run(scenario())
