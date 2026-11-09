from pydantic import ConfigDict, Field

from pleiad.contracts import CapabilityGrants, ContractModel


class AgentPolicyHints(ContractModel):
    """Optional bounds requested by an Agent author.

    Hints are never an authorization grant. The host intersects them with the
    request and parent policy before a Run starts.
    """

    model_config = ConfigDict(strict=True)

    timeout_seconds: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    max_model_steps: int | None = Field(default=None, gt=0)
    max_output_tokens: int | None = Field(default=None, gt=0)
    grants: CapabilityGrants | None = None
