from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from pleiad.contracts import ContractModel, JsonObject
from pleiad.messages.message import Message
from pleiad.models.result import ModelUsage
from pleiad.sessions.checkpoint import RunCheckpoint
from pleiad.sessions.input import InputRecord
from pleiad.sessions.usage import RunUsage
from pleiad.tools.contracts import ToolAcceptance, ToolResult, ToolSubmission


class RunStatus(StrEnum):
    """Separate active, suspended, and terminal SDK execution facts."""

    RUNNING = "running"
    WAITING_EVENT = "waiting_event"
    PAUSED = "paused"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RunReceipt(ContractModel):
    """Persist the final outcome after all locally owned work is settled."""

    run_id: str = Field(min_length=1)
    input_ids: tuple[str, ...]
    state: Literal["succeeded", "failed", "cancelled"]
    reason: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    final_output: str = ""
    structured_output: JsonObject | None = None
    usage: RunUsage = RunUsage()
    cleanup: JsonObject = Field(default_factory=dict)
    final_sequence: int = Field(default=0, ge=0)
    completed_at: AwareDatetime


class ModelAttemptRecord(ContractModel):
    """Persist actual request authorization and its optionally reported completion."""

    attempt_id: str = Field(min_length=1, max_length=256)
    step_id: str = Field(min_length=1, max_length=256)
    provider: str = Field(min_length=1, max_length=256)
    model: str = Field(min_length=1, max_length=256)
    request_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    max_output_tokens: int = Field(gt=0)
    deadline: AwareDatetime
    usage_before: RunUsage
    admitted_at: AwareDatetime
    result_usage: ModelUsage | None = None
    response_id: str | None = None
    completed_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def validate_completion(self) -> "ModelAttemptRecord":
        """Keep reported completion separate from an attempt with unknown usage."""
        if (self.result_usage is None) != (self.completed_at is None):
            raise ValueError("Model completion requires both reported usage and its recorded time")
        if self.response_id is not None and self.completed_at is None:
            raise ValueError("A model response identity requires a completed attempt")
        if self.admitted_at >= self.deadline:
            raise ValueError("A model attempt must be admitted before its authorization expires")
        if self.completed_at is not None and self.completed_at < self.admitted_at:
            raise ValueError("A model completion cannot precede its admission")
        return self


class RunSnapshot(ContractModel):
    """Expose an accepted invocation and its currently committed facts."""

    session_id: str
    run_id: str
    invocation_id: str
    parameters_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    agent_id: str
    parent_run_id: str | None = Field(default=None, min_length=1, max_length=256)
    input_ids: tuple[str, ...]
    input_mode: Literal["batch", "live"]
    initial_input_ids: tuple[str, ...]
    state: RunStatus
    writer_epoch: int = Field(ge=1)
    revision: int = Field(ge=1)
    accepted_at: AwareDatetime
    usage: RunUsage = RunUsage()
    model_attempts: tuple[ModelAttemptRecord, ...] = ()
    checkpoint: RunCheckpoint | None = None
    receipt: RunReceipt | None = None


class SessionMessage(ContractModel):
    """Retain message order independently from step and Run order."""

    sequence: int = Field(ge=1)
    run_id: str
    step_id: str
    message: Message


class RunStart(ContractModel):
    """Distinguish a newly acquired writer from an accepted invocation retry."""

    snapshot: RunSnapshot
    duplicate: bool


class SessionPage(ContractModel):
    """Return bounded original history and pending input acceptance facts."""

    session_id: str
    messages: tuple[SessionMessage, ...]
    next_sequence: int = Field(ge=0)
    last_sequence: int = Field(ge=0)
    pending_inputs: tuple[InputRecord, ...]


class EventRecord(ContractModel):
    """Persist a replayable typed event payload with a stable Run sequence."""

    sequence: int = Field(ge=1)
    event_id: str
    run_id: str
    kind: str
    timestamp: datetime
    data: JsonObject


class ReconcileRecord(ContractModel):
    """Retain an idempotent local reconciliation result."""

    request_id: str
    run_id: str
    parameters_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_ids: tuple[str, ...] = ()
    snapshot: RunSnapshot
    result: JsonObject
    accepted_at: AwareDatetime


class ToolCallRecord(ContractModel):
    """Save a tool intent before external effects can start."""

    run_id: str
    step_id: str
    call_id: str
    tool_code: str
    binding_digest: str
    arguments_digest: str
    authorization_ref: str
    state: Literal["prepared", "running", "succeeded", "failed", "cancelled", "unknown"] = "prepared"
    result_ref: str | None = None
    operation_id: str | None = None
    result: ToolResult | None = None
    backend_evidence_ref: str | None = None


class OperationRecord(ContractModel):
    """Correlate a background backend receipt with its original tool call."""

    operation_id: str
    run_id: str
    call_id: str
    state: Literal["prepared", "running", "succeeded", "failed", "cancel_requested", "cancelled", "unknown"]
    submission: ToolSubmission | None = None
    backend_receipt: ToolAcceptance | None = None
    result_ref: str | None = None
    result: ToolResult | None = None
    ended_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def validate_terminal(self) -> "OperationRecord":
        """Require completion time for a confirmed terminal backend outcome."""
        if self.submission is not None and self.submission.operation_id != self.operation_id:
            raise ValueError("Backend submission must retain its operation identity")
        if self.backend_receipt is not None and (
            self.submission is None or self.submission.request_digest != self.backend_receipt.request_digest
        ):
            raise ValueError("Backend acceptance must match the prepared request")
        if self.backend_receipt is not None and self.backend_receipt.operation_id != self.operation_id:
            raise ValueError("Backend receipts must retain their operation identity")
        if self.state in {"running", "succeeded"} and self.backend_receipt is None:
            raise ValueError("Accepted operations require a concrete backend receipt")
        if self.state in {"succeeded", "failed", "cancelled"} and self.ended_at is None:
            raise ValueError("terminal operations require ended_at")
        return self
