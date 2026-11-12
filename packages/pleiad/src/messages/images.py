import hashlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Literal

from pydantic import Field, field_validator

from pleiad.contracts import ContractModel


class ImageReference(ContractModel):
    """Identify immutable host-owned image content without storing transport data."""

    reference: str = Field(min_length=1, max_length=2048)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    mime: Literal["image/png", "image/jpeg", "image/webp", "image/gif"]
    width: int = Field(gt=0, strict=True)
    height: int = Field(gt=0, strict=True)

    @field_validator("reference")
    @classmethod
    def validate_reference(cls, value: str) -> str:
        """Reject empty identities and inline transport payloads."""
        if not value.strip() or value.lower().startswith(("data:", "http:", "https:")):
            raise ValueError("image reference must be a stable host identity")
        return value


class ImageBlock(ContractModel):
    """Carry a stable image reference in an ordered user message."""

    type: Literal["image"] = "image"
    image: ImageReference


@dataclass(frozen=True, slots=True)
class ImageReadScope:
    """Identify the Session and Run whose host authorization must be checked."""

    session_id: str
    run_id: str

    def __post_init__(self) -> None:
        """Require concrete scope identities."""
        if not self.session_id.strip() or not self.run_id.strip():
            raise ValueError("image reads require a Session and Run identity")


@dataclass(frozen=True, slots=True)
class ResolvedImage:
    """Hold host-validated image metadata and request-local bytes.

    The host must decode the image and verify MIME, dimensions and orientation
    before returning it. The SDK checks the bytes against the durable digest.
    """

    image: ImageReference
    data: bytes = field(repr=False)

    def __post_init__(self) -> None:
        """Reject empty or content-mismatched resolver output."""
        if not self.data:
            raise ValueError("resolved image requires nonempty bytes")
        if hashlib.sha256(self.data).hexdigest() != self.image.sha256:
            raise ValueError("resolved image digest does not match the image reference")


ImageResolver = Callable[[ImageReference, ImageReadScope], Awaitable[ResolvedImage]]


class ImageLimits(ContractModel):
    """Apply host-selected physical image limits independently of token budgets."""

    max_images: int = Field(default=16, gt=0, strict=True)
    max_image_bytes: int = Field(default=20_000_000, gt=0, strict=True)
    max_total_bytes: int = Field(default=50_000_000, gt=0, strict=True)
    max_width: int = Field(default=8192, gt=0, strict=True)
    max_height: int = Field(default=8192, gt=0, strict=True)


async def resolve_images(
    references: tuple[ImageReference, ...],
    resolver: ImageResolver,
    scope: ImageReadScope,
    limits: ImageLimits,
) -> tuple[ResolvedImage, ...]:
    """Authorize and validate each ordered image before submitting any request.

    Args:
        references: Stable identities in provider request order, including repeats.
        resolver: Host callback responsible for access control and decoding.
        scope: Current invocation identity used by host authorization.
        limits: Physical limits applied to the complete request.

    Returns:
        Validated images in the same order as the references.

    Raises:
        ValueError: If metadata, content integrity or a physical limit is invalid.
        PermissionError: If the host denies access to an image.
        FileNotFoundError: If the host cannot retrieve the original image.
    """
    if len(references) > limits.max_images:
        raise ValueError("image count exceeds the request limit")
    for reference in references:
        if reference.width > limits.max_width or reference.height > limits.max_height:
            raise ValueError("image dimensions exceed the request limit")
    images: list[ResolvedImage] = []
    total_bytes = 0
    for reference in references:
        resolved = await resolver(reference, scope)
        if resolved.image != reference:
            raise ValueError("resolved image metadata does not match the requested reference")
        if hashlib.sha256(resolved.data).hexdigest() != reference.sha256:
            raise ValueError("resolved image digest does not match the requested reference")
        if len(resolved.data) > limits.max_image_bytes:
            raise ValueError("image bytes exceed the individual image limit")
        total_bytes += len(resolved.data)
        if total_bytes > limits.max_total_bytes:
            raise ValueError("image bytes exceed the total request limit")
        images.append(resolved)
    return tuple(images)
