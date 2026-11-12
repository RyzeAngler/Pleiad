from typing import Literal, Self

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from pleiad.contracts import JsonObject
from pleiad.messages.images import ResolvedImage
from pleiad.models.images.adapter import ImageReply, image_data_url
from pleiad.models.images.contracts import (
    ImageCapabilities,
    ImageEditRequest,
    ImageGenerationRequest,
    ImageResponse,
    ImageSource,
    ImageUsage,
)
from pleiad.models.images.errors import ImageErrorCode, ImageOperationError

MAX_REFERENCE_BYTES = 10_000_000


class MiniMaxImageOptions(BaseModel):
    """Validate documented native options without accepting arbitrary body fields."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    aspect_ratio: Literal["1:1", "16:9", "4:3", "3:2", "2:3", "3:4", "9:16", "21:9"] | None = None
    response_format: Literal["url", "base64"] = "url"
    seed: int | None = Field(default=None, ge=-(2**63), le=2**63 - 1)
    prompt_optimizer: bool = False


class MiniMaxBaseResponse(BaseModel):
    """Validate business status independently of the HTTP status code."""

    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)
    status_code: int
    status_msg: str = Field(repr=False)


class MiniMaxIdentity(BaseModel):
    """Read tracking identity independently of business and image validation."""

    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)
    id: str | None = Field(default=None, min_length=1)


class MiniMaxEnvelope(MiniMaxIdentity):
    """Read business status before parsing success-only fields."""

    base_resp: MiniMaxBaseResponse


class MiniMaxImageData(BaseModel):
    """Validate exactly one native response representation."""

    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)
    image_urls: tuple[str, ...] | None = Field(default=None, min_length=1, max_length=9, repr=False)
    image_base64: tuple[str, ...] | None = Field(default=None, min_length=1, max_length=9, repr=False)

    @model_validator(mode="after")
    def validate_sources(self) -> Self:
        """Reject ambiguous or absent image sources."""
        if (self.image_urls is None) == (self.image_base64 is None):
            raise ValueError("MiniMax must return exactly one image representation")
        return self


class MiniMaxImageMetadata(BaseModel):
    """Accept both documented integer counts and the official decimal-string examples."""

    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)
    success_count: int | None = Field(default=None, ge=0)
    failed_count: int | None = Field(default=None, ge=0)

    @field_validator("success_count", "failed_count", mode="before")
    @classmethod
    def validate_count(cls, value: object) -> object:
        """Convert only canonical nonnegative ASCII decimal strings to integers."""
        if (
            isinstance(value, str)
            and value.isascii()
            and value.isdecimal()
            and len(value) <= 20
            and (value == "0" or not value.startswith("0"))
        ):
            return int(value)
        return value


class MiniMaxSuccess(BaseModel):
    """Validate generated content only after the native business status succeeds."""

    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)
    data: MiniMaxImageData
    metadata: MiniMaxImageMetadata | None = None


def minimax_error_code(status: int) -> ImageErrorCode:
    """Map documented failures conservatively without assuming submission is retryable."""
    if status in {1004, 2049}:
        return "authentication"
    if status in {1008, 2053, 2056, 2067}:
        return "quota"
    if status in {1002, 1039, 1041, 1045, 2045, 2046, 2047, 2062, 2064}:
        return "rate_limit"
    if status in {1026, 1027, 1046, 2013, 2014, 2022, 2057, 2061, 2068, 2069}:
        return "invalid_request"
    return "acceptance_unknown"


class MiniMaxImageAdapter:
    """Translate MiniMax generation and explicit single-character reference requests."""

    base_url = "https://api.minimax.io/v1/"
    capabilities = ImageCapabilities(
        image_input="unverified",
        image_generate="unverified",
        reference_edit="unsupported",
        character_reference="unverified",
        mask_edit="unsupported",
        transparent_output="unsupported",
    )

    def validate(self, model: str, request: ImageGenerationRequest) -> None:
        """Reject unsupported native semantics before the host authorizes asset reads."""
        options = MiniMaxImageOptions.model_validate(request.provider_options)
        if request.output_format is not None or request.quality != "auto" or request.background != "auto":
            raise ImageOperationError(
                "unsupported_capability",
                "MiniMax does not expose image encoding, quality or background controls.",
                operation_id=request.operation_id,
            )
        if request.count > 9 or len(request.prompt) > 1500:
            raise ValueError("MiniMax supports at most nine images and 1500 prompt characters")
        editing = isinstance(request, ImageEditRequest)
        if model != "image-01" and not (editing and model == "image-01-live"):
            raise ImageOperationError(
                "unsupported_capability", "The MiniMax image model is unsupported.", operation_id=request.operation_id
            )
        if request.size != "auto":
            width, height = (int(value) for value in request.size.split("x"))
            if model != "image-01" or options.aspect_ratio is not None:
                raise ValueError("MiniMax dimensions require image-01 and no aspect_ratio override")
            if any(value < 512 or value > 2048 or value % 8 != 0 for value in (width, height)):
                raise ValueError("MiniMax dimensions must be divisible by eight and between 512 and 2048")
        if isinstance(request, ImageEditRequest) and (
            request.reference_kind != "character"
            or request.mask is not None
            or len(request.references) != 1
            or request.references[0].mime not in {"image/png", "image/jpeg"}
        ):
            raise ImageOperationError(
                "unsupported_capability",
                "MiniMax supports one JPEG or PNG character reference without a mask.",
                operation_id=request.operation_id,
            )

    async def send(
        self,
        client: httpx.AsyncClient,
        model: str,
        request: ImageGenerationRequest,
        images: tuple[ResolvedImage, ...],
    ) -> ImageReply:
        """Submit once and normalize successful native sources without downloading them."""
        options = MiniMaxImageOptions.model_validate(request.provider_options)
        payload: JsonObject = options.model_dump(mode="json", exclude_none=True)
        payload.update(model=model, prompt=request.prompt, n=request.count)
        if request.size != "auto":
            width, height = (int(value) for value in request.size.split("x"))
            payload.update(width=width, height=height)
        if isinstance(request, ImageEditRequest):
            if len(images) != 1 or len(images[0].data) >= MAX_REFERENCE_BYTES:
                raise ImageOperationError(
                    "invalid_request",
                    "MiniMax requires one reference smaller than 10 MB.",
                    operation_id=request.operation_id,
                )
            payload["subject_reference"] = [{"type": "character", "image_file": image_data_url(images[0])}]
        response = await client.post("image_generation", json=payload)
        status_error: httpx.HTTPStatusError | None = None
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as error:
            status_error = error
        request_id = response.headers.get("x-request-id")
        try:
            identity = MiniMaxIdentity.model_validate_json(response.content)
            if identity.id is not None:
                request_id = identity.id
            envelope = MiniMaxEnvelope.model_validate_json(response.content)
        except ValueError as error:
            if status_error is not None:
                raise status_error from None
            raise ImageOperationError(
                "invalid_response",
                "MiniMax returned an invalid image response.",
                operation_id=request.operation_id,
                provider_request_id=request_id,
            ) from error
        if envelope.base_resp.status_code != 0:
            raise ImageOperationError(
                minimax_error_code(envelope.base_resp.status_code),
                f"MiniMax image request failed with status {envelope.base_resp.status_code}.",
                operation_id=request.operation_id,
                provider_request_id=request_id,
            ) from status_error
        if status_error is not None:
            raise status_error
        try:
            result = MiniMaxSuccess.model_validate_json(response.content)
            if options.response_format == "url" and result.data.image_urls is not None:
                sources = tuple(ImageSource(url=url) for url in result.data.image_urls)
            elif options.response_format == "base64" and result.data.image_base64 is not None:
                sources = tuple(ImageSource(b64_json=data) for data in result.data.image_base64)
            else:
                raise ValueError("MiniMax response did not match the requested representation")
            usage = (
                None
                if result.metadata is None or result.metadata.success_count is None
                else ImageUsage(image_count=result.metadata.success_count)
            )
            normalized = ImageResponse(data=sources, usage=usage)
        except (ValidationError, ValueError) as error:
            raise ImageOperationError(
                "invalid_response",
                "MiniMax returned an invalid image response.",
                operation_id=request.operation_id,
                provider_request_id=request_id,
            ) from error
        return ImageReply(response=normalized, request_id=request_id)
