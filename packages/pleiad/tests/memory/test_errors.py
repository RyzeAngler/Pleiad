from pleiad.errors.base import ErrorType
from pleiad.memory.errors import (
    MemoryDataCorruptedError,
    MemoryErrorCode,
    MemoryStorageError,
    MemoryTurnConflictError,
    MemoryTurnInvalidError,
)


def test_invalid_turn_is_a_business_error_with_memory_owned_code() -> None:
    """Invalid caller data is classified for recoverable Agent-loop handling."""
    error = MemoryTurnInvalidError("The submitted Turn is invalid.")

    assert error.code is MemoryErrorCode.TURN_INVALID
    assert error.message == "The submitted Turn is invalid."
    assert error.type is ErrorType.BUSINESS


def test_storage_conflict_and_corruption_are_system_errors() -> None:
    """Memory consistency and persistence failures terminate the current Run."""
    errors = (MemoryTurnConflictError(), MemoryDataCorruptedError(), MemoryStorageError())

    assert all(error.type is ErrorType.SYSTEM for error in errors)
    assert {error.code for error in errors} == {
        MemoryErrorCode.TURN_CONFLICT,
        MemoryErrorCode.DATA_CORRUPTED,
        MemoryErrorCode.STORAGE_FAILED,
    }
