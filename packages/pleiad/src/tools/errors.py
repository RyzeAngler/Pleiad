import asyncio
from enum import StrEnum

from pleiad.errors.base import BusinessError, SystemFault


class ToolErrorCode(StrEnum):
    """Stable error codes owned by the Tool component."""

    NOT_FOUND = "tool.not_found"
    RESULT_NOT_FOUND = "tool.result.not_found"
    INVALID_ARGUMENTS = "tool.arguments.invalid"
    PERMISSION_DENIED = "tool.permission.denied"
    TIMEOUT = "tool.execution.timed_out"
    INVALID_OUTPUT = "tool.output.invalid"
    EXECUTION_FAILED = "tool.execution.failed"
    REMOTE_FAILED = "tool.remote.failed"
    NOT_ALLOWED = "tool.execution.not_allowed"
    FILE_NOT_FOUND = "tool.file.not_found"
    FILE_ACCESS_DENIED = "tool.file.access_denied"
    FILE_CONFLICT = "tool.file.conflict"
    FILE_INVALID_ENCODING = "tool.file.invalid_encoding"
    FILE_TOO_LARGE = "tool.file.too_large"
    SKILL_NOT_SELECTED = "tool.skill.not_selected"
    RUNTIME_UNAVAILABLE = "tool.runtime.unavailable"


class ToolCancelledError(asyncio.CancelledError):
    """Carry the backend's actual stop confirmation while preserving cancellation propagation."""

    def __init__(self, *, confirmed_stop: bool) -> None:
        """Retain concrete cleanup certainty from the executing backend."""
        super().__init__("The Tool execution was cancelled")
        self.confirmed_stop = confirmed_stop


class ToolError(BusinessError):
    """Base class for expected Tool failures."""


class BackgroundNotAcceptedError(ToolError):
    """Report a confirmed rejection before a background backend accepted work."""

    def __init__(self, message: str = "The backend did not accept this background action.") -> None:
        """Keep backend rejection distinct from an accepted action's later outcome."""
        super().__init__("tool.background.not_accepted", message)


class ToolSystemFault(SystemFault):
    """Report an unexpected Tool runtime failure."""

    def __init__(self, message: str = "The Tool runtime failed unexpectedly.") -> None:
        """Create a Tool system fault.

        Args:
            message: Diagnostic description of the runtime failure.
        """
        super().__init__(ToolErrorCode.RUNTIME_UNAVAILABLE, message)


class UnknownToolError(ToolError):
    """Report a Tool name that is not available."""

    def __init__(self, message: str = "The requested Tool is not available.") -> None:
        """Create an unknown Tool error.

        Args:
            message: Diagnostic description of the missing Tool.
        """
        super().__init__(ToolErrorCode.NOT_FOUND, message)


class InvalidToolArgumentsError(ToolError):
    """Report Tool arguments that violate the input contract."""

    def __init__(self, message: str = "The Tool arguments are invalid.") -> None:
        """Create an invalid Tool arguments error.

        Args:
            message: Diagnostic description of the invalid arguments.
        """
        super().__init__(ToolErrorCode.INVALID_ARGUMENTS, message)


class ToolResultNotFoundError(ToolError):
    """Report a result reference outside the current completed history."""

    def __init__(
        self, message: str = "The Tool result is not available in this conversation's completed history."
    ) -> None:
        """Create a missing result error.

        Args:
            message: Diagnostic description without exposing another memory scope.
        """
        super().__init__(ToolErrorCode.RESULT_NOT_FOUND, message)


class ToolPermissionDeniedError(ToolError):
    """Report missing permissions for a Tool invocation."""

    def __init__(self, message: str = "The Tool invocation lacks the required permissions.") -> None:
        """Create a Tool permission error.

        Args:
            message: Diagnostic description of the denied invocation.
        """
        super().__init__(ToolErrorCode.PERMISSION_DENIED, message)


class ToolExecutionFailedError(ToolError):
    """Report a Tool execution failure."""

    def __init__(self, message: str = "The Tool execution failed.") -> None:
        """Create a Tool execution error.

        Args:
            message: Diagnostic description of the execution failure.
        """
        super().__init__(ToolErrorCode.EXECUTION_FAILED, message)


class RemoteToolError(ToolError):
    """Report a remote Tool's model-visible business failure."""

    def __init__(self, message: str = "The remote Tool reported an execution failure.") -> None:
        """Create a remote Tool error.

        Args:
            message: Diagnostic description of the remote failure.
        """
        super().__init__(ToolErrorCode.REMOTE_FAILED, message)


class TransientToolError(ToolExecutionFailedError):
    """Mark a Tool execution failure as safe for an idempotent retry."""


class ToolTimeoutError(TransientToolError):
    """Report a Tool execution timeout."""

    def __init__(self, message: str = "The Tool execution exceeded its configured timeout.") -> None:
        """Create a Tool timeout error.

        Args:
            message: Diagnostic description of the timeout.
        """
        ToolError.__init__(self, ToolErrorCode.TIMEOUT, message)


class InvalidToolOutputError(ToolError):
    """Report Tool output that violates the normalized result contract."""

    def __init__(self, message: str = "The Tool output does not satisfy the result contract.") -> None:
        """Create an invalid Tool output error.

        Args:
            message: Diagnostic description of the invalid output.
        """
        super().__init__(ToolErrorCode.INVALID_OUTPUT, message)


class ToolNotAllowedError(ToolError):
    """Report a Tool invocation rejected by host policy."""

    def __init__(self, message: str = "The Tool invocation is not allowed by host policy.") -> None:
        """Create a Tool policy error.

        Args:
            message: Diagnostic description of the rejected invocation.
        """
        super().__init__(ToolErrorCode.NOT_ALLOWED, message)


class ToolFileNotFoundError(ToolError):
    """Report a scoped file that does not exist."""

    def __init__(self, message: str = "The requested file does not exist.") -> None:
        """Create a missing file error.

        Args:
            message: Diagnostic description of the missing file.
        """
        super().__init__(ToolErrorCode.FILE_NOT_FOUND, message)


class ToolFileAccessDeniedError(ToolError):
    """Report a file path outside the permitted boundary."""

    def __init__(self, message: str = "Access to the requested file path is denied.") -> None:
        """Create a file access error.

        Args:
            message: Diagnostic description of the denied path.
        """
        super().__init__(ToolErrorCode.FILE_ACCESS_DENIED, message)


class ToolFileConflictError(ToolError):
    """Report a conflicting file update."""

    def __init__(self, message: str = "The file changed before the requested update completed.") -> None:
        """Create a file conflict error.

        Args:
            message: Diagnostic description of the conflict.
        """
        super().__init__(ToolErrorCode.FILE_CONFLICT, message)


class ToolFileEncodingError(ToolError):
    """Report a file that is not valid UTF-8 text."""

    def __init__(self, message: str = "The requested file is not valid UTF-8 text.") -> None:
        """Create a file encoding error.

        Args:
            message: Diagnostic description of the encoding failure.
        """
        super().__init__(ToolErrorCode.FILE_INVALID_ENCODING, message)


class ToolFileTooLargeError(ToolError):
    """Report a file that exceeds the configured byte limit."""

    def __init__(self, message: str = "The requested file exceeds the configured size limit.") -> None:
        """Create a file size error.

        Args:
            message: Diagnostic description of the size limit.
        """
        super().__init__(ToolErrorCode.FILE_TOO_LARGE, message)


class ToolSkillNotSelectedError(ToolError):
    """Report a Skill that is not active in the current Run."""

    def __init__(self, message: str = "The requested Skill is not active in this Run.") -> None:
        """Create an inactive Skill error.

        Args:
            message: Diagnostic description of the inactive Skill.
        """
        super().__init__(ToolErrorCode.SKILL_NOT_SELECTED, message)
