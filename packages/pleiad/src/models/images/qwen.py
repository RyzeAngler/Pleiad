from typing import Literal, Self

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from pleiad.contracts import ContractModel, JsonValue
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


class QwenImageOptions(ContractModel):
    """Validate native Qwen options without allowing shared fields to be overridden."""

    negative_prompt: str | None = Field(default=None, max_length=500)
    seed: int | None = Field(default=None, ge=0, le=2_147_483_647, strict=True)
    watermark: bool | None = Field(default=None, strict=True)
    prompt_extend: bool | None = Field(default=None, strict=True)
    prompt_extend_mode: Literal["direct", "agent"] | None = None
    enable_thinking: bool | None = Field(default=None, strict=True)


class QwenImageContent(BaseModel):
    """Read a native image or accompanying text content block."""

    model_config = ConfigDict(extra="ignore", frozen=True)
    image: str | None = Field(default=None, min_length=1)
    text: str | None = None

    @model_validator(mode="after")
    def validate_content(self) -> Self:
        """Reject incomplete native content blocks instead of silently dropping them."""
        if self.image is None and self.text is None:
            raise ValueError("Qwen content requires an image or text")
        return self


class QwenImageMessage(BaseModel):
    """Read ordered native assistant content."""

    model_config = ConfigDict(extra="ignore", frozen=True)
    role: Literal["assistant"]
    content: tuple[QwenImageContent, ...] = Field(min_length=1)


class QwenImageChoice(BaseModel):
    """Read one completed native assistant message."""

    model_config = ConfigDict(extra="ignore", frozen=True)
    message: QwenImageMessage


class QwenImageOutput(BaseModel):
    """Require actual choices in a successful image response."""

    model_config = ConfigDict(extra="ignore", frozen=True)
    choices: tuple[QwenImageChoice, ...] = Field(min_length=1)


class QwenImageUsage(BaseModel):
    """Retain image billing counters reported by different Qwen families."""

    model_config = ConfigDict(extra="ignore", frozen=True)
    image_count: int | None = Field(default=None, ge=0, strict=True)
    output_image_count: int | None = Field(default=None, ge=0, strict=True)


class QwenImageEnvelope(BaseModel):
    """Read native request identity and business status independently of output."""

    model_config = ConfigDict(extra="ignore", frozen=True)
    code: str | None = None
    request_id: str | None = Field(default=None, min_length=1)


class QwenImageResponse(QwenImageEnvelope):
    """Validate a successful native response without discarding its identity."""

    output: QwenImageOutput | None = None
    usage: QwenImageUsage | None = None


class QwenImageAdapter:
    """Translate Qwen's synchronous native image generation and editing protocol.

    Current 2.0, 2.1 and 3.0 families and legacy edit models share this route.
    Legacy asynchronous generation models require a separate task lifecycle.
    Custom model aliases use the shared native contract and remote validation.
    """

    base_url = "https://dashscope.aliyuncs.com/api/v1"
    capabilities = ImageCapabilities(
        image_input="unverified",
        image_generate="unverified",
        reference_edit="unverified",
        mask_edit="unsupported",
        transparent_output="unsupported",
    )

    def validate(self, model: str, request: ImageGenerationRequest) -> None:
        """Reject unsupported options before resolving images or submitting HTTP.

        Args:
            model: Exact model binding, including provider-supported aliases.
            request: Provider-neutral operation and native option values.

        Raises:
            ValueError: If the request does not fit the native synchronous contract.
        """
        options = QwenImageOptions.model_validate(request.provider_options)
        if model == "qwen-image" or model.startswith(("qwen-image-plus", "qwen-image-max")):
            raise ValueError("legacy Qwen generation requires the asynchronous task API")
        legacy_edit = model.startswith("qwen-image-edit")
        if legacy_edit and not isinstance(request, ImageEditRequest):
            raise ValueError("legacy Qwen edit models require reference images")
        if request.count > (1 if model == "qwen-image-edit" else 6):
            raise ValueError("the Qwen model does not support this output image count")
        if request.output_format not in {None, "png"} or request.quality != "auto" or request.background != "auto":
            raise ValueError("Qwen supports PNG output with automatic quality and background")
        if request.size != "auto":
            if model == "qwen-image-edit":
                raise ValueError("legacy qwen-image-edit does not accept a size")
            width, height = (int(value) for value in request.size.split("x"))
            if not 512**2 <= width * height <= 2048**2:
                raise ValueError("Qwen output pixel area must be between 512 squared and 2048 squared")
            if model.startswith(("qwen-image-2.1", "qwen-image-3.0")) and not 1 / 8 <= width / height <= 8:
                raise ValueError("Qwen output aspect ratio must be between 1:8 and 8:1")
        if model.startswith("qwen-image-2.1") and options.negative_prompt is not None:
            raise ValueError("Qwen 2.1 does not support a negative prompt")
        if (legacy_edit or model.startswith("qwen-image-2.0")) and (
            options.prompt_extend_mode is not None or options.enable_thinking is not None
        ):
            raise ValueError("this Qwen family does not support thinking or prompt extension mode")
        if options.enable_thinking is True and options.prompt_extend is False:
            raise ValueError("Qwen thinking requires prompt extension")
        if isinstance(request, ImageEditRequest):
            limit = 10 if model.startswith("qwen-image-2.1") else 3
            if len(request.references) > limit:
                raise ValueError("the Qwen model does not support this reference count")
            if request.mask is not None or request.reference_kind != "general":
                raise ValueError("Qwen accepts general references without an explicit mask")
            if options.prompt_extend_mode == "agent":
                raise ValueError("Qwen agent prompt extension is available only for generation")
            if model.startswith(("qwen-image-2.1", "qwen-image-3.0")):
                for image in request.references:
                    if max(image.width, image.height) > 8000 or not 1 / 10 <= image.width / image.height <= 10:
                        raise ValueError("Qwen reference dimensions or aspect ratio exceed the model limit")

    async def send(
        self,
        client: httpx.AsyncClient,
        model: str,
        request: ImageGenerationRequest,
        images: tuple[ResolvedImage, ...],
    ) -> ImageReply:
        """Submit one native request and normalize its original image sources.

        Raises:
            ImageOperationError: For safe native business failures or oversized input.
            httpx.HTTPStatusError: For HTTP failures lacking a native error envelope.
            ValueError: If the native success response is malformed.
        """
        if any(len(image.data) > 10 * 1024 * 1024 for image in images):
            raise ImageOperationError(
                "invalid_request", "A Qwen image reference exceeds 10 MB.", operation_id=request.operation_id
            )
        content: list[JsonValue] = [{"image": image_data_url(image)} for image in images]
        content.append({"text": request.prompt})
        parameters = QwenImageOptions.model_validate(request.provider_options).model_dump(
            mode="json", exclude_none=True
        )
        parameters["n"] = request.count
        if request.size != "auto":
            parameters["size"] = request.size.replace("x", "*")
        response = await client.post(
            "services/aigc/multimodal-generation/generation",
            json={
                "model": model,
                "input": {"messages": [{"role": "user", "content": content}]},
                "parameters": parameters,
            },
        )
        try:
            envelope = QwenImageEnvelope.model_validate_json(response.content)
        except ValidationError as cause:
            response.raise_for_status()
            raise ImageOperationError(
                "invalid_response",
                "The Qwen image provider returned an invalid response.",
                operation_id=request.operation_id,
                provider_request_id=response.headers.get("x-request-id"),
            ) from cause
        request_id = envelope.request_id if envelope.request_id is not None else response.headers.get("x-request-id")
        if envelope.code:
            error = ImageOperationError(
                qwen_error_code(envelope.code, response.status_code),
                "The Qwen image operation failed.",
                operation_id=request.operation_id,
                provider_request_id=request_id,
            )
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as cause:
                raise error from cause
            raise error
        response.raise_for_status()
        try:
            payload = QwenImageResponse.model_validate_json(response.content)
            if payload.output is None:
                raise ValueError("Qwen image response is missing output")
            usage = None
            if payload.usage is not None:
                count = payload.usage.output_image_count
                if count is None:
                    count = payload.usage.image_count
                usage = ImageUsage(image_count=count)
            result = ImageResponse(
                data=tuple(
                    ImageSource(url=block.image)
                    for choice in payload.output.choices
                    for block in choice.message.content
                    if block.image is not None
                ),
                usage=usage,
                output_format="png",
            )
        except ValueError as cause:
            raise ImageOperationError(
                "invalid_response",
                "The Qwen image provider returned an invalid response.",
                operation_id=request.operation_id,
                provider_request_id=request_id,
            ) from cause
        return ImageReply(response=result, request_id=request_id)


def qwen_error_code(code: str, status: int) -> ImageErrorCode:
    """Classify native failures without exposing provider messages or secrets."""
    if code in {"Throttling.AllocationQuota", "Arrearage", "insufficient_quota"}:
        return "quota"
    if code in {"InvalidApiKey", "invalid_api_key", "AccessDenied", "access_denied"} or status in {401, 403}:
        return "authentication"
    if code.startswith("Throttling") or status == 429:
        return "rate_limit"
    if code in {"InvalidParameter", "ModelNotFound", "NotFound"} or status in {400, 404, 422}:
        return "invalid_request"
    if status >= 500:
        return "acceptance_unknown"
    return "invalid_response"
