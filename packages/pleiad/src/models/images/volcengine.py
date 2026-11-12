from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

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

FLASH_MODEL = "doubao-seedream-5-0-flash-260915"
PRO_MODEL = "doubao-seedream-5-0-pro-260628"


class VolcenginePromptOptions(BaseModel):
    """Validate the documented native prompt optimization modes."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    mode: Literal["standard", "fast"] = "standard"


class VolcengineImageOptions(BaseModel):
    """Validate the native options implemented for synchronous Seedream images."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    watermark: bool = True
    response_format: Literal["url", "b64_json"] = "url"
    optimize_prompt_options: VolcenginePromptOptions | None = None


class VolcengineImageError(BaseModel):
    """Read native failure codes without retaining provider message text."""

    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)
    code: str = Field(min_length=1)


class VolcengineEnvelope(BaseModel):
    """Read business failures independently of success-only image fields."""

    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)
    error: VolcengineImageError | None = None


class VolcengineImageSource(ImageSource):
    """Retain the per-image format reported by the native response."""

    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)
    output_format: Literal["png", "jpeg"] | None = None
    error: VolcengineImageError | None = None


class VolcengineImageUsage(BaseModel):
    """Validate reported usage without estimating tokens from image dimensions."""

    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)
    generated_images: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)


class VolcengineSuccess(BaseModel):
    """Validate the single output produced by Seedream 5.0 Flash and Pro."""

    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)
    data: tuple[VolcengineImageSource, ...] = Field(min_length=1, max_length=1)
    model: str | None = Field(default=None, min_length=1)
    usage: VolcengineImageUsage | None = None


class VolcengineImageAdapter:
    """Translate Seedream 5.0 Flash and Pro generation and ordered reference edits."""

    base_url = "https://ark.cn-beijing.volces.com/api/v3/"
    capabilities = ImageCapabilities(
        character_reference="unverified", mask_edit="unsupported", transparent_output="unsupported"
    )

    def validate(self, model: str, request: ImageGenerationRequest) -> None:
        """Validate native semantics before the host authorizes reference reads."""
        if model not in {FLASH_MODEL, PRO_MODEL}:
            raise ValueError("this adapter requires a supported Seedream 5.0 Flash or Pro model")
        options = VolcengineImageOptions.model_validate(request.provider_options)
        if request.count != 1:
            raise ValueError("Seedream 5.0 Flash and Pro generate one image per request")
        if request.quality != "auto" or request.background != "auto" or request.output_format == "webp":
            raise ImageOperationError(
                "unsupported_capability",
                "This Seedream adapter supports PNG or JPEG without quality or background controls.",
                operation_id=request.operation_id,
            )
        if (
            model == FLASH_MODEL
            and options.optimize_prompt_options is not None
            and options.optimize_prompt_options.mode == "fast"
        ):
            raise ValueError("Seedream 5.0 Flash supports only standard prompt optimization")
        if request.size != "auto":
            width, height = (int(value) for value in request.size.split("x"))
            if not 921_600 <= width * height <= 4_624_220 or width > 16 * height or height > 16 * width:
                raise ValueError("Seedream dimensions violate the pixel or aspect-ratio limits")
        if isinstance(request, ImageEditRequest):
            if request.mask is not None:
                raise ImageOperationError(
                    "unsupported_capability",
                    "This Seedream adapter does not support masks.",
                    operation_id=request.operation_id,
                )
            if len(request.references) > 10:
                raise ValueError("Seedream 5.0 Flash and Pro accept at most ten references")
            for image in request.references:
                if (
                    min(image.width, image.height) <= 14
                    or image.width * image.height > 36_000_000
                    or image.width > 16 * image.height
                    or image.height > 16 * image.width
                ):
                    raise ValueError("Seedream reference dimensions violate the native limits")

    async def send(
        self,
        client: httpx.AsyncClient,
        model: str,
        request: ImageGenerationRequest,
        images: tuple[ResolvedImage, ...],
    ) -> ImageReply:
        """Submit generation or reference editing as one native JSON request."""
        options = VolcengineImageOptions.model_validate(request.provider_options)
        payload: JsonObject = {
            "model": model,
            "prompt": request.prompt,
            **options.model_dump(mode="json", exclude_none=True),
        }
        if request.size != "auto":
            payload["size"] = request.size
        if request.output_format is not None:
            payload["output_format"] = request.output_format
        if isinstance(request, ImageEditRequest):
            if len(images) != len(request.references) or any(len(image.data) > 30_000_000 for image in images):
                raise ImageOperationError(
                    "invalid_request",
                    "Seedream references must not exceed 30 MB each.",
                    operation_id=request.operation_id,
                )
            payload["image"] = [image_data_url(image) for image in images]
        response = await client.post("images/generations", json=payload)
        request_id = response.headers.get("x-request-id")
        try:
            envelope = VolcengineEnvelope.model_validate_json(response.content)
        except ValidationError as error:
            response.raise_for_status()
            raise ImageOperationError(
                "invalid_response",
                "Seedream returned an invalid image response.",
                operation_id=request.operation_id,
                provider_request_id=request_id,
            ) from error
        if envelope.error is not None:
            failure = volcengine_image_error(
                envelope.error.code, response.status_code, request.operation_id, request_id
            )
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as error:
                raise failure from error
            raise failure
        response.raise_for_status()
        try:
            parsed = VolcengineSuccess.model_validate_json(response.content)
            source = parsed.data[0]
            if source.error is not None:
                raise ValueError("Seedream returned an unexpected per-image error")
            if (options.response_format == "url") != (source.url is not None):
                raise ValueError("Seedream response did not match the requested representation")
            if request.output_format is not None and source.output_format not in {None, request.output_format}:
                raise ValueError("Seedream response did not match the requested image format")
            usage = (
                None
                if parsed.usage is None
                else ImageUsage(
                    image_count=parsed.usage.generated_images,
                    output_tokens=parsed.usage.output_tokens,
                    total_tokens=parsed.usage.total_tokens,
                )
            )
            normalized = ImageResponse(
                data=(ImageSource(url=source.url, b64_json=source.b64_json),),
                model=parsed.model,
                usage=usage,
                output_format=source.output_format,
            )
        except ValueError as error:
            raise ImageOperationError(
                "invalid_response",
                "Seedream returned an invalid image response.",
                operation_id=request.operation_id,
                provider_request_id=request_id,
            ) from error
        return ImageReply(response=normalized, request_id=request_id)


def volcengine_image_error(code: str, status: int, operation_id: str, request_id: str | None) -> ImageOperationError:
    """Classify native failures while keeping private provider messages out of errors."""
    kind: ImageErrorCode
    if status >= 500 or status in {408, 499} or code in {"InternalServiceError", "RequestTimeout", "RequestCanceled"}:
        kind = "acceptance_unknown"
    elif code.startswith("QuotaExceeded") or code in {
        "AccountQuotaExceeded",
        "SetLimitExceeded",
        "AccountOverdueError",
        "OperationDenied.ServiceOverdue",
    }:
        kind = "quota"
    elif status in {401, 403} or code in {"AuthenticationError", "InvalidAccountStatus", "AccessDenied"}:
        kind = "authentication"
    elif (
        status == 429
        or code.startswith("RateLimitExceeded")
        or code.endswith("RateLimitExceeded")
        or code
        in {
            "ServerOverloaded",
            "RequestBurstTooFast",
            "ModelAccountConcurrentRequestsLimitExceeded",
        }
    ):
        kind = "rate_limit"
    elif status in {400, 404, 422} or code.startswith(
        (
            "Invalid",
            "MissingParameter",
            "InputTextSensitiveContentDetected",
            "InputImageSensitiveContentDetected",
            "OutputImageSensitiveContentDetected",
            "InputTextRiskDetection",
            "InputImageRiskDetection",
            "OutputImageRiskDetection",
        )
    ):
        kind = "invalid_request"
    else:
        kind = "acceptance_unknown"
    return ImageOperationError(
        kind, "The native Seedream image request failed.", operation_id=operation_id, provider_request_id=request_id
    )
