import asyncio
import hashlib

import pytest
from pydantic import TypeAdapter, ValidationError

from pleiad.messages.images import (
    ImageBlock,
    ImageLimits,
    ImageReadScope,
    ImageReference,
    ResolvedImage,
    resolve_images,
)
from pleiad.messages.message import (
    AssistantMessage,
    Message,
    SystemMessage,
    TextBlock,
    ToolCall,
    ToolMessage,
    UserMessage,
)


def test_user_message_generates_a_stable_identity() -> None:
    first = UserMessage(content="hello")
    second = UserMessage(content="hello")

    assert first.message_id
    assert first.message_id != second.message_id


def test_message_union_selects_the_concrete_role() -> None:
    message: Message = TypeAdapter(Message).validate_python({"role": "tool", "tool_call_id": "call-1", "content": ""})

    assert isinstance(message, ToolMessage)


def test_assistant_message_accepts_tool_only_output_and_raw_invalid_arguments() -> None:
    message = AssistantMessage(tool_calls=(ToolCall(id="call-1", name="lookup", arguments="{invalid"),))

    assert message.content is None
    assert message.tool_calls[0].arguments == "{invalid"


def test_assistant_message_accepts_a_provider_refusal() -> None:
    message = AssistantMessage(refusal="I cannot help with that.")

    assert message.refusal == "I cannot help with that."


def test_assistant_message_rejects_duplicate_call_ids() -> None:
    with pytest.raises(ValidationError, match="must be unique"):
        AssistantMessage(
            tool_calls=(
                ToolCall(id="call-1", name="first", arguments="{}"),
                ToolCall(id="call-1", name="second", arguments="{}"),
            )
        )


def test_user_content_validation() -> None:
    message = UserMessage.model_validate({"content": ({"type": "text", "text": "attached"},)})

    assert not isinstance(message.content, str)
    assert isinstance(message.content[0], TextBlock)
    assert message.content[0].text == "attached"

    with pytest.raises(ValidationError, match="Field required"):
        UserMessage.model_validate({"content": ({"type": "image", "url": "secret"},)})


def test_role_specific_messages_reject_foreign_fields() -> None:
    with pytest.raises(ValidationError, match="extra"):
        UserMessage.model_validate({"content": "hello", "tool_calls": ()})
    with pytest.raises(ValidationError, match="extra"):
        ToolMessage.model_validate({"tool_call_id": "call-1", "content": "ok", "refusal": "no"})


def test_system_message_rejects_whitespace_instructions() -> None:
    with pytest.raises(ValidationError, match="non-empty"):
        SystemMessage(content="   ")


def reference(data: bytes = b"host-validated-image") -> ImageReference:
    return ImageReference(
        reference="asset:version-1", sha256=hashlib.sha256(data).hexdigest(), mime="image/png", width=2, height=3
    )


@pytest.mark.parametrize("image_only", [False, True])
def test_image_message_roundtrip(image_only: bool) -> None:
    image = ImageBlock(image=reference())
    content = (image,) if image_only else (TextBlock(text="before"), image, TextBlock(text="after"), image)
    original = UserMessage(message_id="input-1", content=content)

    restored: Message = TypeAdapter(Message).validate_json(original.model_dump_json())

    assert restored == original
    assert restored.content == content


@pytest.mark.parametrize(
    "field,value",
    [
        ("reference", " "),
        ("reference", "data:image/png;base64,AA=="),
        ("reference", "https://example.com/temporary.png"),
        ("sha256", "a" * 63),
        ("sha256", "A" * 64),
        ("mime", "application/pdf"),
        ("width", 0),
        ("height", -1),
        ("width", True),
    ],
)
def test_image_reference_validation(field: str, value: object) -> None:
    payload = reference().model_dump()
    payload[field] = value
    with pytest.raises(ValidationError):
        ImageReference.model_validate(payload)


@pytest.mark.parametrize("content", [(), ({"type": "video", "text": "invalid"},)])
def test_image_message_rejects_bad_blocks(content: object) -> None:
    with pytest.raises(ValidationError):
        UserMessage.model_validate({"content": content})


@pytest.mark.parametrize(
    "limits",
    [ImageLimits(), ImageLimits(max_images=2, max_image_bytes=20, max_total_bytes=40, max_width=2, max_height=3)],
)
def test_image_resolution_order_and_scope(limits: ImageLimits) -> None:
    data = b"host-validated-image"
    image = reference(data)
    scope = ImageReadScope(session_id="session-1", run_id="run-1")
    reads: list[tuple[ImageReference, ImageReadScope]] = []

    async def resolver(requested: ImageReference, requested_scope: ImageReadScope) -> ResolvedImage:
        reads.append((requested, requested_scope))
        return ResolvedImage(image=requested, data=data)

    resolved = asyncio.run(resolve_images((image, image), resolver, scope, limits))

    assert resolved == (ResolvedImage(image, data), ResolvedImage(image, data))
    assert reads == [(image, scope), (image, scope)]
    assert data.decode() not in repr(resolved)


@pytest.mark.parametrize(
    "limits,match",
    [
        (ImageLimits(max_images=1), "count"),
        (ImageLimits(max_width=1), "dimensions"),
        (ImageLimits(max_height=2), "dimensions"),
        (ImageLimits(max_image_bytes=19), "individual"),
        (ImageLimits(max_total_bytes=39), "total"),
    ],
)
def test_image_resolution_limits(limits: ImageLimits, match: str) -> None:
    data = b"host-validated-image"
    image = reference(data)

    async def resolver(requested: ImageReference, scope: ImageReadScope) -> ResolvedImage:
        assert scope.session_id == "session-1"
        return ResolvedImage(requested, data)

    with pytest.raises(ValueError, match=match):
        asyncio.run(resolve_images((image, image), resolver, ImageReadScope("session-1", "run-1"), limits))


def test_image_resolution_rejects_mismatch() -> None:
    image = reference()

    async def resolver(requested: ImageReference, scope: ImageReadScope) -> ResolvedImage:
        assert scope.run_id == "run-1"
        return ResolvedImage(requested.model_copy(update={"reference": "asset:other"}), b"host-validated-image")

    with pytest.raises(ValueError, match="metadata"):
        asyncio.run(resolve_images((image,), resolver, ImageReadScope("session-1", "run-1"), ImageLimits()))


@pytest.mark.parametrize("data", [b"", b"changed-image"])
def test_resolved_image_checks_digest(data: bytes) -> None:
    with pytest.raises(ValueError):
        ResolvedImage(reference(), data)
