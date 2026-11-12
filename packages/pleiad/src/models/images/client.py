import asyncio
import base64
import binascii
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx
from pydantic import BaseModel, ConfigDict, SecretStr, ValidationError

from pleiad.messages.images import ImageLimits, ImageReadScope, ImageResolver, ResolvedImage, resolve_images
from pleiad.models.binding import binding_fingerprint
from pleiad.models.images.adapter import ImageAdapter
from pleiad.models.images.contracts import (
    ImageCapabilities,
    ImageClientSettings,
    ImageEditRequest,
    ImageGenerationRequest,
    ImageOutput,
    ImageProvider,
    ImageRecovery,
    ImageResult,
)
from pleiad.models.images.errors import ImageErrorCode, ImageOperationError
from pleiad.models.images.glm import GLMImageAdapter
from pleiad.models.images.minimax import MiniMaxImageAdapter
from pleiad.models.images.openai import OpenAIImageAdapter
from pleiad.models.images.qwen import QwenImageAdapter
from pleiad.models.images.volcengine import VolcengineImageAdapter


@dataclass(slots=True)
class ImageOperation:
    """Serialize identical local submissions and retain their terminal facts."""

    digest: str
    scope: ImageReadScope | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    result: ImageResult | None = None
    error: ImageOperationError | None = None
    recovery: ImageRecovery | None = None


class ImageClient:
    """Run native image operations with shared admission and download recovery.

    The host owns durable operation admission and image decoding. Local duplicate
    protection lasts until this client is closed. Unverified capabilities permit
    host validation requests and remain explicitly unverified in the binding.
    """

    def __init__(
        self,
        *,
        provider: ImageProvider,
        model: str,
        api_key: str | SecretStr,
        base_url: str | None = None,
        timeout_seconds: float = 120.0,
        capabilities: ImageCapabilities | None = None,
        image_limits: ImageLimits | None = None,
        max_output_bytes: int = 50 * 1024 * 1024,
        transport: httpx.AsyncBaseTransport | None = None,
        download_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """Bind an exact model, endpoint and owned HTTP resources.

        Args:
            provider: SDK provider whose native image API will be called.
            model: Host-selected image model name.
            api_key: Credential kept out of binding fingerprints and public errors.
            base_url: Native API prefix, including its version path.
            timeout_seconds: Timeout for provider and download HTTP requests.
            capabilities: Host capability declarations for this exact binding.
            image_limits: Reference count, dimensions and byte limits.
            max_output_bytes: Maximum decoded or downloaded bytes per output image.
            transport: Optional external HTTP transport for provider requests.
            download_transport: Optional credential-free result download transport.

        Raises:
            ImageOperationError: If binding configuration is invalid.
        """
        self.adapter = image_adapter(provider)
        endpoint = self.adapter.base_url if base_url is None else base_url
        credential = api_key.get_secret_value() if isinstance(api_key, SecretStr) else api_key
        if not credential.strip():
            raise ImageOperationError(
                "invalid_configuration", "The image client configuration is invalid.", operation_id=""
            )
        try:
            settings = ImageClientSettings(
                model=model,
                timeout_seconds=timeout_seconds,
                max_output_bytes=max_output_bytes,
            )
        except ValidationError as error:
            raise ImageOperationError(
                "invalid_configuration", "The image client configuration is invalid.", operation_id=""
            ) from error
        self.model = settings.model
        self.capabilities = binding_capabilities(self.adapter.capabilities, capabilities)
        self.provider = provider
        self.image_limits = ImageLimits() if image_limits is None else image_limits
        self.max_output_bytes = settings.max_output_bytes
        try:
            self.binding_fingerprint = binding_fingerprint(
                protocol="native-images",
                provider=provider,
                base_url=endpoint,
                model=model,
                provider_options={
                    "timeout_seconds": timeout_seconds,
                    "capabilities": self.capabilities.model_dump(mode="json"),
                },
            )
        except ValueError as error:
            raise ImageOperationError(
                "invalid_configuration", "The image client binding is invalid.", operation_id=""
            ) from error
        self.client = httpx.AsyncClient(
            base_url=endpoint.rstrip("/") + "/",
            headers={"Authorization": f"Bearer {credential}"},
            transport=transport,
            timeout=timeout_seconds,
            follow_redirects=False,
        )
        self.downloader = httpx.AsyncClient(
            transport=download_transport, timeout=timeout_seconds, follow_redirects=False
        )
        self.operations: dict[str, ImageOperation] = {}
        self.closed = False

    async def generate(self, request: ImageGenerationRequest) -> ImageResult:
        """Generate images once for a locally stable operation identity.

        Args:
            request: Validated request including the host's durable operation ID.

        Returns:
            Actual images and provider-reported metadata.

        Raises:
            ImageOperationError: If submission fails or an operation conflicts.
        """
        if isinstance(request, ImageEditRequest):
            raise ImageOperationError(
                "invalid_request", "Use edit for image references.", operation_id=request.operation_id
            )
        return await self.submit(request)

    async def edit(
        self,
        request: ImageEditRequest,
        *,
        resolver: ImageResolver,
        scope: ImageReadScope,
    ) -> ImageResult:
        """Edit authorized, integrity-checked references in their declared order.

        Args:
            request: Validated references, explicit base and optional mask.
            resolver: Host authorizer and image reader.
            scope: Session and Run that authorize these image reads.

        Returns:
            Actual edited images and provider-reported metadata.

        Raises:
            ImageOperationError: If submission fails or a capability is unsupported.
            ValueError: If image content violates the reference or resource contract.
        """
        return await self.submit(request, resolver=resolver, scope=scope)

    def validate_capabilities(self, request: ImageGenerationRequest) -> None:
        """Reject closed clients and explicitly unsupported requested capabilities."""
        if self.closed:
            raise ImageOperationError(
                "invalid_configuration", "The image client is closed.", operation_id=request.operation_id
            )
        required = [self.capabilities.image_generate]
        if isinstance(request, ImageEditRequest):
            required = [
                self.capabilities.character_reference
                if request.reference_kind == "character"
                else self.capabilities.reference_edit
            ]
            if request.mask is not None:
                required.append(self.capabilities.mask_edit)
        if request.background == "transparent":
            required.append(self.capabilities.transparent_output)
        if "unsupported" in required:
            raise ImageOperationError(
                "unsupported_capability",
                "The image binding does not support this operation.",
                operation_id=request.operation_id,
            )

    async def submit(
        self,
        request: ImageGenerationRequest,
        *,
        resolver: ImageResolver | None = None,
        scope: ImageReadScope | None = None,
    ) -> ImageResult:
        """Own local admission, network submission and response materialization."""
        self.validate_capabilities(request)
        try:
            self.adapter.validate(self.model, request)
        except ValueError as error:
            raise ImageOperationError(
                "invalid_request", "The native image request options are invalid.", operation_id=request.operation_id
            ) from error
        digest = request.payload_digest()
        operation = self.operations.setdefault(request.operation_id, ImageOperation(digest, scope=scope))
        if operation.digest != digest or operation.scope != scope:
            raise ImageOperationError(
                "operation_conflict",
                "The operation identity already has different input.",
                operation_id=request.operation_id,
            )
        async with operation.lock:
            if operation.result is not None:
                return operation.result
            if operation.error is not None:
                raise operation.error
            images: tuple[ResolvedImage, ...] = ()
            if isinstance(request, ImageEditRequest):
                if resolver is None or scope is None:
                    raise ValueError("editing requires an image resolver and read scope")
                references = request.references
                limits = self.image_limits
                if request.mask is not None:
                    references = (*references, request.mask)
                    limits = limits.model_copy(update={"max_images": limits.max_images + 1})
                images = await resolve_images(references, resolver, scope, limits)
            operation.error = ImageOperationError(
                "acceptance_unknown",
                "Image operation acceptance requires host reconciliation.",
                operation_id=request.operation_id,
            )
            try:
                recovery = await self.send(request, images, scope=scope)
                operation.recovery = recovery
                operation.error = ImageOperationError(
                    "download_failed",
                    "The original image result requires recovery.",
                    operation_id=request.operation_id,
                    provider_request_id=recovery.provider_request_id,
                    recovery=recovery,
                )
                operation.result = await self.materialize(recovery)
            except ImageOperationError as error:
                operation.error = error
                raise
            operation.error = None
            return operation.result

    async def send(
        self,
        request: ImageGenerationRequest,
        images: tuple[ResolvedImage, ...],
        *,
        scope: ImageReadScope | None,
    ) -> ImageRecovery:
        """Send one provider request and retain its validated original response."""
        try:
            reply = await self.adapter.send(self.client, self.model, request, images)
            return ImageRecovery(
                operation_id=request.operation_id,
                request_digest=request.payload_digest(),
                binding_fingerprint=self.binding_fingerprint,
                provider_request_id=reply.request_id,
                model_name=self.model,
                scope=scope,
                response=reply.response,
            )
        except httpx.HTTPStatusError as error:
            raise provider_error(error, request.operation_id) from error
        except httpx.RequestError as error:
            raise ImageOperationError(
                "acceptance_unknown",
                "Image operation acceptance requires host reconciliation.",
                operation_id=request.operation_id,
            ) from error
        except ValueError as error:
            raise ImageOperationError(
                "invalid_response",
                "The image provider returned an invalid response.",
                operation_id=request.operation_id,
            ) from error

    async def materialize(self, recovery: ImageRecovery) -> ImageResult:
        """Decode or download all original outputs without any generation request."""
        images: list[ImageOutput] = []
        for source in recovery.response.data:
            try:
                if source.b64_json is not None:
                    if len(source.b64_json) > ((self.max_output_bytes + 2) // 3) * 4:
                        raise ValueError("encoded image exceeds the output limit")
                    data = base64.b64decode(source.b64_json, validate=True)
                elif source.url is not None:
                    data = await self.download(source.url)
                else:
                    raise ValueError("image response source is missing")
                if len(data) > self.max_output_bytes:
                    raise ValueError("image exceeds the output limit")
                mime = image_mime(data)
                if recovery.response.output_format is not None and mime != f"image/{recovery.response.output_format}":
                    raise ValueError("reported output format does not match image content")
            except httpx.HTTPError as error:
                raise ImageOperationError(
                    "download_failed",
                    "The original image result could not be downloaded.",
                    operation_id=recovery.operation_id,
                    provider_request_id=recovery.provider_request_id,
                    recovery=recovery,
                ) from error
            except (ValueError, binascii.Error, httpx.InvalidURL) as error:
                raise ImageOperationError(
                    "invalid_response",
                    "The image provider returned invalid image content.",
                    operation_id=recovery.operation_id,
                    provider_request_id=recovery.provider_request_id,
                ) from error
            images.append(ImageOutput(data=data, mime=mime, revised_prompt=source.revised_prompt))
        return ImageResult(
            operation_id=recovery.operation_id,
            provider_request_id=recovery.provider_request_id,
            model_name=recovery.model_name,
            reported_model_name=recovery.response.model,
            images=tuple(images),
            usage=recovery.response.usage,
            completed_at=datetime.now(UTC),
        )

    async def download(self, url: str) -> bytes:
        """Download bounded result bytes without forwarding provider credentials."""
        parsed = httpx.URL(url)
        if parsed.scheme not in {"http", "https"} or not parsed.host or parsed.userinfo:
            raise ValueError("image download requires a credential-free HTTP URL")
        data = bytearray()
        async with self.downloader.stream("GET", parsed) as response:
            response.raise_for_status()
            async for chunk in response.aiter_bytes():
                data.extend(chunk)
                if len(data) > self.max_output_bytes:
                    raise ValueError("downloaded image exceeds the output limit")
        return bytes(data)

    async def recover(self, recovery: ImageRecovery) -> ImageResult:
        """Recover downloads from an original response, including after process restart.

        Args:
            recovery: Sensitive original response retained by the host operation ledger.

        Returns:
            Materialized original images, without submitting another paid operation.

        Raises:
            ImageOperationError: If bindings conflict or original download fails again.
        """
        if self.closed or recovery.binding_fingerprint != self.binding_fingerprint or recovery.model_name != self.model:
            raise ImageOperationError(
                "invalid_configuration",
                "Image recovery requires the original open binding.",
                operation_id=recovery.operation_id,
            )
        candidate = ImageOperation(recovery.request_digest, scope=recovery.scope)
        operation = self.operations.setdefault(recovery.operation_id, candidate)
        async with operation.lock:
            if (
                operation.digest != recovery.request_digest
                or operation.scope != recovery.scope
                or (operation is not candidate and operation.recovery != recovery)
            ):
                raise ImageOperationError(
                    "operation_conflict",
                    "Recovery conflicts with the original operation.",
                    operation_id=recovery.operation_id,
                )
            if operation.result is not None:
                return operation.result
            operation.recovery = recovery
            operation.error = ImageOperationError(
                "download_failed",
                "The original image download requires recovery.",
                operation_id=recovery.operation_id,
                provider_request_id=recovery.provider_request_id,
                recovery=recovery,
            )
            try:
                operation.result = await self.materialize(recovery)
            except ImageOperationError as error:
                operation.error = error
                raise
            operation.error = None
            return operation.result

    async def aclose(self) -> None:
        """Close both owned HTTP clients after the host settles active operations."""
        self.closed = True
        try:
            await self.client.aclose()
        finally:
            await self.downloader.aclose()


def image_mime(data: bytes) -> str:
    """Identify supported output signatures before host-level image decoding."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    raise ValueError("image output has an unsupported or missing format signature")


class NativeErrorDetail(BaseModel):
    """Read only the code required for safe HTTP error classification."""

    model_config = ConfigDict(extra="ignore")
    code: str | None = None


class NativeErrorBody(BaseModel):
    """Allow a missing envelope without assuming a successful provider response."""

    model_config = ConfigDict(extra="ignore")
    error: NativeErrorDetail | None = None


def provider_error(error: httpx.HTTPStatusError, operation_id: str) -> ImageOperationError:
    """Classify native HTTP failures without copying private provider response text."""
    status = error.response.status_code
    code: ImageErrorCode = "invalid_request"
    if status in {401, 403}:
        code = "authentication"
    elif status == 429:
        code = "rate_limit"
        try:
            body = NativeErrorBody.model_validate_json(error.response.content)
        except ValidationError:
            body = NativeErrorBody()
        if body.error is not None and body.error.code in {"insufficient_quota", "billing_hard_limit_reached"}:
            code = "quota"
    elif status >= 500 or status == 408:
        code = "acceptance_unknown"
    return ImageOperationError(
        code,
        f"Image operation failed with {code}.",
        operation_id=operation_id,
        provider_request_id=error.response.headers.get("x-request-id"),
    )


def image_adapter(provider: ImageProvider) -> ImageAdapter:
    """Select the implemented native API or reject vendors without one."""
    match provider:
        case "openai":
            return OpenAIImageAdapter()
        case "qwen":
            return QwenImageAdapter()
        case "glm":
            return GLMImageAdapter()
        case "minimax":
            return MiniMaxImageAdapter()
        case "volcengine":
            return VolcengineImageAdapter()
        case _:
            raise ImageOperationError(
                "unsupported_capability",
                "This provider has no integrated public image-generation API.",
                operation_id="",
            )


def binding_capabilities(native: ImageCapabilities, declared: ImageCapabilities | None) -> ImageCapabilities:
    """Keep native unsupported capabilities authoritative over host declarations."""
    if declared is None:
        return native
    values = declared.model_dump(mode="json")
    for key, status in native.model_dump(mode="json").items():
        if status == "unsupported":
            values[key] = "unsupported"
    return ImageCapabilities.model_validate(values)
