from pleiad.errors.base import BusinessError, SystemFault


class OrchestrationConfigurationError(SystemFault):
    """Report an invalid SDK orchestration binding."""

    def __init__(self, message: str) -> None:
        """Create a configuration fault with a stable component code."""
        super().__init__("orchestration.configuration.invalid", message)


class WorkerNotFoundError(BusinessError):
    """Report a model-selected worker that is not on the host allowlist."""

    def __init__(self, worker_id: str) -> None:
        """Create a worker selection failure without exposing bindings."""
        super().__init__("orchestration.worker.not_found", f"The Worker is not available: {worker_id}.")


class DelegationNotFoundError(BusinessError):
    """Report an unknown delegation query."""

    def __init__(self, delegation_id: str) -> None:
        """Create a delegation lookup failure."""
        super().__init__("orchestration.delegation.not_found", f"The delegation is not available: {delegation_id}.")


class WorkerPolicyError(SystemFault):
    """Report parent or Worker policy that cannot authorize a child Run."""

    def __init__(self, message: str) -> None:
        """Create a Worker policy fault with a stable code."""
        super().__init__("orchestration.worker.policy_invalid", message)


class OrchestrationStateError(SystemFault):
    """Report damaged state required to resume an orchestration action."""

    def __init__(self, message: str) -> None:
        """Create an orchestration recovery fault with a stable code."""
        super().__init__("orchestration.state.invalid", message)


class WorkerBudgetError(BusinessError):
    """Report a parent budget that cannot accept another Worker delegation."""

    def __init__(self, message: str = "The parent Run has no remaining Worker budget.") -> None:
        """Create a Worker budget exhaustion error."""
        super().__init__("orchestration.worker.budget_exhausted", message)
