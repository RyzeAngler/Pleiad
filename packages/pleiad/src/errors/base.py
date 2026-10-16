import re
from enum import StrEnum


class ErrorType(StrEnum):
    """Classify whether an SDK error can remain in the Agent loop."""

    BUSINESS = "business"
    SYSTEM = "system"


class PleiadError(Exception):
    """Constrain the fields exposed by every classified SDK exception."""

    def __init__(self, code: str, message: str, error_type: ErrorType) -> None:
        """Create a classified SDK exception.

        Args:
            code: Stable component-owned error identifier.
            message: Non-empty diagnostic description.
            error_type: Control-flow classification for the failure.

        Raises:
            ValueError: If the code or message does not satisfy the contract.
        """
        if re.fullmatch(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+", code) is None:
            raise ValueError("error code must contain lowercase dotted identifiers")
        if not message.strip():
            raise ValueError("error message must not be empty")
        self.code = code
        self.message = message
        self.type = error_type
        super().__init__(message)


class BusinessError(PleiadError):
    """Represent an expected failure that can be returned to the Agent loop."""

    def __init__(self, code: str, message: str) -> None:
        """Create a business error.

        Args:
            code: Stable component-owned error identifier.
            message: Non-empty diagnostic description.
        """
        super().__init__(code, message, ErrorType.BUSINESS)


class SystemFault(PleiadError):
    """Represent a system failure that terminates the current Run."""

    def __init__(self, code: str, message: str) -> None:
        """Create a system fault.

        Args:
            code: Stable component-owned error identifier.
            message: Non-empty diagnostic description.
        """
        super().__init__(code, message, ErrorType.SYSTEM)
