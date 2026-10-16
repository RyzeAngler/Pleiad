from pleiad.errors.base import BusinessError, SystemFault


class GraphSystemFault(SystemFault):
    """Report an unexpected Graph scheduler or recovery failure."""

    def __init__(self, message: str = "The Graph runtime failed unexpectedly.") -> None:
        """Create a Graph system fault."""
        super().__init__("orchestration.graph.system_failure", message)


class InvalidGraphError(BusinessError):
    """Report a Graph definition or input contract failure."""

    def __init__(self, message: str = "The Graph definition or input is invalid.") -> None:
        """Create an invalid Graph business error."""
        super().__init__("orchestration.graph.invalid", message)


class GraphNodeError(BusinessError):
    """Report a business failure from one Graph Node."""

    def __init__(self, message: str = "A Graph Node failed or returned invalid output.") -> None:
        """Create a Node business error."""
        super().__init__("orchestration.graph.node_failed", message)


class GraphTimeoutError(BusinessError):
    """Report a Graph deadline reached before terminal state."""

    def __init__(self, message: str = "Graph execution exceeded its configured timeout.") -> None:
        """Create a Graph timeout error."""
        super().__init__("orchestration.graph.timed_out", message)


class GraphRevisionConflictError(BusinessError):
    """Report an expected revision mismatch."""

    def __init__(self, message: str = "The Graph revision no longer matches the expected revision.") -> None:
        """Create a revision conflict error."""
        super().__init__("orchestration.graph.revision_conflict", message)
