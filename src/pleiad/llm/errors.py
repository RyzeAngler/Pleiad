"""Normalized errors exposed by the LLM layer."""

from enum import StrEnum
from typing import Any

from pleiad.llm.config import LLMProtocol, LLMProvider


class LLMErrorCode(StrEnum):
    """Stable categories for provider-boundary failures."""

    AUTHENTICATION = "authentication"
    CONFIGURATION = "configuration"
    CONTENT_FILTERED = "content_filtered"
    INVALID_REQUEST = "invalid_request"
    MALFORMED_RESPONSE = "malformed_response"
    OVERLOADED = "overloaded"
    PROVIDER_ERROR = "provider_error"
    RATE_LIMIT = "rate_limit"
    TIMEOUT = "timeout"
    UNSUPPORTED_CAPABILITY = "unsupported_capability"


class LLMError(RuntimeError):
    """Sanitized provider-boundary failure."""

    def __init__(
        self,
        code: LLMErrorCode,
        message: str,
        *,
        retryable: bool,
        protocol: LLMProtocol | None = None,
        provider: LLMProvider | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        """Create one normalized LLM failure."""
        super().__init__(message)
        self.message = message
        self.code = code
        self.retryable = retryable
        self.protocol = protocol
        self.provider = provider
        self.details = details or {}


class LLMConfigurationError(LLMError):
    """Configuration cannot create a usable LLM client."""

    def __init__(self, message: str) -> None:
        """Create a non-retryable configuration failure."""
        super().__init__(LLMErrorCode.CONFIGURATION, message, retryable=False)


class UnsupportedCapabilityError(LLMError):
    """The selected provider cannot satisfy a requested LLM capability."""

    def __init__(
        self,
        message: str,
        *,
        protocol: LLMProtocol,
        provider: LLMProvider,
    ) -> None:
        """Create a non-retryable capability failure."""
        super().__init__(
            LLMErrorCode.UNSUPPORTED_CAPABILITY,
            message,
            retryable=False,
            protocol=protocol,
            provider=provider,
        )
