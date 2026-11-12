import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from pleiad.contracts import ContractModel, JsonObject
from pleiad.messages.images import ImageReadScope, ImageReference

type CapabilityStatus = Literal["supported", "unsupported", "unverified"]
type ImageFormat = Literal["png", "jpeg", "webp"]
type ImageProvider = Literal["openai", "anthropic", "qwen", "glm", "kimi", "deepseek", "minimax", "volcengine"]


class ImageCapabilities(ContractModel):
    """Describe host-declared capabilities for one exact image-model binding."""

    image_input: CapabilityStatus = "unverified"
    image_generate: CapabilityStatus = "unverified"
    reference_edit: CapabilityStatus = "unverified"
    character_reference: CapabilityStatus = "unsupported"
    mask_edit: CapabilityStatus = "unverified"
    transparent_output: CapabilityStatus = "unverified"
    query_operation: Literal["unsupported"] = "unsupported"
    cancel_operation: Literal["unsupported"] = "unsupported"
    idempotent_submission: Literal["unsupported"] = "unsupported"


class ImageClientSettings(ContractModel):
    """Validate non-secret image client configuration before opening resources."""

    model: str = Field(min_length=1)
    timeout_seconds: float = Field(gt=0, allow_inf_nan=False)
    max_output_bytes: int = Field(gt=0, strict=True)

    @field_validator("model")
    @classmethod
    def validate_model(cls, value: str) -> str:
        """Reject a model identifier containing only whitespace."""
        if not value.strip():
            raise ValueError("image model must be nonempty")
        return value


class ImageGenerationRequest(ContractModel):
    """Validate provider-neutral input for one synchronous image operation.

    Provider options are validated by the selected native adapter. Unspecified
    output format and automatic size, quality and background use provider defaults.
    """

    operation_id: str = Field(min_length=1, max_length=256)
    prompt: str = Field(min_length=1, max_length=32_000)
    count: int = Field(default=1, ge=1, le=10, strict=True)
    size: str = Field(default="auto", pattern=r"^(auto|[1-9][0-9]{0,4}x[1-9][0-9]{0,4})$")
    quality: Literal["auto", "low", "medium", "high"] = "auto"
    output_format: ImageFormat | None = None
    background: Literal["auto", "opaque", "transparent"] = "auto"
    provider_options: JsonObject = Field(default_factory=dict)

    @field_validator("operation_id", "prompt")
    @classmethod
    def validate_nonempty(cls, value: str) -> str:
        """Reject whitespace-only identities and prompts."""
        if not value.strip():
            raise ValueError("image operation identity and prompt must be nonempty")
        return value

    @model_validator(mode="after")
    def validate_transparency(self) -> Self:
        """Require an output encoding that can preserve transparency."""
        if self.background == "transparent" and self.output_format == "jpeg":
            raise ValueError("transparent output requires png or webp")
        return self

    def payload_digest(self) -> str:
        """Hash operation semantics independently of the operation identity."""
        content = self.model_dump(mode="json", exclude={"operation_id"})
        content["operation"] = "edit" if isinstance(self, ImageEditRequest) else "generate"
        encoded = json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        return hashlib.sha256(encoded.encode()).hexdigest()


class ImageEditRequest(ImageGenerationRequest):
    """Validate ordered references and a mask anchored to the first source image."""

    references: tuple[ImageReference, ...] = Field(min_length=1, max_length=16)
    base_reference: ImageReference
    mask: ImageReference | None = None
    reference_kind: Literal["general", "character"] = "general"

    @model_validator(mode="after")
    def validate_references(self) -> Self:
        """Require an explicit base and protocol-compatible reference and mask formats."""
        if self.base_reference != self.references[0]:
            raise ValueError("base_reference must match the first reference")
        if any(image.mime == "image/gif" for image in self.references):
            raise ValueError("image editing supports png, jpeg and webp references")
        if self.mask is not None:
            if self.mask.mime != "image/png":
                raise ValueError("an image editing mask must be PNG")
            if (self.mask.width, self.mask.height) != (self.base_reference.width, self.base_reference.height):
                raise ValueError("the mask dimensions must match the base image")
        return self


class ImageTokenDetails(BaseModel):
    """Preserve token details actually reported by the provider."""

    model_config = ConfigDict(extra="ignore", frozen=True)
    image_tokens: int | None = Field(default=None, ge=0, strict=True)
    text_tokens: int | None = Field(default=None, ge=0, strict=True)


class ImageUsage(BaseModel):
    """Preserve missing usage fields as unknown without estimating billed usage."""

    model_config = ConfigDict(extra="ignore", frozen=True)
    input_tokens: int | None = Field(default=None, ge=0, strict=True)
    output_tokens: int | None = Field(default=None, ge=0, strict=True)
    total_tokens: int | None = Field(default=None, ge=0, strict=True)
    image_count: int | None = Field(default=None, ge=0, strict=True)
    input_tokens_details: ImageTokenDetails | None = None
    output_tokens_details: ImageTokenDetails | None = None


@dataclass(frozen=True, slots=True)
class ImageOutput:
    """Carry real image bytes for host decoding and asset persistence."""

    data: bytes = field(repr=False)
    mime: str
    revised_prompt: str | None


@dataclass(frozen=True, slots=True)
class ImageResult:
    """Return provider facts and the actual number of completed images."""

    operation_id: str
    provider_request_id: str | None
    model_name: str
    reported_model_name: str | None
    images: tuple[ImageOutput, ...]
    usage: ImageUsage | None
    completed_at: datetime


class ImageSource(BaseModel):
    """Validate one sensitive provider response source before materialization."""

    model_config = ConfigDict(extra="ignore", frozen=True)
    b64_json: str | None = Field(default=None, min_length=1, repr=False)
    url: str | None = Field(default=None, min_length=1, repr=False)
    revised_prompt: str | None = None

    @model_validator(mode="after")
    def validate_source(self) -> Self:
        """Require exactly one complete base64 or download source."""
        if (self.b64_json is None) == (self.url is None):
            raise ValueError("each returned image requires exactly one content source")
        return self


class ImageResponse(BaseModel):
    """Validate provider response contents independently of the SDK's lax parser."""

    model_config = ConfigDict(extra="ignore", frozen=True)
    data: tuple[ImageSource, ...] = Field(min_length=1, max_length=10)
    usage: ImageUsage | None = None
    model: str | None = Field(default=None, min_length=1)
    output_format: ImageFormat | None = None


class ImageRecovery(ContractModel):
    """Retain original provider sources for download-only recovery.

    Hosts must store this sensitive transmission record separately from Session,
    trace and tool-text records. It can contain expiring URLs and encoded bytes.
    """

    operation_id: str = Field(min_length=1, max_length=256)
    request_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    binding_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    provider_request_id: str | None
    model_name: str
    scope: ImageReadScope | None = None
    response: ImageResponse = Field(repr=False)
