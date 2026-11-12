from typing import Literal

import httpx
from pydantic import Field

from pleiad.contracts import ContractModel, JsonObject
from pleiad.messages.images import ResolvedImage
from pleiad.models.images.adapter import ImageReply
from pleiad.models.images.contracts import ImageCapabilities, ImageEditRequest, ImageGenerationRequest, ImageResponse
from pleiad.models.images.errors import ImageOperationError


class OpenAIImageOptions(ContractModel):
    """Validate optional GPT Image controls without overriding shared fields."""

    output_compression: int | None = Field(default=None, ge=0, le=100, strict=True)
    input_fidelity: Literal["low", "high"] | None = None
    moderation: Literal["auto", "low"] | None = None
    user: str | None = Field(default=None, min_length=1)


class OpenAIImageAdapter:
    """Translate GPT Image generation and multipart reference editing."""

    base_url = "https://api.openai.com/v1/"
    capabilities = ImageCapabilities(character_reference="unverified")

    def validate(self, model: str, request: ImageGenerationRequest) -> None:
        """Validate GPT Image options before submitting an operation."""
        options = OpenAIImageOptions.model_validate(request.provider_options)
        if model.startswith("dall-e"):
            raise ValueError("this adapter requires a GPT Image model")
        if not isinstance(request, ImageEditRequest) and options.input_fidelity is not None:
            raise ValueError("input_fidelity requires reference editing")
        if isinstance(request, ImageEditRequest) and options.moderation is not None:
            raise ValueError("moderation is a generation-only option")
        if options.output_compression is not None and request.output_format not in {"jpeg", "webp"}:
            raise ValueError("output_compression requires jpeg or webp")

    async def send(
        self,
        client: httpx.AsyncClient,
        model: str,
        request: ImageGenerationRequest,
        images: tuple[ResolvedImage, ...],
    ) -> ImageReply:
        """Submit JSON generation or ordered multipart edit to OpenAI Images."""
        options = OpenAIImageOptions.model_validate(request.provider_options)
        payload: JsonObject = {
            "model": model,
            "prompt": request.prompt,
            "n": request.count,
            "size": request.size,
            "quality": request.quality,
            "output_format": "png" if request.output_format is None else request.output_format,
            "background": request.background,
            **options.model_dump(mode="json", exclude_none=True),
        }
        if isinstance(request, ImageEditRequest):
            files = [("image[]", image_file(image)) for image in images[: len(request.references)]]
            if request.mask is not None:
                files.append(("mask", image_file(images[-1])))
            response = await client.post(
                "images/edits", data={key: str(value) for key, value in payload.items()}, files=files
            )
        else:
            response = await client.post("images/generations", json=payload)
        response.raise_for_status()
        request_id = response.headers.get("x-request-id")
        try:
            parsed = ImageResponse.model_validate_json(response.content)
        except ValueError as error:
            raise ImageOperationError(
                "invalid_response",
                "The image provider returned an invalid response.",
                operation_id=request.operation_id,
                provider_request_id=request_id,
            ) from error
        return ImageReply(parsed, request_id)


def image_file(image: ResolvedImage) -> tuple[str, bytes, str]:
    """Build a multipart file with a non-sensitive content-addressed filename."""
    extension = image.image.mime.removeprefix("image/")
    return (f"{image.image.sha256}.{extension}", image.data, image.image.mime)
