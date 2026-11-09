from pleiad.errors.base import BusinessError, SystemFault


class OrchestrationConfigurationError(SystemFault):
    """Report an invalid SDK orchestration binding."""

    def __init__(self, message: str) -> None:
        """Create a configuration fault with a stable component code."""
        super().__init__("orchestration.configuration.invalid", message)


class SubagentNotFoundError(BusinessError):
    """Report a model-selected subagent that is not on the host allowlist."""

    def __init__(self, subagent_id: str) -> None:
        """Create a subagent selection failure without exposing bindings."""
        super().__init__("orchestration.subagent.not_found", f"The Subagent is not available: {subagent_id}.")


class DelegationNotFoundError(BusinessError):
    """Report an unknown delegation query."""

    def __init__(self, delegation_id: str) -> None:
        """Create a delegation lookup failure."""
        super().__init__("orchestration.delegation.not_found", f"The delegation is not available: {delegation_id}.")


class SubagentPolicyError(SystemFault):
    """Report parent or Subagent policy that cannot authorize a child Run."""

    def __init__(self, message: str) -> None:
        """Create a Subagent policy fault with a stable code."""
        super().__init__("orchestration.subagent.policy_invalid", message)


class OrchestrationStateError(SystemFault):
    """Report damaged state required to resume an orchestration action."""

    def __init__(self, message: str) -> None:
        """Create an orchestration recovery fault with a stable code."""
        super().__init__("orchestration.state.invalid", message)


class SubagentBudgetError(BusinessError):
    """Report a parent budget that cannot accept another Subagent delegation."""

    def __init__(self, message: str = "The parent Run has no remaining Subagent budget.") -> None:
        """Create a Subagent budget exhaustion error."""
        super().__init__("orchestration.subagent.budget_exhausted", message)
