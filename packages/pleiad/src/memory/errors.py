from enum import StrEnum

from pleiad.errors.base import BusinessError, SystemFault


class MemoryErrorCode(StrEnum):
    """Stable error codes owned by the ShortMemory component."""

    TURN_INVALID = "memory.turn.invalid"
    TURN_CONFLICT = "memory.turn.conflict"
    DATA_CORRUPTED = "memory.data.corrupted"
    STORAGE_FAILED = "memory.storage.failed"


class MemoryTurnInvalidError(BusinessError):
    """Report a message batch or Turn that violates the memory contract."""

    def __init__(self, message: str = "The Turn or Message batch is invalid.") -> None:
        """Create a business error for invalid Turn data.

        Args:
            message: Diagnostic description of the invalid data.
        """
        super().__init__(MemoryErrorCode.TURN_INVALID, message)


class MemoryTurnConflictError(SystemFault):
    """Report a concurrent or conflicting mutation of a Turn."""

    def __init__(self, message: str = "The requested Turn mutation conflicts with stored state.") -> None:
        """Create a system fault for a Turn mutation conflict.

        Args:
            message: Diagnostic description of the conflict.
        """
        super().__init__(MemoryErrorCode.TURN_CONFLICT, message)


class MemoryDataCorruptedError(SystemFault):
    """Report persisted or in-memory state that violates ShortMemory invariants."""

    def __init__(self, message: str = "ShortMemory data is corrupted.") -> None:
        """Create a system fault for invalid stored memory data.

        Args:
            message: Diagnostic description of the corruption.
        """
        super().__init__(MemoryErrorCode.DATA_CORRUPTED, message)


class MemoryStorageError(SystemFault):
    """Report a storage operation that could not complete."""

    def __init__(self, message: str = "ShortMemory storage failed.") -> None:
        """Create a system fault for a storage failure.

        Args:
            message: Diagnostic description of the storage failure.
        """
        super().__init__(MemoryErrorCode.STORAGE_FAILED, message)
