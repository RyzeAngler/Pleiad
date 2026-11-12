import base64
from dataclasses import dataclass
from typing import Protocol

import httpx

from pleiad.messages.images import ResolvedImage
from pleiad.models.images.contracts import ImageCapabilities, ImageGenerationRequest, ImageResponse


@dataclass(frozen=True, slots=True)
class ImageReply:
    """Retain validated native response facts before downloading image assets."""

    response: ImageResponse
    request_id: str | None


class ImageAdapter(Protocol):
    """Translate one native image API without owning operation admission or assets."""

    base_url: str
    capabilities: ImageCapabilities

    def validate(self, model: str, request: ImageGenerationRequest) -> None:
        """Reject unsupported options before authorization or paid submission."""
        ...

    async def send(
        self,
        client: httpx.AsyncClient,
        model: str,
        request: ImageGenerationRequest,
        images: tuple[ResolvedImage, ...],
    ) -> ImageReply:
        """Submit once, raising HTTP status or safe business errors on failure."""
        ...


def image_data_url(image: ResolvedImage) -> str:
    """Encode authorized bytes only at the native provider request boundary."""
    return f"data:{image.image.mime};base64,{base64.b64encode(image.data).decode('ascii')}"
