from pleiad.errors.base import BusinessError, SystemFault


class CollaborationConflict(BusinessError):
    """Reject a stale identity, conflicting retry, or unauthorized operation."""

    def __init__(self, message: str) -> None:
        """Create an actionable model-visible command rejection."""
        super().__init__("collaboration.conflict", message)


class CollaborationStorageError(SystemFault):
    """Fail closed when durable collaboration ownership cannot be established."""

    def __init__(self, message: str) -> None:
        """Retain a stable storage fault code."""
        super().__init__("collaboration.storage", message)
