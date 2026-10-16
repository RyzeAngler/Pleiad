from pleiad.errors.base import SystemFault


class ObservabilityConfigurationError(SystemFault):
    """Report invalid explicit observability configuration."""

    def __init__(self, message: str) -> None:
        """Create a configuration failure without exposing credentials."""
        super().__init__("observability.configuration.invalid", message)


class ObservabilityInitializationError(SystemFault):
    """Report failure to construct an explicitly requested provider."""

    def __init__(self, message: str) -> None:
        """Create an initialization failure without exposing provider payloads."""
        super().__init__("observability.initialization.failed", message)
