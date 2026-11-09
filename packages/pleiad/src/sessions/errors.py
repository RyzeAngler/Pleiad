from pleiad.errors.base import BusinessError, SystemFault


class SessionConflictError(BusinessError):
    """Reject an input, invocation, or lifecycle mutation that conflicts with facts."""

    def __init__(self, message: str) -> None:
        """Describe the conflicting contract without exposing stored content."""
        super().__init__("session.conflict", message)


class StaleWriterError(SystemFault):
    """Reject a mutation from an expired epoch or revision."""

    def __init__(self, message: str = "The Session writer epoch or Run revision is stale.") -> None:
        """Describe a fenced mutation."""
        super().__init__("session.writer.stale", message)


class SessionDataCorruptedError(SystemFault):
    """Report invalid persisted Session facts."""

    def __init__(self, message: str) -> None:
        """Describe a stored-state invariant violation."""
        super().__init__("session.data.corrupted", message)


class SessionStorageError(SystemFault):
    """Report a failed Session backend operation."""

    def __init__(self, message: str) -> None:
        """Describe a storage fault with the original exception retained as cause."""
        super().__init__("session.storage.failed", message)


class ModelStepInvalidError(BusinessError):
    """Report messages that violate the ModelStep protocol."""

    def __init__(self, message: str) -> None:
        """Describe the invalid ordering or incomplete pairing."""
        super().__init__("session.step.invalid", message)


class ModelStepConflictError(SystemFault):
    """Reject conflicting changes to a model step."""

    def __init__(self, message: str) -> None:
        """Describe the conflicting mutation."""
        super().__init__("session.step.conflict", message)
