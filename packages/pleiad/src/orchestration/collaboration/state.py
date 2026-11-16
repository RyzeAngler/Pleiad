from typing import Literal

from pydantic import Field, model_validator

from pleiad.budgets import LedgerSnapshot
from pleiad.contracts import ContractModel
from pleiad.orchestration.collaboration.contracts import (
    CollaborationEvent,
    CollaborationLimits,
    CommandReceipt,
    WorkPermit,
    WorkProposal,
)
from pleiad.runtime.checkpoint import ResumeDecision
from pleiad.sessions.input import InputEnvelope
from pleiad.sessions.records import RunReceipt

type WorkPhase = Literal["prepared", "queued", "dispatching", "running", "paused", "stopping", "terminal"]


class MemberRecord(ContractModel):
    """Persist stable membership independently from its current invocation."""

    member_id: str
    session_id: str
    profile_id: str
    title: str
    parent_member_id: str | None = None
    depth: int = Field(ge=0)
    state: Literal["open", "closing", "closed"] = "open"


class WorkRecord(ContractModel):
    """Keep one invocation's ownership, authorization, and outcome separate."""

    proposal: WorkProposal
    phase: WorkPhase = "prepared"
    permit: WorkPermit | None = None
    fingerprint: str | None = None
    run_id: str | None = None
    input_ids: tuple[str, ...] = ()
    gate_open: bool = False
    resolution: Literal["clear", "unknown"] = "clear"
    receipt: RunReceipt | None = None
    reason: str | None = None


class MessageRecord(ContractModel):
    """Track acceptance and outbox delivery; Session owns actual step consumption."""

    message_id: str
    actor_id: str
    member_id: str
    envelope: InputEnvelope
    work_id: str | None = None
    target_run_id: str | None = None
    explicit: bool = False
    delivered: bool = False
    revoked: bool = False
    consumed_run_id: str | None = None
    consumed_step_id: str | None = None


class CommandRecord(ContractModel):
    """Keep a command digest and its immutable response across retries."""

    actor_id: str
    request_id: str
    digest: str
    receipt: CommandReceipt | None = None


class CollaborationState(ContractModel):
    """Validate an atomic collaboration journal, including its transactional outbox."""

    scope_id: str
    root_member_id: str
    owner_epoch: int = Field(default=1, ge=1)
    revision: int = Field(default=0, ge=0)
    state: Literal["open", "closing", "closed", "faulted"] = "open"
    limits: CollaborationLimits
    members: dict[str, MemberRecord] = Field(default_factory=dict)
    works: dict[str, WorkRecord] = Field(default_factory=dict)
    commands: dict[str, CommandRecord] = Field(default_factory=dict)
    messages: dict[str, MessageRecord] = Field(default_factory=dict)
    events: tuple[CollaborationEvent, ...] = ()
    ledger: LedgerSnapshot = LedgerSnapshot()
    run_cursors: dict[str, int] = Field(default_factory=dict)
    resumptions: dict[str, tuple[ResumeDecision, ...]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_relations(self) -> "CollaborationState":
        """Reject dangling ownership and multiple writers in a persisted scope."""
        if self.root_member_id not in self.members:
            raise ValueError("A scope requires its root member")
        root = self.members[self.root_member_id]
        if root.depth != 0 or root.parent_member_id is not None:
            raise ValueError("The root must have depth zero and no parent")
        writers: set[str] = set()
        for identity, member in self.members.items():
            if identity != member.member_id:
                raise ValueError("Member keys must match their identity")
            if identity != self.root_member_id and member.parent_member_id is None:
                raise ValueError("Non-root members require a parent")
            if member.parent_member_id is not None:
                parent = self.members.get(member.parent_member_id)
                if parent is None or parent.depth + 1 != member.depth:
                    raise ValueError("Members require an acyclic parent with the preceding depth")
        if len({value.session_id for value in self.members.values()}) != len(self.members):
            raise ValueError("Members must have independent Sessions")
        for identity, work in self.works.items():
            if identity != work.proposal.work_id or work.proposal.member_id not in self.members:
                raise ValueError("Works must belong to their recorded member")
            if (
                work.proposal.scope_id != self.scope_id
                or work.proposal.session_id != self.members[work.proposal.member_id].session_id
                or work.proposal.actor_id not in self.members
                or work.proposal.authority.run_id is None
                or work.proposal.authority.parent_limits is None
            ):
                raise ValueError("Work scope, Session, and parent authority must remain valid")
            if work.phase not in {"prepared", "terminal"} and work.permit is None:
                raise ValueError("Admitted work requires a host permit")
            if work.phase in {"dispatching", "running", "paused", "stopping"}:
                if work.proposal.member_id in writers:
                    raise ValueError("A member can have only one active or paused writer")
                writers.add(work.proposal.member_id)
            if work.gate_open and (work.phase != "running" or work.run_id is None):
                raise ValueError("Input admission requires a running writer")
            if work.phase in {"running", "paused"} and work.run_id is None:
                raise ValueError("Running and paused work require their accepted Run identity")
            if work.receipt is not None and work.receipt.run_id != work.run_id:
                raise ValueError("Work receipt must retain its original Run identity")
        if tuple(value.sequence for value in self.events) != tuple(range(1, len(self.events) + 1)):
            raise ValueError("Collaboration event sequences must be continuous")
        if any(value.scope_id != self.scope_id for value in self.events):
            raise ValueError("Events cannot escape their scope")
        if len({value.event_id for value in self.events}) != len(self.events):
            raise ValueError("Event identities must be unique")
        for identity, message in self.messages.items():
            if identity != message.message_id or identity != message.envelope.input_id:
                raise ValueError("Message and Session input identities must match")
            if message.member_id not in self.members or message.actor_id not in self.members:
                raise ValueError("Messages must remain in their original scope")
            if message.work_id is not None and message.work_id not in self.works:
                raise ValueError("Work inputs require a persisted owning work")
            if message.work_id is not None and self.works[message.work_id].proposal.member_id != message.member_id:
                raise ValueError("Work input delivery must retain its target member")
            if (message.consumed_run_id is None) != (message.consumed_step_id is None):
                raise ValueError("Consumed messages require both their Run and model step")
        return self


def transition(work: WorkRecord, phase: WorkPhase) -> WorkRecord:
    """Validate every ordinary work transition at its owning state machine."""
    allowed: dict[WorkPhase, set[WorkPhase]] = {
        "prepared": {"queued", "terminal"},
        "queued": {"dispatching", "terminal"},
        "dispatching": {"queued", "running", "paused", "stopping", "terminal"},
        "running": {"paused", "stopping", "terminal"},
        "paused": {"dispatching", "stopping", "terminal"},
        "stopping": {"paused", "terminal"},
        "terminal": set(),
    }
    if phase != work.phase and phase not in allowed[work.phase]:
        raise ValueError(f"Invalid collaboration transition: {work.phase} -> {phase}")
    return work.model_copy(update={"phase": phase, "gate_open": work.gate_open if phase == "running" else False})
