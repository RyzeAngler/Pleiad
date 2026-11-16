from datetime import UTC, datetime
from typing import Literal

from pydantic import Field

from pleiad.contracts import ContractModel
from pleiad.runtime.limits import RunLimits
from pleiad.sessions.input import InputEnvelope
from pleiad.tools.contracts import ToolContext


class CollaborationLimits(ContractModel):
    """Bound resource admission independently from individual Run allowances."""

    max_active_runs: int = Field(default=4, ge=1, le=128)
    max_queued_work: int = Field(default=32, ge=0, le=4096)
    max_members: int = Field(default=32, ge=1, le=4096)
    max_depth: int = Field(default=4, ge=1, le=32)
    max_pending_messages: int = Field(default=128, ge=1)
    max_message_bytes: int = Field(default=65_536, ge=1)
    max_event_page_size: int = Field(default=100, ge=1, le=1000)


class SpawnRequest(ContractModel):
    """Request a new member from a host-authorized profile."""

    request_id: str = Field(min_length=1, max_length=256)
    profile_id: str = Field(min_length=1, max_length=256)
    title: str = Field(min_length=1, max_length=256)
    content: InputEnvelope


class MessageRequest(ContractModel):
    """Send bounded input to an existing member without selecting caller identity."""

    request_id: str = Field(min_length=1, max_length=256)
    member_id: str = Field(min_length=1)
    content: InputEnvelope
    expected_run_id: str | None = None


class InterruptRequest(ContractModel):
    """Stop only the exact work and Run observed by the caller."""

    request_id: str = Field(min_length=1, max_length=256)
    member_id: str = Field(min_length=1)
    expected_work_id: str = Field(min_length=1)
    expected_run_id: str = Field(min_length=1)


class CloseRequest(ContractModel):
    """Close one member and its descendants without deleting their history."""

    request_id: str = Field(min_length=1, max_length=256)
    member_id: str = Field(min_length=1)


class WorkPermit(ContractModel):
    """Persist non-secret host authorization and immutable binding identity."""

    authorization_ref: str = Field(min_length=1)
    binding_key: str = Field(min_length=1)
    limits: RunLimits


class WorkProposal(ContractModel):
    """Identify an idempotent host admission before any child invocation."""

    scope_id: str
    work_id: str
    member_id: str
    session_id: str
    actor_id: str
    profile_id: str
    content: InputEnvelope
    authority: ToolContext


class WorkRejection(ContractModel):
    """Report a normal host policy rejection without allocating runtime resources."""

    code: str = Field(min_length=1)
    message: str = Field(min_length=1)


class CommandReceipt(ContractModel):
    """Retain the immutable route chosen when a command was accepted."""

    request_id: str
    accepted: bool
    route: Literal["new_work", "active_run", "message", "interrupt", "close", "rejected"]
    member_id: str
    work_id: str | None = None
    run_id: str | None = None
    message_id: str | None = None
    code: str | None = None


class EventCursor(ContractModel):
    """Identify an observer's exclusive position within one collaboration scope."""

    scope_id: str
    sequence: int = Field(default=0, ge=0)


class CollaborationEvent(ContractModel):
    """Expose ordered collaboration facts without private member transcripts."""

    sequence: int = Field(ge=1)
    event_id: str
    scope_id: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    kind: str
    member_id: str
    work_id: str | None = None
    run_id: str | None = None
    message_id: str | None = None
    source_sequence: int | None = None
    step_id: str | None = None
    actor_id: str | None = None


class MessageStatus(ContractModel):
    """Separate durable acceptance, delivery, and model-step consumption."""

    message_id: str
    member_id: str
    state: Literal["received", "delivered", "consumed", "retained", "revoked"]
    run_id: str | None = None
    step_id: str | None = None


class EventPage(ContractModel):
    """Return a bounded, replayable wait-any observation."""

    events: tuple[CollaborationEvent, ...]
    cursor: EventCursor
    timed_out: bool = False
