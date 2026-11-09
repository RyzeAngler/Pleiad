import asyncio
from enum import StrEnum

from pleiad.errors.base import SystemFault


class SandboxCancelledError(asyncio.CancelledError):
    """Preserve cancellation with the concrete container cleanup result."""

    def __init__(self, *, confirmed_stop: bool) -> None:
        """Retain whether the backend confirmed that its owned container stopped."""
        super().__init__("The Sandbox execution was cancelled")
        self.confirmed_stop = confirmed_stop


class SandboxErrorCode(StrEnum):
    """Stable error codes owned by the Sandbox component."""

    CONFIGURATION_INVALID = "sandbox.config.invalid"
    RUNTIME_UNAVAILABLE = "sandbox.runtime.unavailable"
    EXECUTION_FAILED = "sandbox.execution.failed"
    CLEANUP_FAILED = "sandbox.cleanup.failed"


class SandboxConfigurationError(SystemFault):
    """Report a Sandbox configuration or request contract failure."""

    def __init__(self, message: str = "The Sandbox configuration is invalid.") -> None:
        """Create a configuration system fault."""
        super().__init__(SandboxErrorCode.CONFIGURATION_INVALID, message)


class SandboxUnavailableError(SystemFault):
    """Report an unavailable or unreachable selected OCI runtime."""

    def __init__(self, message: str = "The selected OCI runtime is unavailable.") -> None:
        """Create an unavailable-runtime system fault."""
        super().__init__(SandboxErrorCode.RUNTIME_UNAVAILABLE, message)


class SandboxExecutionError(SystemFault):
    """Report a container creation, start, inspect, or output failure."""

    def __init__(self, message: str = "The Sandbox container could not execute the request.") -> None:
        """Create an execution system fault."""
        super().__init__(SandboxErrorCode.EXECUTION_FAILED, message)


class SandboxCleanupError(SystemFault):
    """Report cleanup that did not establish removal of the owned container."""

    def __init__(self, message: str = "The Sandbox container could not be cleaned up.") -> None:
        """Create a cleanup system fault."""
        super().__init__(SandboxErrorCode.CLEANUP_FAILED, message)
