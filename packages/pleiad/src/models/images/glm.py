from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from pleiad.contracts import ContractModel, JsonObject
from pleiad.messages.images import ResolvedImage
from pleiad.models.images.adapter import ImageReply
from pleiad.models.images.contracts import (
    ImageCapabilities,
    ImageEditRequest,
    ImageGenerationRequest,
    ImageResponse,
    ImageSource,
)
from pleiad.models.images.errors import ImageErrorCode, ImageOperationError


class GLMImageOptions(ContractModel):
    """Validate documented native controls for GLM-Image and CogView."""

    quality: Literal["hd", "standard"] | None = None
    watermark_enabled: bool | None = Field(default=None, strict=True)
    user_id: str | None = Field(default=None, min_length=6, max_length=128)


class GLMImageSource(BaseModel):
    """Require the URL returned by the native image generation endpoint."""

    model_config = ConfigDict(extra="ignore", frozen=True)
    url: str = Field(min_length=1)


class GLMImageError(BaseModel):
    """Read native error codes without publishing provider message contents."""

    model_config = ConfigDict(extra="ignore", frozen=True)
    code: str = Field(min_length=1)


class GLMImageResponse(BaseModel):
    """Validate native URL results separately from an optional business error."""

    model_config = ConfigDict(extra="ignore", frozen=True)
    data: tuple[GLMImageSource, ...] | None = Field(default=None, min_length=1, max_length=1)
    error: GLMImageError | None = None
    model: str | None = Field(default=None, min_length=1)
    request_id: str | None = Field(default=None, min_length=1)


class GLMImageAdapter:
    """Translate synchronous text-to-image requests for the documented GLM API.

    Reference editing and asynchronous task submission are not exposed by this
    synchronous adapter. Native quality remains explicit in provider options.
    """

    base_url = "https://open.bigmodel.cn/api/paas/v4/"
    capabilities = ImageCapabilities(
        image_input="unsupported",
        reference_edit="unsupported",
        mask_edit="unsupported",
        transparent_output="unsupported",
    )

    def validate(self, model: str, request: ImageGenerationRequest) -> None:
        """Reject unsupported operations and options before paid submission."""
        if isinstance(request, ImageEditRequest):
            raise ValueError("GLM image generation does not accept reference images")
        if model not in {"glm-image", "cogview-4-250304", "cogview-4", "cogview-3-flash"}:
            raise ValueError("model is not a documented GLM image generation model")
        options = GLMImageOptions.model_validate(request.provider_options)
        if request.count != 1:
            raise ValueError("GLM image generation returns one image per request")
        if request.quality != "auto" or request.background != "auto" or request.output_format is not None:
            raise ValueError("GLM requires native quality and does not expose background or output format")
        if model == "glm-image":
            if len(request.prompt) > 1000:
                raise ValueError("GLM-Image prompts must not exceed 1000 characters")
            if options.quality == "standard":
                raise ValueError("GLM-Image supports only hd quality")
        if request.size != "auto":
            width, height = (int(value) for value in request.size.split("x"))
            multiple = 32 if model == "glm-image" else 16
            max_pixels = 2**22 if model == "glm-image" else 2**21
            if not all(512 <= side <= 2048 and side % multiple == 0 for side in (width, height)):
                raise ValueError("GLM image dimensions violate the model's range or alignment")
            if width * height > max_pixels:
                raise ValueError("GLM image dimensions exceed the model's pixel budget")

    async def send(
        self,
        client: httpx.AsyncClient,
        model: str,
        request: ImageGenerationRequest,
        images: tuple[ResolvedImage, ...],
    ) -> ImageReply:
        """Submit native JSON once and retain validated original download URLs."""
        if images:
            raise ValueError("GLM image generation does not accept resolved images")
        options = GLMImageOptions.model_validate(request.provider_options)
        payload: JsonObject = {
            "model": model,
            "prompt": request.prompt,
            **options.model_dump(mode="json", exclude_none=True),
        }
        if request.size != "auto":
            payload["size"] = request.size
        response = await client.post("images/generations", json=payload)
        request_id = response.headers.get("x-request-id")
        try:
            parsed = GLMImageResponse.model_validate_json(response.content)
        except ValidationError as error:
            response.raise_for_status()
            raise ImageOperationError(
                "invalid_response",
                "The image provider returned an invalid response.",
                operation_id=request.operation_id,
                provider_request_id=request_id,
            ) from error
        if request_id is None:
            request_id = parsed.request_id
        if parsed.error is not None:
            failure = glm_image_error(parsed.error.code, response.status_code, request.operation_id, request_id)
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as error:
                raise failure from error
            raise failure
        response.raise_for_status()
        if parsed.data is None:
            raise ImageOperationError(
                "invalid_response",
                "The image provider returned no image result.",
                operation_id=request.operation_id,
                provider_request_id=request_id,
            )
        return ImageReply(
            ImageResponse(data=tuple(ImageSource(url=source.url) for source in parsed.data), model=parsed.model),
            request_id,
        )


def glm_image_error(code: str, status: int, operation_id: str, request_id: str | None) -> ImageOperationError:
    """Classify native errors without leaking the provider's message or body."""
    kind: ImageErrorCode
    if status >= 500 or status == 408:
        kind = "acceptance_unknown"
    elif code in {"1000", "1001", "1003", "1005", "1220"} or status in {401, 403}:
        kind = "authentication"
    elif code in {"1113", "1308", "1309", "1310", "1314", "1316", "1317", "1318", "1319", "1320", "1321"}:
        kind = "quota"
    elif code in {"1302", "1305", "1313"} or status == 429:
        kind = "rate_limit"
    elif code in {"1210", "1211", "1212", "1213", "1214", "1215", "1221", "1222", "1261", "1301"} or status in {
        400,
        404,
        422,
    }:
        kind = "invalid_request"
    else:
        kind = "acceptance_unknown"
    return ImageOperationError(
        kind,
        "The native image provider rejected the operation."
        if kind != "acceptance_unknown"
        else "Image operation acceptance requires host reconciliation.",
        operation_id=operation_id,
        provider_request_id=request_id,
    )
