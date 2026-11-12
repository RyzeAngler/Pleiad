import asyncio
import base64
import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import httpx
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from anthropic import AsyncAnthropic
from openai import AsyncOpenAI
from pydantic import TypeAdapter
from zai import ZhipuAiClient

from pleiad.agent.agent import Agent
from pleiad.agent.policy import AgentPolicyHints
from pleiad.context.budget import ApproximateTokenCounter
from pleiad.contracts import JsonObject
from pleiad.messages.images import ImageBlock, ImageLimits, ImageReadScope, ImageReference, ResolvedImage
from pleiad.messages.message import TextBlock, UserMessage
from pleiad.models.anthropic.client import AnthropicModelClient
from pleiad.models.anthropic.request import AnthropicRequest
from pleiad.models.binding import binding_fingerprint
from pleiad.models.openai.client import OpenAIModelClient
from pleiad.models.openai.request import OpenAIRequest
from pleiad.models.providers import anthropic as anthropic_provider
from pleiad.models.providers import deepseek as deepseek_provider
from pleiad.models.providers import glm as glm_provider
from pleiad.models.providers import kimi as kimi_provider
from pleiad.models.providers import minimax as minimax_provider
from pleiad.models.providers import openai as openai_provider
from pleiad.runtime.checkpoint import ResumeDecision
from pleiad.runtime.errors import RuntimeStateError
from pleiad.runtime.result import RunResult, RunSuspension
from pleiad.runtime.runner import Runner
from pleiad.sessions.backends.in_memory import InMemorySessionStore
from pleiad.sessions.backends.sqlite import SqliteSessionStore
from pleiad.sessions.records import RunStatus
from pleiad.sessions.session import Session
from pleiad.skills.loader import SkillLoader
from pleiad.tools.contracts import ToolResult
from pleiad.tools.registry import ToolRegistry
from pleiad.tools.runtime import ToolRuntime
from tests.fakes.runtime import (
    FakeAnthropicModel,
    FakeOpenAIModel,
    anthropic_text_batch,
    anthropic_tool_batch,
    interrupting_tool,
    make_runner,
    successful_tool,
    text_batch,
    tool_batch,
)
from tests.runtime_support import resume_agent, run_agent, start_agent

IMAGE_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC"
)
SECOND_IMAGE_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGNgYPgPAAEDAQAIicLsAAAAAElFTkSuQmCC"
)


def image_reference(data: bytes = IMAGE_BYTES, identity: str = "asset:version-1") -> ImageReference:
    return ImageReference(
        reference=identity, sha256=hashlib.sha256(data).hexdigest(), mime="image/png", width=1, height=1
    )


@pytest.mark.parametrize("protocol", ["openai", "anthropic"])
def test_image_tool_roundtrip(tmp_path: Path, protocol: Literal["openai", "anthropic"]) -> None:
    model: FakeOpenAIModel | FakeAnthropicModel
    if protocol == "openai":
        model = FakeOpenAIModel((tool_batch("call-1"), text_batch("finished", "response-2")))
    else:
        model = FakeAnthropicModel((anthropic_tool_batch("call-1"), anthropic_text_batch("finished", "response-2")))
    first = image_reference()
    second = image_reference(SECOND_IMAGE_BYTES, "asset:version-2")
    content = (TextBlock(text="first"), ImageBlock(image=first), TextBlock(text="last"), ImageBlock(image=second))
    reads: list[tuple[ImageReference, ImageReadScope]] = []

    async def resolver(reference: ImageReference, scope: ImageReadScope) -> ResolvedImage:
        reads.append((reference, scope))
        return ResolvedImage(reference, IMAGE_BYTES if reference == first else SECOND_IMAGE_BYTES)

    runner, agent = make_runner(
        model,
        tmp_path,
        successful_tool,
        image_resolver=resolver,
        token_counter=ApproximateTokenCounter(image_token_estimator=lambda image: image.width * image.height),
    )
    session = Session("image-tool-session", InMemorySessionStore())
    result = asyncio.run(run_agent(runner, agent, UserMessage(content=content), session=session))

    assert isinstance(result, RunResult)
    assert result.state is RunStatus.SUCCEEDED
    assert result.final_output == "finished"
    assert [read[0] for read in reads] == [first, second, first, second]
    assert all(scope == ImageReadScope(session.session_id, result.run_id) for image, scope in reads)
    expected_images = []
    for data in (IMAGE_BYTES, SECOND_IMAGE_BYTES):
        encoded = base64.b64encode(data).decode("ascii")
        expected_images.append(
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{encoded}"}}
            if protocol == "openai"
            else {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": encoded}}
        )
    for request in model.requests:
        user = next(message for message in request.messages if message.role == "user")
        assert isinstance(user.content, list)
        assert user.content[-4:] == [
            {"type": "text", "text": "first"},
            expected_images[0],
            {"type": "text", "text": "last"},
            expected_images[1],
        ]
    if isinstance(model, FakeOpenAIModel):
        assert model.requests[1].messages[-1].tool_call_id == "call-1"
        assert model.requests[1].messages[-2].tool_calls is not None
        assert model.requests[1].messages[-2].tool_calls[0].id == "call-1"
    else:
        assert model.requests[1].messages[-1].content == [
            {
                "type": "tool_result",
                "tool_use_id": "call-1",
                "content": '{"error_code": null, "error_message": null, "error_type": null, '
                '"ok": true, "output": {"echoed": "x"}}',
            }
        ]
    history = asyncio.run(session.history())
    assert isinstance(history[0].messages[0], UserMessage)
    assert not isinstance(history[0].messages[0].content, str)
    assert history[0].messages[0].content[1:] == content


def test_image_followup_after_reopen(tmp_path: Path) -> None:
    image = image_reference()
    reads: list[ImageReadScope] = []

    async def resolver(reference: ImageReference, scope: ImageReadScope) -> ResolvedImage:
        reads.append(scope)
        return ResolvedImage(reference, IMAGE_BYTES)

    database = tmp_path / "sessions.sqlite3"
    model = FakeOpenAIModel((text_batch("red", "first-response"), text_batch("still red", "second-response")))
    runner, agent = make_runner(
        model,
        tmp_path,
        image_resolver=resolver,
        token_counter=ApproximateTokenCounter(image_token_estimator=lambda image: image.width * image.height),
    )

    async def scenario() -> None:
        first_session = Session("persisted-images", SqliteSessionStore(database))
        first = await run_agent(runner, agent, UserMessage(content=(ImageBlock(image=image),)), session=first_session)
        assert first.state is RunStatus.SUCCEEDED
        reopened = Session("persisted-images", SqliteSessionStore(database))
        second = await run_agent(runner, agent, "What color was it?", session=reopened)
        assert second.state is RunStatus.SUCCEEDED
        assert reads == [
            ImageReadScope(reopened.session_id, first.run_id),
            ImageReadScope(reopened.session_id, second.run_id),
        ]
        persisted = (await reopened.store.snapshot(reopened.session_id)).model_dump_json()
        assert image.sha256 in persisted and image.reference in persisted
        assert base64.b64encode(IMAGE_BYTES).decode("ascii") not in persisted
        assert "data:image" not in persisted
        assert base64.b64encode(IMAGE_BYTES).decode("ascii") in model.requests[1].model_dump_json()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "failure,error_type", [("missing", FileNotFoundError), ("denied", PermissionError), ("changed", ValueError)]
)
def test_invalid_image_blocks_request(tmp_path: Path, failure: str, error_type: type[Exception]) -> None:
    async def resolver(reference: ImageReference, scope: ImageReadScope) -> ResolvedImage:
        assert scope.session_id == "invalid-image"
        if failure == "missing":
            raise FileNotFoundError("original asset is missing")
        if failure == "denied":
            raise PermissionError("asset access denied")
        return ResolvedImage(reference, SECOND_IMAGE_BYTES)

    model = FakeOpenAIModel(())
    runner, agent = make_runner(
        model,
        tmp_path,
        image_resolver=resolver,
        token_counter=ApproximateTokenCounter(image_token_estimator=lambda image: image.width * image.height),
    )
    session = Session("invalid-image", InMemorySessionStore())
    with pytest.raises(error_type):
        asyncio.run(
            run_agent(runner, agent, UserMessage(content=(ImageBlock(image=image_reference()),)), session=session)
        )
    snapshot = asyncio.run(session.store.snapshot(session.session_id))
    assert snapshot.runs[0].state is RunStatus.FAILED
    assert snapshot.runs[0].usage.model_calls == 0
    assert snapshot.runs[0].model_attempts == ()
    assert model.requests == []


@pytest.mark.parametrize("unsupported", [False, True])
def test_image_requires_model_and_resolver(tmp_path: Path, unsupported: bool) -> None:
    reads: list[ImageReadScope] = []

    async def resolver(reference: ImageReference, scope: ImageReadScope) -> ResolvedImage:
        reads.append(scope)
        return ResolvedImage(reference, IMAGE_BYTES)

    model = FakeOpenAIModel(())
    model.image_input = "unsupported" if unsupported else "unverified"
    runner, agent = make_runner(
        model,
        tmp_path,
        image_resolver=resolver if unsupported else None,
        token_counter=ApproximateTokenCounter(image_token_estimator=lambda image: image.width * image.height),
    )
    session = Session("invalid-vision-binding", InMemorySessionStore())
    with pytest.raises(ValueError, match="does not support" if unsupported else "resolver"):
        asyncio.run(
            run_agent(runner, agent, UserMessage(content=(ImageBlock(image=image_reference()),)), session=session)
        )
    snapshot = asyncio.run(session.store.snapshot(session.session_id))
    assert snapshot.runs[0].state is RunStatus.FAILED
    assert snapshot.runs[0].usage.model_calls == 0
    assert snapshot.runs[0].model_attempts == ()
    assert model.requests == []
    assert reads == []


def test_image_byte_limit_blocks_request(tmp_path: Path) -> None:
    async def resolver(reference: ImageReference, scope: ImageReadScope) -> ResolvedImage:
        assert scope.session_id
        return ResolvedImage(reference, IMAGE_BYTES)

    model = FakeOpenAIModel(())
    runner, agent = make_runner(
        model,
        tmp_path,
        image_resolver=resolver,
        image_limits=ImageLimits(max_image_bytes=len(IMAGE_BYTES) - 1),
        token_counter=ApproximateTokenCounter(image_token_estimator=lambda image: image.width * image.height),
    )
    session = Session("oversized-image", InMemorySessionStore())
    with pytest.raises(ValueError, match="individual image limit"):
        asyncio.run(
            run_agent(runner, agent, UserMessage(content=(ImageBlock(image=image_reference()),)), session=session)
        )
    snapshot = asyncio.run(session.store.snapshot(session.session_id))
    assert snapshot.runs[0].state is RunStatus.FAILED
    assert snapshot.runs[0].usage.model_calls == 0
    assert snapshot.runs[0].model_attempts == ()
    assert model.requests == []


def test_image_resolution_cancellation(tmp_path: Path) -> None:
    entered = asyncio.Event()
    closed = asyncio.Event()
    release = asyncio.Event()

    async def resolver(reference: ImageReference, scope: ImageReadScope) -> ResolvedImage:
        assert scope.session_id == "cancel-image"
        try:
            entered.set()
            await release.wait()
            return ResolvedImage(reference, IMAGE_BYTES)
        finally:
            closed.set()

    model = FakeOpenAIModel(())
    runner, agent = make_runner(
        model,
        tmp_path,
        image_resolver=resolver,
        token_counter=ApproximateTokenCounter(image_token_estimator=lambda image: image.width * image.height),
    )

    async def scenario() -> None:
        session = Session("cancel-image", InMemorySessionStore())
        handle = await start_agent(
            runner, agent, UserMessage(content=(ImageBlock(image=image_reference()),)), session=session
        )
        async with handle:
            await entered.wait()
            result = await handle.cancel(reason="cancelled image read")
        assert closed.is_set()
        assert result.state is RunStatus.CANCELLED
        assert result.usage.model_calls == 0
        snapshot = await session.store.run(session.session_id, result.run_id)
        assert snapshot.model_attempts == ()
        assert model.requests == []

    asyncio.run(scenario())


@pytest.mark.parametrize("revoke_access", [False, True])
def test_image_resume_reauthorizes(tmp_path: Path, revoke_access: bool) -> None:
    authorized = True
    scopes: list[ImageReadScope] = []

    async def resolver(reference: ImageReference, scope: ImageReadScope) -> ResolvedImage:
        scopes.append(scope)
        if not authorized:
            raise PermissionError("access revoked")
        return ResolvedImage(reference, IMAGE_BYTES)

    model = FakeOpenAIModel((tool_batch("call-1"), text_batch("resumed", "response-2")))
    runner, agent = make_runner(
        model,
        tmp_path,
        interrupting_tool,
        image_resolver=resolver,
        token_counter=ApproximateTokenCounter(image_token_estimator=lambda image: image.width * image.height),
    )
    database = tmp_path / "resume.sqlite3"

    async def scenario() -> None:
        nonlocal authorized
        session = Session("image-resume", SqliteSessionStore(database))
        paused = await run_agent(
            runner, agent, UserMessage(content=(ImageBlock(image=image_reference()),)), session=session
        )
        assert isinstance(paused, RunSuspension)
        serialized_checkpoint = paused.checkpoint.model_dump_json()
        assert base64.b64encode(IMAGE_BYTES).decode("ascii") not in serialized_checkpoint
        authorized = not revoke_access
        now = datetime.now(UTC)
        resume = resume_agent(
            runner,
            agent,
            type(paused.checkpoint).model_validate_json(serialized_checkpoint),
            session=Session("image-resume", SqliteSessionStore(database)),
            decisions=(
                ResumeDecision(
                    interruption_id="interrupt-1",
                    result=ToolResult(
                        call_id="call-1", ok=True, output={"approved": True}, started_at=now, finished_at=now
                    ),
                ),
            ),
        )
        if revoke_access:
            with pytest.raises(PermissionError, match="revoked"):
                await resume
            snapshot = await session.store.run(session.session_id, paused.run_id)
            assert snapshot.state is RunStatus.FAILED
            assert len(snapshot.model_attempts) == 1
        else:
            resumed = await resume
            assert resumed.run_id == paused.run_id
            assert resumed.state is RunStatus.SUCCEEDED
        assert scopes == [ImageReadScope("image-resume", paused.run_id)] * 2
        assert len(model.requests) == (1 if revoke_access else 2)
        if not revoke_access:
            assert model.requests[1].messages[-1].tool_call_id == "call-1"
            assert base64.b64encode(IMAGE_BYTES).decode("ascii") in model.requests[1].model_dump_json()

    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["url", "options", "model"])
def test_resume_rejects_binding_change(tmp_path: Path, change: str) -> None:
    model = FakeOpenAIModel((tool_batch("call-1"),))
    runner, agent = make_runner(model, tmp_path, interrupting_tool)
    session = Session("binding-resume", InMemorySessionStore())

    async def scenario() -> None:
        paused = await run_agent(runner, agent, "pause", session=session)
        assert isinstance(paused, RunSuspension)
        model.binding_fingerprint = binding_fingerprint(
            protocol="openai",
            provider=model.provider,
            model="different-model" if change == "model" else model.model,
            base_url="https://other.example/v1" if change == "url" else None,
            provider_options={"reasoning_effort": "high"} if change == "options" else {},
        )
        with pytest.raises(RuntimeStateError, match="binding"):
            await resume_agent(runner, agent, paused.checkpoint, session=session)
        assert len(model.requests) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("protocol", ["openai", "anthropic"])
def test_binding_normalization(
    protocol: Literal["openai", "anthropic"],
) -> None:
    constructor = OpenAIModelClient if protocol == "openai" else AnthropicModelClient

    async def scenario() -> None:
        original = constructor(
            provider=protocol,
            model="vision-model",
            api_key="first-test-credential",
            base_url="https://EXAMPLE.com:443/v1/",
            provider_options={"reasoning_effort": "high"},
            headers={"Authorization": "first-authorization", "X-Feature": "enabled"},
        )
        rotated = constructor(
            provider=protocol,
            model="vision-model",
            api_key="second-test-credential",
            base_url="https://example.com/v1",
            provider_options={"reasoning_effort": "high"},
            headers={"Authorization": "second-authorization", "x-feature": "enabled"},
        )
        try:
            assert original.binding_fingerprint == rotated.binding_fingerprint
            assert len(original.binding_fingerprint) == 64
        finally:
            await original.aclose()
            await rotated.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "first,rotated",
    [
        ("https://user:first@example.com/v1", "https://user:second@example.com/v1"),
        ("https://example.com/v1?api_key=first&route=vision", "https://example.com/v1?api_key=second&route=vision"),
        ("https://example.com/v1?signature=first&route=vision", "https://example.com/v1?signature=second&route=vision"),
    ],
)
def test_binding_credential_rotation(first: str, rotated: str) -> None:
    original = binding_fingerprint(
        protocol="openai", provider="openai", model="vision", base_url=first, provider_options={}
    )
    replacement = binding_fingerprint(
        protocol="openai", provider="openai", model="vision", base_url=rotated, provider_options={}
    )

    assert original == replacement


def test_binding_nested_credentials() -> None:
    original = binding_fingerprint(
        protocol="openai",
        provider="openai",
        model="vision",
        base_url=None,
        provider_options={
            "vendor": [{"accessToken": "first-credential", "alias": "first-credential", "max_tokens": 64}]
        },
    )
    rotated = binding_fingerprint(
        protocol="openai",
        provider="openai",
        model="vision",
        base_url=None,
        provider_options={
            "vendor": [{"accessToken": "second-credential", "alias": "second-credential", "max_tokens": 64}]
        },
    )
    changed = binding_fingerprint(
        protocol="openai",
        provider="openai",
        model="vision",
        base_url=None,
        provider_options={
            "vendor": [{"accessToken": "second-credential", "alias": "second-credential", "max_tokens": 128}]
        },
    )

    assert original == rotated
    assert original != changed


def test_binding_masks_key_aliases() -> None:
    original = binding_fingerprint(
        protocol="openai",
        provider="openai",
        model="vision",
        base_url=None,
        provider_options={"vendor": {"alias": "first-credential", "mode": "vision"}},
        headers={"X-Custom": "Bearer first-credential"},
        secrets=("first-credential",),
    )
    rotated = binding_fingerprint(
        protocol="openai",
        provider="openai",
        model="vision",
        base_url=None,
        provider_options={"vendor": {"alias": "second-credential", "mode": "vision"}},
        headers={"X-Custom": "Bearer second-credential"},
        secrets=("second-credential",),
    )
    changed = binding_fingerprint(
        protocol="openai",
        provider="openai",
        model="vision",
        base_url=None,
        provider_options={"vendor": {"alias": "second-credential", "mode": "text"}},
        headers={"X-Custom": "Bearer second-credential"},
        secrets=("second-credential",),
    )

    assert original == rotated
    assert original != changed


@pytest.mark.parametrize(
    "first,second",
    [
        ("https://example.com/v1?route=first&route=second", "https://example.com/v1?route=second&route=first"),
        ("https://example.com/v1//", "https://example.com/v1/"),
    ],
)
def test_binding_tracks_routing(first: str, second: str) -> None:
    first_fingerprint = binding_fingerprint(
        protocol="openai", provider="openai", model="vision", base_url=first, provider_options={}
    )
    second_fingerprint = binding_fingerprint(
        protocol="openai", provider="openai", model="vision", base_url=second, provider_options={}
    )

    assert first_fingerprint != second_fingerprint


@pytest.mark.parametrize(
    "protocol,provider,provider_module,path",
    [
        ("openai", "openai", openai_provider, "/v1/chat/completions"),
        ("openai", "deepseek", deepseek_provider, "/chat/completions"),
        ("openai", "kimi", kimi_provider, "/v1/chat/completions"),
        ("openai", "minimax", minimax_provider, "/v1/chat/completions"),
        ("openai", "glm", glm_provider, "/api/paas/v4/chat/completions"),
        ("openai", "volcengine", openai_provider, "/api/v3/chat/completions"),
        ("anthropic", "anthropic", anthropic_provider, "/v1/messages"),
    ],
    ids=["openai", "deepseek", "kimi", "minimax", "glm", "volcengine", "anthropic"],
)
def test_image_http_payload(
    monkeypatch: pytest.MonkeyPatch,
    protocol: Literal["openai", "anthropic"],
    provider: str,
    provider_module: object,
    path: str,
) -> None:
    encoded = base64.b64encode(IMAGE_BYTES).decode("ascii")
    model = "doubao-seed-2-0-lite-260428" if provider == "volcengine" else "vision"
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.path == path
        if protocol == "openai":
            payload = OpenAIRequest.model_validate_json(request.content)
            if provider == "glm":
                assert payload.max_tokens is not None
                assert payload.max_completion_tokens is None
                assert payload.stream_options is None
            if provider == "volcengine":
                assert str(request.url) == "https://ark.cn-beijing.volces.com/api/v3/chat/completions"
                assert payload.model == model
                assert payload.max_completion_tokens == 97
                assert payload.max_tokens is None
                assert payload.model_extra == {"thinking": {"type": "disabled"}}
            user = next(message for message in payload.messages if message.role == "user")
            assert isinstance(user.content, list)
            assert user.content[-3:] == [
                {"type": "text", "text": "before"},
                {
                    "type": "image_url",
                    "image_url": {"url": encoded if provider == "glm" else f"data:image/png;base64,{encoded}"},
                },
                {"type": "text", "text": "after"},
            ]
            chunk = {
                "id": "vision-answer",
                "object": "chat.completion.chunk",
                "created": 123,
                "model": model,
                "choices": [{"index": 0, "delta": {"content": "red"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 1, "total_tokens": 11},
            }
            body = f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n"
        else:
            anthropic_payload = AnthropicRequest.model_validate_json(request.content)
            anthropic_user = next(message for message in anthropic_payload.messages if message.role == "user")
            assert isinstance(anthropic_user.content, list)
            assert anthropic_user.content[-3:] == [
                {"type": "text", "text": "before"},
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": encoded}},
                {"type": "text", "text": "after"},
            ]
            events = (
                {
                    "type": "message_start",
                    "message": {
                        "id": "vision-answer",
                        "type": "message",
                        "role": "assistant",
                        "content": [],
                        "model": "vision",
                        "stop_reason": None,
                        "stop_sequence": None,
                        "usage": {"input_tokens": 10, "output_tokens": 0},
                    },
                },
                {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
                {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "red"}},
                {"type": "content_block_stop", "index": 0},
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                    "usage": {"output_tokens": 1},
                },
                {"type": "message_stop"},
            )
            body = "".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    def create_client(
        *, api_key: str, base_url: str | None, timeout: float, max_retries: int, default_headers: Mapping[str, str]
    ) -> AsyncOpenAI | AsyncAnthropic:
        constructor = AsyncOpenAI if protocol == "openai" else AsyncAnthropic
        return constructor(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
            max_retries=max_retries,
            default_headers=default_headers,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
        )

    def create_zhipu(
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

    if provider == "glm":
        monkeypatch.setattr(provider_module, "ZhipuAiClient", create_zhipu)
    else:
        monkeypatch.setattr(provider_module, "AsyncOpenAI" if protocol == "openai" else "AsyncAnthropic", create_client)

    async def resolver(reference: ImageReference, scope: ImageReadScope) -> ResolvedImage:
        assert scope.session_id and scope.run_id
        return ResolvedImage(reference, IMAGE_BYTES)

    async def scenario() -> None:
        constructor = OpenAIModelClient if protocol == "openai" else AnthropicModelClient
        options: JsonObject = {"extra_body": {"thinking": {"type": "disabled"}}} if provider == "volcengine" else {}
        client = constructor(
            provider=provider, model=model, api_key="test-credential", max_retries=0, provider_options=options
        )
        runner = Runner(
            tools=ToolRegistry(),
            skills=SkillLoader(),
            tool_runtime=ToolRuntime(),
            image_resolver=resolver,
            token_counter=ApproximateTokenCounter(image_token_estimator=lambda image: image.width * image.height),
        )
        agent = Agent(
            name="vision",
            model=client,
            prompt="Describe the image.",
            tools=(),
            policy_hints=AgentPolicyHints(max_output_tokens=97),
        )
        try:
            result = await run_agent(
                runner,
                agent,
                UserMessage(
                    content=(TextBlock(text="before"), ImageBlock(image=image_reference()), TextBlock(text="after"))
                ),
            )
            assert isinstance(result, RunResult)
            assert result.state is RunStatus.SUCCEEDED
            assert result.final_output == "red"
            assert result.usage.model_calls == 1
            assert len(requests) == 1
        finally:
            await client.aclose()

    asyncio.run(scenario())


def test_qwen_image_run() -> None:
    encoded = base64.b64encode(IMAGE_BYTES).decode("ascii")
    received: list[JsonObject] = []

    async def respond(request: web.Request) -> web.Response:
        payload: JsonObject = TypeAdapter(JsonObject).validate_json(await request.text())
        received.append(payload)
        native_input = payload["input"]
        assert isinstance(native_input, dict)
        messages = native_input["messages"]
        assert isinstance(messages, list)
        user = next(message for message in messages if isinstance(message, dict) and message["role"] == "user")
        assert isinstance(user["content"], list)
        assert user["content"][-3:] == [
            {"text": "before"},
            {"image": f"data:image/png;base64,{encoded}"},
            {"text": "after"},
        ]
        body = {
            "request_id": "qwen-answer",
            "output": {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": [{"text": "red"}]},
                    }
                ]
            },
            "usage": {"input_tokens": 10, "output_tokens": 1},
        }
        return web.Response(text=f"data: {json.dumps(body)}\n\n", content_type="text/event-stream")

    async def resolver(reference: ImageReference, scope: ImageReadScope) -> ResolvedImage:
        assert scope.session_id and scope.run_id
        return ResolvedImage(reference, IMAGE_BYTES)

    async def scenario() -> None:
        app = web.Application()
        app.router.add_post("/api/v1/services/aigc/multimodal-generation/generation", respond)
        async with TestServer(app) as server:
            client = OpenAIModelClient(
                provider="qwen",
                model="host-vision",
                api_key="test-key",
                max_retries=0,
                base_url=str(server.make_url("/api/v1")),
            )
            runner = Runner(
                tools=ToolRegistry(),
                skills=SkillLoader(),
                tool_runtime=ToolRuntime(),
                image_resolver=resolver,
                token_counter=ApproximateTokenCounter(image_token_estimator=lambda image: image.width * image.height),
            )
            agent = Agent(name="vision", model=client, prompt="Describe the image.", tools=())
            try:
                result = await run_agent(
                    runner,
                    agent,
                    UserMessage(
                        content=(
                            TextBlock(text="before"),
                            ImageBlock(image=image_reference()),
                            TextBlock(text="after"),
                        )
                    ),
                )
                assert isinstance(result, RunResult)
                assert result.state is RunStatus.SUCCEEDED
                assert result.final_output == "red"
                assert result.usage.model_calls == 1
                assert len(received) == 1
            finally:
                await client.aclose()

    asyncio.run(scenario())
