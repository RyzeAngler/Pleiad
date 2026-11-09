from pydantic import Field

from pleiad.contracts import ContractModel
from pleiad.tools.contracts import ToolResult


class ResumeDecision(ContractModel):
    """Bind a trusted host decision to one persisted pending action."""

    interruption_id: str = Field(min_length=1)
    result: ToolResult
