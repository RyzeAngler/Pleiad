from pydantic import AwareDatetime, ConfigDict, Field

from pleiad.context.budget import ContextBudget
from pleiad.contracts import CapabilityGrants, ContractModel


class RunLimits(ContractModel):
    """Request-local limits for one semantic Agent Run."""

    model_config = ConfigDict(strict=True)

    execution_timeout_seconds: float = Field(default=600.0, gt=0, allow_inf_nan=False)
    deadline: AwareDatetime | None = None
    max_model_steps: int = Field(default=32, gt=0)
    max_output_tokens: int = Field(default=8_192, gt=0)
    tool_concurrency: int = Field(default=8, gt=0)
    max_handoffs: int = Field(default=8, gt=0)
    context_budget: ContextBudget = ContextBudget(
        window_tokens=128_000,
        output_reserve=8_192,
        compact_at=96_000,
        recent_tokens=32_000,
    )
    grants: CapabilityGrants | None = None
