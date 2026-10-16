from enum import StrEnum

from pleiad.errors.base import BusinessError


class CapabilityErrorCode(StrEnum):
    """Stable error codes owned by the capability component."""

    NOT_FOUND = "capability.not_found"
    DIGEST_MISMATCH = "capability.digest.mismatch"
    UNAVAILABLE = "capability.unavailable"


class CapabilityError(BusinessError):
    """Base class for expected capability failures."""


class CapabilityNotFoundError(CapabilityError):
    """Report a requested capability that does not exist."""

    def __init__(self, message: str = "The requested capability does not exist.") -> None:
        """Create a missing capability error.

        Args:
            message: Diagnostic description of the missing capability.
        """
        super().__init__(CapabilityErrorCode.NOT_FOUND, message)


class CapabilityDigestMismatchError(CapabilityError):
    """Report a capability snapshot that changed before resume."""

    def __init__(self, message: str = "The capability snapshot changed before the Run resumed.") -> None:
        """Create a capability digest mismatch error.

        Args:
            message: Diagnostic description of the mismatch.
        """
        super().__init__(CapabilityErrorCode.DIGEST_MISMATCH, message)


class CapabilityUnavailableError(CapabilityError):
    """Report a known capability that cannot be used in the current Run."""

    def __init__(self, message: str = "The requested capability is unavailable for this Run.") -> None:
        """Create an unavailable capability error.

        Args:
            message: Diagnostic description of the unavailable capability.
        """
        super().__init__(CapabilityErrorCode.UNAVAILABLE, message)
