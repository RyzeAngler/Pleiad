from pleiad.errors.base import BusinessError, SystemFault


class AgentConfigurationError(SystemFault):
    """Report an invalid Agent definition or unavailable capability binding."""

    def __init__(self, message: str = "The Agent configuration is invalid.") -> None:
        """Create an Agent configuration fault."""
        super().__init__("agent.config.invalid", message)


class InvalidAgentOutputError(BusinessError):
    """Report a final model output that violates the Agent contract."""

    def __init__(self, message: str = "The model output does not satisfy the Agent result contract.") -> None:
        """Create an invalid output error.

        Args:
            message: Diagnostic description of the invalid output.
        """
        super().__init__("agent.output.invalid", message)
