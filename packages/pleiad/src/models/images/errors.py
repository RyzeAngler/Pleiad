from typing import Literal

from pleiad.models.images.contracts import ImageRecovery

type ImageErrorCode = Literal[
    "invalid_configuration",
    "unsupported_capability",
    "authentication",
    "rate_limit",
    "quota",
    "invalid_request",
    "invalid_response",
    "download_failed",
    "acceptance_unknown",
    "operation_conflict",
]


class ImageOperationError(Exception):
    """Expose safe operation facts while preserving the original exception cause."""

    def __init__(
        self,
        code: ImageErrorCode,
        message: str,
        *,
        operation_id: str,
        provider_request_id: str | None = None,
        recovery: ImageRecovery | None = None,
    ) -> None:
        """Create an error containing identity and optional download recovery data."""
        super().__init__(message)
        self.code = code
        self.operation_id = operation_id
        self.provider_request_id = provider_request_id
        self.recovery = recovery
