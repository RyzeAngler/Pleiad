from pleiad.errors.base import BusinessError, SystemFault


class RuntimeStateError(SystemFault):
    """Report a checkpoint or lifecycle state that Runtime cannot interpret."""

    def __init__(self, message: str = "The Runtime checkpoint is invalid.") -> None:
        """Create a Runtime state system fault."""
        super().__init__("runtime.state.invalid", message)


class HandoffConflictError(BusinessError):
    """Report a model Tool batch that mixes incompatible control actions."""

    def __init__(self, message: str = "The Tool batch contains an incompatible Handoff action.") -> None:
        """Create a Handoff conflict feedback error."""
        super().__init__("runtime.handoff.conflict", message)


class HandoffLimitError(BusinessError):
    """Report a Run that reached its configured Handoff limit."""

    def __init__(self, message: str = "The Run reached its Handoff limit.") -> None:
        """Create a Handoff limit feedback error."""
        super().__init__("runtime.handoff.limit_reached", message)
