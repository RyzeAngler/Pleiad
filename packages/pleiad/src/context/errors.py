from pleiad.errors.base import BusinessError, SystemFault


class ContextWindowError(BusinessError):
    """Report a request that cannot fit while preserving required context."""

    def __init__(self, message: str) -> None:
        """Create a context-window business error.

        Args:
            message: Safe description of the content that could not fit.
        """
        super().__init__("context.window.exceeded", message)


class ContextCompactionError(SystemFault):
    """Report a failure while producing a request-local compacted history."""

    def __init__(self, message: str = "Context compaction failed.") -> None:
        """Create a context compaction system fault."""
        super().__init__("context.compaction.failed", message)
