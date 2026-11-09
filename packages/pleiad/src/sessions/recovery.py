from datetime import datetime
from typing import Literal

from pydantic import Field

from pleiad.contracts import ContractModel
from pleiad.sessions.records import RunSnapshot
from pleiad.tools.contracts import ToolAcceptance, ToolResult


class ActionRef(ContractModel):
    """Refer to an original persisted tool intent and optional operation."""

    run_id: str
    call_id: str
    operation_id: str | None = None


class ActionEvidence(ContractModel):
    """Carry trusted adapter observations for local factual reconciliation."""

    evidence_id: str = Field(min_length=1)
    action: ActionRef
    binding_digest: str
    arguments_digest: str
    backend_evidence_ref: str = Field(min_length=1)
    observed_at: datetime
    observed_state: Literal["running", "succeeded", "failed", "cancelled", "not_started", "unknown"]
    result_ref: str | None = None
    result: ToolResult | None = None
    acceptance: ToolAcceptance | None = None


class ReconcileResult(ContractModel):
    """Describe committed local recovery without replaying external actions."""

    disposition: Literal["terminal", "paused", "interrupted"]
    snapshot: RunSnapshot
    unsettled_actions: tuple[ActionRef, ...]
    duplicate: bool
