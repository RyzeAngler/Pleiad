from collections.abc import AsyncIterator, Mapping
from importlib import import_module
from typing import Protocol, runtime_checkable

from pydantic import SecretStr, ValidationError

from pleiad.contracts import JsonValue
from pleiad.models.errors import ModelConfigurationError, ModelRequestError
from pleiad.models.openai.request import OpenAIRequest, parse_request
from pleiad.models.openai.response import OpenAIResponse, OpenAIStreamChunk


@runtime_checkable
class OpenAIProviderPort(Protocol):
    """Internal provider operations used by the OpenAI protocol object."""

    async def complete(self, request: OpenAIRequest) -> OpenAIResponse:
        """Send one OpenAI request through a provider SDK."""

    def stream(self, request: OpenAIRequest) -> AsyncIterator[OpenAIStreamChunk]:
        """Stream one OpenAI request through a provider SDK."""

    async def aclose(self) -> None:
        """Release provider SDK resources."""


class OpenAIModelClient:
    """OpenAI protocol client backed by a selected vendor SDK."""

    def __init__(
        self,
        provider: str,
        model: str,
        api_key: str | SecretStr,
        *,
        base_url: str | None = None,
        timeout_seconds: float = 120.0,
        max_retries: int = 2,
        provider_options: Mapping[str, JsonValue] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        """Create an OpenAI protocol object for one provider.

        Args:
            provider: Vendor identifier such as ``openai`` or ``glm``.
            model: Vendor model name.
            api_key: Host-provided vendor credential.
            base_url: Optional vendor endpoint override.
            timeout_seconds: Per-request SDK timeout.
            max_retries: SDK retry limit.
            provider_options: Vendor-specific SDK options.
            headers: Additional vendor request headers.
        Raises:
            ModelConfigurationError: If the provider package or binding is unavailable.
            ValueError: If the client configuration is invalid.
        """
        if not provider.strip():
            raise ValueError("provider must be non-empty")
        if not model.strip():
            raise ValueError("model must be non-empty")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if max_retries < 0:
            raise ValueError("max_retries must not be negative")
        self.provider = provider
        self.model = model
        self.api_key = api_key if isinstance(api_key, SecretStr) else SecretStr(api_key)
        self.base_url = base_url
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.provider_options = dict(provider_options) if provider_options is not None else {}
        self.headers = dict(headers) if headers is not None else {}
        self.binding = self.load_provider()

    async def complete(self, request: OpenAIRequest | Mapping[str, object]) -> OpenAIResponse:
        """Validate and send an OpenAI Chat Completions request body."""
        parsed = self.parse(request)
        non_streaming = parsed.model_copy(update={"stream": False})
        return await self.binding.complete(non_streaming)

    async def stream(
        self,
        request: OpenAIRequest | Mapping[str, object],
    ) -> AsyncIterator[OpenAIStreamChunk]:
        """Validate and stream OpenAI Chat Completions chunks."""
        parsed = self.parse(request)
        streamed = parsed.model_copy(update={"stream": True})
        async for chunk in self.binding.stream(streamed):
            yield chunk

    async def aclose(self) -> None:
        """Close the selected provider operation."""
        await self.binding.aclose()

    def parse(self, request: OpenAIRequest | Mapping[str, object]) -> OpenAIRequest:
        """Validate one caller-provided OpenAI request body."""
        try:
            parsed = parse_request(request)
        except ValidationError as error:
            raise ModelRequestError(
                "The OpenAI request body is invalid.",
            ) from error
        if parsed.model is not None and parsed.model != self.model:
            raise ModelRequestError(
                "The request model must match the model client.",
            )
        return parsed

    def load_provider(self) -> OpenAIProviderPort:
        """Load the configured provider module and construct its binding."""
        if not self.provider.isidentifier():
            raise ModelConfigurationError(
                f"unsupported OpenAI provider: {self.provider}",
            )
        module_name = f"pleiad.models.providers.{self.provider}"
        try:
            module = import_module(module_name)
        except ModuleNotFoundError as error:
            raise ModelConfigurationError(
                f"provider {self.provider} requires its official SDK extra",
            ) from error
        candidate: object = getattr(module, "OpenAIProvider", None)
        if not callable(candidate):
            raise ModelConfigurationError(
                f"provider {self.provider} does not support the OpenAI protocol",
            )
        try:
            binding = candidate(
                model=self.model,
                api_key=self.api_key,
                base_url=self.base_url,
                timeout_seconds=self.timeout_seconds,
                max_retries=self.max_retries,
                provider_options=self.provider_options,
                headers=self.headers,
            )
        except (ImportError, TypeError, ValueError) as error:
            raise ModelConfigurationError(
                f"provider {self.provider} could not create its OpenAI binding",
            ) from error
        if not isinstance(binding, OpenAIProviderPort):
            raise ModelConfigurationError(
                f"provider {self.provider} returned an invalid OpenAI binding",
            )
        return binding
