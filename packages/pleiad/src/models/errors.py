from enum import StrEnum

from pleiad.errors.base import SystemFault


class ModelErrorCode(StrEnum):
    """Stable error codes owned by the Model component."""

    AUTHENTICATION = "model.authentication.failed"
    PERMISSION = "model.permission.denied"
    CONNECTION = "model.connection.failed"
    CONFIGURATION = "model.configuration.invalid"
    INVALID_REQUEST = "model.request.invalid"
    MALFORMED_RESPONSE = "model.response.malformed"
    OVERLOADED = "model.provider.overloaded"
    PROVIDER_ERROR = "model.provider.unavailable"
    RATE_LIMIT = "model.request.rate_limited"
    TIMEOUT = "model.request.timed_out"
    UNSUPPORTED = "model.feature.unsupported"


class ModelConfigurationError(SystemFault):
    """Report a Model configuration that cannot create a provider binding."""

    def __init__(self, message: str) -> None:
        """Create a Model configuration error.

        Args:
            message: Safe diagnostic description.
        """
        super().__init__(ModelErrorCode.CONFIGURATION, message)


class ModelAuthenticationError(SystemFault):
    """Report rejected provider credentials."""

    def __init__(self, message: str = "The model provider rejected authentication.") -> None:
        """Create an authentication error."""
        super().__init__(ModelErrorCode.AUTHENTICATION, message)


class ModelPermissionError(SystemFault):
    """Report credentials without access to the requested model operation."""

    def __init__(self, message: str = "The model provider denied the requested operation.") -> None:
        """Create a permission error."""
        super().__init__(ModelErrorCode.PERMISSION, message)


class ModelRateLimitError(SystemFault):
    """Report provider throttling that may be retried by the owning layer."""

    def __init__(self, message: str = "The model provider rate limit was exceeded.") -> None:
        """Create a rate-limit error."""
        super().__init__(ModelErrorCode.RATE_LIMIT, message)


class ModelTimeoutError(SystemFault):
    """Report expiry of a model request deadline."""

    def __init__(self, message: str = "The model provider request timed out.") -> None:
        """Create a timeout error."""
        super().__init__(ModelErrorCode.TIMEOUT, message)


class ModelConnectionError(SystemFault):
    """Report failure to establish or retain provider transport."""

    def __init__(self, message: str = "The model provider connection failed.") -> None:
        """Create a connection error."""
        super().__init__(ModelErrorCode.CONNECTION, message)


class ModelRequestError(SystemFault):
    """Report a provider-rejected model request."""

    def __init__(self, message: str = "The model provider rejected the request.") -> None:
        """Create a request error."""
        super().__init__(ModelErrorCode.INVALID_REQUEST, message)


class ModelResponseError(SystemFault):
    """Report a malformed or incomplete provider response."""

    def __init__(self, message: str = "The model provider response was malformed.") -> None:
        """Create a response error."""
        super().__init__(ModelErrorCode.MALFORMED_RESPONSE, message)


class ModelProviderUnavailableError(SystemFault):
    """Report a provider-side or unknown availability failure."""

    def __init__(self, message: str = "The model provider request failed.") -> None:
        """Create a provider availability error."""
        super().__init__(ModelErrorCode.PROVIDER_ERROR, message)


class ModelProviderOverloadedError(SystemFault):
    """Report a provider service failure or overload response."""

    def __init__(self, message: str = "The model provider is overloaded.") -> None:
        """Create a provider overload error."""
        super().__init__(ModelErrorCode.OVERLOADED, message)


class ModelUnsupportedError(SystemFault):
    """Report a requested feature unsupported by the selected protocol."""

    def __init__(self, message: str) -> None:
        """Create an unsupported-feature error."""
        super().__init__(ModelErrorCode.UNSUPPORTED, message)


def classify_provider_error(
    error: Exception,
) -> (
    ModelAuthenticationError
    | ModelPermissionError
    | ModelRateLimitError
    | ModelTimeoutError
    | ModelRequestError
    | ModelProviderOverloadedError
    | ModelConnectionError
    | ModelProviderUnavailableError
):
    """Classify an official provider SDK exception without exposing its content.

    Args:
        error: Exception raised by an official provider SDK.

    Returns:
        A sanitized Model system fault.
    """
    status = getattr(error, "status_code", None)
    if not isinstance(status, int):
        status = getattr(error, "http_code", None)
    name = type(error).__name__
    if status == 401 or name == "AuthenticationError":
        return ModelAuthenticationError()
    if status == 403 or name == "PermissionDeniedError":
        return ModelPermissionError()
    elif status == 429:
        return ModelRateLimitError()
    elif status in {408, 504} or isinstance(error, TimeoutError) or "Timeout" in name:
        return ModelTimeoutError()
    elif isinstance(status, int) and 400 <= status < 500:
        return ModelRequestError()
    elif isinstance(status, int) and status >= 500:
        return ModelProviderOverloadedError()
    if isinstance(error, ConnectionError) or "Connection" in name:
        return ModelConnectionError()
    return ModelProviderUnavailableError()
