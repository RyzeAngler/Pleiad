from __future__ import annotations

import asyncio
import hashlib
import json
from uuid import NAMESPACE_URL, uuid5

from pleiad.budgets import BudgetLedger
from pleiad.contracts import JsonObject, UsageCharge
from pleiad.errors.base import PleiadError
from pleiad.limits import expired
from pleiad.messages.results import ResultReference
from pleiad.models.errors import (
    ModelAuthenticationError,
    ModelConfigurationError,
    ModelConnectionError,
    ModelPermissionError,
    ModelProviderOverloadedError,
    ModelProviderUnavailableError,
    ModelRateLimitError,
    ModelRequestError,
    ModelResponseError,
    ModelTimeoutError,
    ModelUnsupportedError,
)
from pleiad.orchestration.budget import child_policy
from pleiad.orchestration.collaboration.binding import CollaborationHost, WorkBinding
from pleiad.orchestration.collaboration.contracts import (
    CollaborationEvent,
    CollaborationLimits,
    CommandReceipt,
    MessageRequest,
    SpawnRequest,
    WorkPermit,
    WorkProposal,
    WorkRejection,
)
from pleiad.orchestration.collaboration.endpoint import MemberEndpoint
from pleiad.orchestration.collaboration.errors import CollaborationConflict, CollaborationStorageError
from pleiad.orchestration.collaboration.state import (
    CollaborationState,
    CommandRecord,
    MemberRecord,
    MessageRecord,
    WorkRecord,
    transition,
)
from pleiad.orchestration.collaboration.store import CollaborationStore
from pleiad.runtime.checkpoint import ResumeDecision
from pleiad.runtime.handle import RunHandle
from pleiad.runtime.policy import effective_policy
from pleiad.runtime.result import RunOutcome, RunResult, RunSuspension
from pleiad.runtime.runner import Runner
from pleiad.sessions.input import InputEnvelope
from pleiad.sessions.records import RunReceipt, RunStatus
from pleiad.sessions.recovery import ActionEvidence
from pleiad.sessions.session import Session
from pleiad.sessions.store import SessionStore, complete_owned
from pleiad.tools.contracts import ToolContext


def stable_id(*parts: str) -> str:
    """Derive a bounded identity from unambiguous command ownership."""
    return uuid5(NAMESPACE_URL, json.dumps(parts, ensure_ascii=False)).hex


class CollaborationGroup:
    """Own persistent members and bounded child work across root invocations.

    Use as an asynchronous context manager. Session stores, Runners, model clients,
    and the shared budget ledger are borrowed from the host. One deployment must
    exclusively own a scope; recovery fencing does not stop external side effects.
    Set close_on_exit=False for a deployment lifetime whose members must survive
    normal shutdown. Explicit close still permanently closes that scope.
    """

    def __init__(
        self,
        scope_id: str,
        *,
        root_session: Session,
        sessions: SessionStore,
        store: CollaborationStore,
        host: CollaborationHost,
        budgets: BudgetLedger,
        limits: CollaborationLimits | None = None,
        close_on_exit: bool = True,
    ) -> None:
        """Bind concrete persistence, authorization, and resource owners.

        Args:
            scope_id: Stable collaboration identity across host deployments.
            root_session: Borrowed persistent Session for the root member.
            sessions: Shared Session store for independent child Sessions.
            store: Dedicated collaboration journal store.
            host: Current admission and binding authorization callbacks.
            budgets: Shared ledger used by every composition mode in this scope.
            limits: Bounded membership, queue, message, and concurrency policy.
            close_on_exit: Whether context exit permanently closes members.
                False still cancels and settles owned work, but retains member
                identities for explicit recovery by a later exclusive owner.
        """
        if not scope_id.strip() or len(scope_id) > 256:
            raise ValueError("scope_id must contain between 1 and 256 characters")
        root_id = stable_id(scope_id, "root")
        self.root_session = root_session
        self.sessions = sessions
        self.store = store
        self.host = host
        self.budgets = budgets
        self.state = CollaborationState(
            scope_id=scope_id,
            root_member_id=root_id,
            limits=CollaborationLimits() if limits is None else limits,
            members={
                root_id: MemberRecord(
                    member_id=root_id, session_id=root_session.session_id, profile_id="root", title="Root", depth=0
                )
            },
        )
        self.lock = asyncio.Lock()
        self.changed = asyncio.Condition(self.lock)
        self.endpoints: dict[str, MemberEndpoint] = {}
        self.handles: dict[str, RunHandle] = {}
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self.task_group: asyncio.TaskGroup | None = None
        self.root_runs: set[str] = set()
        self.blocked_runs: set[str] = set()
        self.entered = False
        self.accepting = False
        self.recovered = False
        self.close_on_exit = close_on_exit

    @property
    def root(self) -> MemberEndpoint:
        """Return the trusted root endpoint for host Tool binding."""
        return self.endpoint(self.state.root_member_id)

    def endpoint(self, member_id: str) -> MemberEndpoint:
        """Bind caller identity outside model-controlled arguments."""
        if member_id not in self.state.members:
            raise CollaborationConflict("The member does not belong to this scope.")
        if member_id not in self.endpoints:
            self.endpoints[member_id] = MemberEndpoint(self, member_id)
        return self.endpoints[member_id]

    def session(self, member_id: str) -> Session:
        """Return the member's stable Session without claiming its writer."""
        if member_id == self.state.root_member_id:
            return self.root_session
        return Session(self.state.members[member_id].session_id, self.sessions)

    async def __aenter__(self) -> CollaborationGroup:
        """Open a new scope, or enter an explicitly reconciled recovered scope."""
        if self.entered:
            raise CollaborationConflict("The group already has an execution owner.")
        async with self.lock:
            if not self.recovered:
                if await self.store.read(self.state.scope_id) is not None:
                    raise CollaborationConflict("Existing scopes require explicit exclusive-owner recovery.")
                await self.commit()
            if self.state.state != "open":
                raise CollaborationConflict("A closed or faulted scope cannot admit new work.")
            self.task_group = asyncio.TaskGroup()
            await self.task_group.__aenter__()
            self.entered = True
            self.accepting = True
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        """Settle deployment work and optionally close persistent membership."""
        try:
            await complete_owned(self.shutdown_owned(close_members=self.close_on_exit))
        except BaseException as error:
            if self.task_group is not None:
                await self.task_group.__aexit__(type(error), error, error.__traceback__)
            raise
        else:
            if self.task_group is not None:
                await self.task_group.__aexit__(None, None, None)
        finally:
            self.entered = False
            self.accepting = False
            self.recovered = False
            self.root_runs.clear()
            self.blocked_runs.clear()
            self.task_group = None

    async def commit(self) -> None:
        """Persist all changed facts and wake observers after the durable commit."""
        await complete_owned(self.commit_owned())

    async def commit_owned(self) -> None:
        """Finish the durable write and local revision update as one owned operation."""
        proposed = self.state.model_copy(update={"revision": self.state.revision + 1, "ledger": self.budgets.export()})
        try:
            await self.store.write(proposed)
        except CollaborationStorageError:
            self.state = self.state.model_copy(update={"state": "faulted"})
            self.changed.notify_all()
            raise
        self.state = proposed
        self.changed.notify_all()

    def event(self, kind: str, member_id: str, *, work_id: str | None = None, message_id: str | None = None) -> None:
        """Append an event to the pending atomic journal revision."""
        work = None if work_id is None else self.state.works[work_id]
        message = None if message_id is None else self.state.messages[message_id]
        value = CollaborationEvent(
            sequence=len(self.state.events) + 1,
            event_id=stable_id(self.state.scope_id, "event", str(len(self.state.events) + 1)),
            scope_id=self.state.scope_id,
            kind=kind,
            member_id=member_id,
            work_id=work_id,
            run_id=None if work is None else work.run_id,
            message_id=message_id,
            actor_id=(None if work is None else work.proposal.actor_id) if message is None else message.actor_id,
        )
        self.state = self.state.model_copy(update={"events": (*self.state.events, value)})

    def put_work(self, value: WorkRecord) -> None:
        """Replace a work fact without mutating an earlier persisted snapshot."""
        self.state = self.state.model_copy(update={"works": {**self.state.works, value.proposal.work_id: value}})
        if value.phase == "terminal":
            self.state = self.state.model_copy(
                update={
                    "resumptions": {
                        key: decisions
                        for key, decisions in self.state.resumptions.items()
                        if key != value.proposal.work_id
                    },
                }
            )

    def put_message(self, value: MessageRecord) -> None:
        """Replace an outbox fact while retaining its original input identity."""
        self.state = self.state.model_copy(update={"messages": {**self.state.messages, value.message_id: value}})

    def member(self, actor_id: str, member_id: str, *, control: bool = False) -> MemberRecord:
        """Check scope membership and descendant control authority."""
        actor = self.state.members.get(actor_id)
        target = self.state.members.get(member_id)
        if actor is None or target is None:
            raise CollaborationConflict("The caller or target is unavailable in this scope.")
        if control and (member_id == self.state.root_member_id or not self.descendant(actor_id, member_id)):
            raise CollaborationConflict("Control operations require a strict descendant member.")
        return target

    def descendant(self, actor_id: str, target_id: str) -> bool:
        """Test ancestry using persisted acyclic membership facts."""
        current = self.state.members[target_id].parent_member_id
        while current is not None:
            if current == actor_id:
                return True
            current = self.state.members[current].parent_member_id
        return False

    def authorize(self, actor_id: str, authority: ToolContext) -> str:
        """Require a live trusted caller and bounded inherited Run authorization."""
        if not self.entered or not self.accepting or self.state.state != "open":
            raise CollaborationConflict("The collaboration scope is not accepting commands.")
        run_id = authority.run_id
        if run_id is None or authority.parent_limits is None:
            raise CollaborationConflict("Commands require an owning Run and its remaining limits.")
        if run_id in self.blocked_runs or expired(authority.parent_limits.deadline):
            raise CollaborationConflict("The owning Run is closed, paused, or expired.")
        if self.member(actor_id, actor_id).state != "open":
            raise CollaborationConflict("The caller is closed.")
        if actor_id == self.state.root_member_id:
            if run_id not in self.root_runs:
                raise CollaborationConflict("The root Run has not opened its bound collaboration scope.")
        elif not any(
            work.proposal.member_id == actor_id and work.run_id == run_id and work.phase == "running"
            for work in self.state.works.values()
        ):
            raise CollaborationConflict("The caller does not own the indicated member Run.")
        return run_id

    def command(self, actor_id: str, request_id: str, operation: str, payload: JsonObject) -> tuple[str, CommandRecord]:
        """Deduplicate by actor, command key, operation, and normalized payload."""
        key = stable_id(actor_id, request_id)
        digest = hashlib.sha256(
            json.dumps([operation, payload], sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
        ).hexdigest()
        current = self.state.commands.get(key)
        if current is not None:
            if current.digest != digest:
                raise CollaborationConflict("The request ID already has a different command payload.")
            return key, current
        return key, CommandRecord(actor_id=actor_id, request_id=request_id, digest=digest)

    async def acknowledge(self, key: str, record: CommandRecord, receipt: CommandReceipt) -> CommandReceipt:
        """Commit an immutable response together with all admission facts."""
        self.state = self.state.model_copy(
            update={"commands": {**self.state.commands, key: record.model_copy(update={"receipt": receipt})}}
        )
        await self.commit()
        return receipt

    def active_work(self, member_id: str) -> WorkRecord | None:
        """Return the single active or suspended writer for a member."""
        return next(
            (
                value
                for value in self.state.works.values()
                if value.proposal.member_id == member_id
                and value.phase in {"dispatching", "running", "paused", "stopping"}
            ),
            None,
        )

    def capacity(self, actor_id: str, member_id: str) -> str | None:
        """Bound admission and reject nested waits that could starve their ancestors."""
        active = sum(value.phase in {"dispatching", "running", "stopping"} for value in self.state.works.values())
        queued = sum(value.phase == "queued" for value in self.state.works.values())
        busy = self.active_work(member_id) is not None
        if actor_id != self.state.root_member_id and (busy or active >= self.state.limits.max_active_runs or queued):
            return "nested_capacity_exhausted"
        if (busy or active >= self.state.limits.max_active_runs) and queued >= self.state.limits.max_queued_work:
            return "queue_full"
        return None

    async def submit(
        self, actor_id: str, request: SpawnRequest | MessageRequest, authority: ToolContext
    ) -> CommandReceipt:
        """Atomically route a spawn or followup, preserving the accepted identity."""
        async with self.lock:
            operation = "spawn" if isinstance(request, SpawnRequest) else "followup"
            key, command = self.command(actor_id, request.request_id, operation, request.model_dump(mode="json"))
            if command.receipt is not None:
                return command.receipt
            owner = self.authorize(actor_id, authority)
            content_size = len(request.content.model_dump_json().encode())
            if content_size > self.state.limits.max_message_bytes:
                raise CollaborationConflict("The message exceeds the configured byte limit.")
            if isinstance(request, SpawnRequest):
                parent = self.state.members[actor_id]
                member_id = stable_id(self.state.scope_id, actor_id, request.request_id, "member")
                if len(self.state.members) - 1 >= self.state.limits.max_members:
                    return await self.reject(key, command, member_id, "member_limit")
                if parent.depth >= self.state.limits.max_depth:
                    return await self.reject(key, command, member_id, "depth_limit")
                rejection = self.capacity(actor_id, member_id)
                if rejection is not None:
                    return await self.reject(key, command, member_id, rejection)
                member = MemberRecord(
                    member_id=member_id,
                    session_id=f"collaboration:{member_id}",
                    profile_id=request.profile_id,
                    title=request.title,
                    parent_member_id=actor_id,
                    depth=parent.depth + 1,
                )
                self.state = self.state.model_copy(update={"members": {**self.state.members, member_id: member}})
            else:
                member_id = request.member_id
                member = self.member(actor_id, member_id, control=True)
                if member.state != "open":
                    return await self.reject(key, command, member_id, "member_closed")
                if any(
                    value.proposal.member_id == member_id and value.resolution == "unknown"
                    for value in self.state.works.values()
                ):
                    return await self.reject(key, command, member_id, "reconciliation_required")
                active = self.active_work(member_id)
                if active is not None and active.phase == "paused":
                    return await self.reject(key, command, member_id, "run_paused")
                if request.expected_run_id is not None and (
                    active is None or active.run_id != request.expected_run_id or not active.gate_open
                ):
                    return await self.reject(key, command, member_id, "run_mismatch")
                if active is not None and active.gate_open:
                    if active.permit is None or active.permit.limits.grants is None:
                        raise CollaborationStorageError("An active work is missing its effective authorization.")
                    if expired(active.permit.limits.deadline):
                        return await self.reject(key, command, member_id, "run_expired")
                    grants = active.permit.limits.grants
                    if grants.intersect(authority.grants) != grants:
                        return await self.reject(key, command, member_id, "new_authorization_required")
                    message = await self.make_message(actor_id, request, work=active, explicit=True)
                    receipt = CommandReceipt(
                        request_id=request.request_id,
                        accepted=True,
                        route="active_run",
                        member_id=member_id,
                        work_id=active.proposal.work_id,
                        run_id=active.run_id,
                        message_id=message.message_id,
                    )
                    await self.acknowledge(key, command, receipt)
                    await self.deliver(message)
                    return receipt
            rejection = self.capacity(actor_id, member_id)
            if rejection is not None:
                return await self.reject(key, command, member_id, rejection)
            work_id = stable_id(self.state.scope_id, actor_id, request.request_id, "work")
            envelope = request.content.model_copy(
                update={"input_id": stable_id(work_id, "input"), "source": actor_id, "kind": "user"}
            )
            proposal = WorkProposal(
                scope_id=self.state.scope_id,
                work_id=work_id,
                member_id=member_id,
                session_id=member.session_id,
                actor_id=actor_id,
                profile_id=member.profile_id,
                content=envelope,
                authority=authority,
            )
            work = WorkRecord(proposal=proposal)
            self.put_work(work)
            self.state = self.state.model_copy(update={"commands": {**self.state.commands, key: command}})
            await self.commit()
            admission = await self.host.admit_work(proposal, self.endpoint(member_id))
            if isinstance(admission, WorkRejection):
                self.put_work(transition(work, "terminal").model_copy(update={"reason": admission.code}))
                self.event("work.rejected", member_id, work_id=work_id)
                return await self.reject(key, command, member_id, admission.code)
            self.validate_binding(admission.binding, member_id, admission.permit)
            parent_limits = authority.parent_limits
            if parent_limits is None:
                raise CollaborationStorageError("An admitted work lost its parent limits.")
            effective = effective_policy(
                admission.permit.limits,
                admission.binding.runner.host_policy,
                agent_hints=admission.binding.agent.policy_hints,
                parent=child_policy(authority),
            )
            reservation = self.budgets.reserve(
                work_id,
                owner,
                parent_limits,
                effective.max_model_steps,
                effective.max_output_tokens,
            )
            if reservation is None:
                self.put_work(transition(work, "terminal").model_copy(update={"reason": "budget_exhausted"}))
                self.event("work.rejected", member_id, work_id=work_id)
                return await self.reject(key, command, member_id, "budget_exhausted")
            limits = admission.permit.limits
            permit = admission.permit.model_copy(
                update={
                    "limits": limits.model_copy(
                        update={
                            "max_model_steps": reservation.model_steps,
                            "max_output_tokens": reservation.output_tokens,
                            "max_response_output_tokens": effective.max_response_output_tokens,
                            "deadline": effective.deadline,
                            "grants": effective.grants,
                            "tool_concurrency": effective.tool_concurrency,
                        }
                    )
                }
            )
            work = transition(work, "queued").model_copy(
                update={
                    "permit": permit,
                    "fingerprint": self.fingerprint(admission.binding),
                }
            )
            self.put_work(work)
            self.put_message(
                MessageRecord(
                    message_id=envelope.input_id,
                    actor_id=actor_id,
                    member_id=member_id,
                    envelope=envelope,
                    work_id=work_id,
                    explicit=True,
                )
            )
            self.event("work.accepted", member_id, work_id=work_id)
            if isinstance(request, SpawnRequest):
                self.event("member.created", member_id, work_id=work_id)
            receipt = CommandReceipt(
                request_id=request.request_id,
                accepted=True,
                route="new_work",
                member_id=member_id,
                work_id=work_id,
                message_id=envelope.input_id,
            )
            await self.acknowledge(key, command, receipt)
            await self.schedule()
            return receipt

    async def reject(self, key: str, command: CommandRecord, member_id: str, code: str) -> CommandReceipt:
        """Persist a normal rejected outcome so retries cannot change its route."""
        return await self.acknowledge(
            key,
            command,
            CommandReceipt(
                request_id=command.request_id,
                accepted=False,
                route="rejected",
                member_id=member_id,
                code=code,
            ),
        )

    def fingerprint(self, binding: WorkBinding) -> str:
        """Bind accepted work to its resolved Agent, context, and handoff definitions."""
        payload = [
            binding.binding_key,
            binding.runner.resolve_binding(binding.agent).fingerprint,
            [value.model_dump(mode="json") for value in binding.context],
            [value.target.fingerprint for value in binding.handoffs],
        ]
        return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def validate_binding(self, binding: WorkBinding, member_id: str, permit: WorkPermit) -> None:
        """Require real endpoint lifecycle hooks and the actual shared ledger."""
        if binding.binding_key != permit.binding_key:
            raise CollaborationConflict("The binding does not match the admitted permit.")
        resolved = binding.runner.resolve_binding(binding.agent)
        tools = resolved.tools
        if not any(value.execution_scope is self.endpoint(member_id).execution_scope for value in tools):
            raise CollaborationConflict("A member Agent must bind at least one Tool from its trusted endpoint.")
        if any(value.budget_ledger is not None and value.budget_ledger is not self.budgets for value in tools):
            raise CollaborationConflict("All orchestration modes must share the same BudgetLedger instance.")

    async def make_message(
        self, actor_id: str, request: MessageRequest, *, work: WorkRecord | None, explicit: bool
    ) -> MessageRecord:
        """Reserve a durable input identity before any cross-store delivery."""
        await self.project_inputs(request.member_id)
        pending = sum(
            value.consumed_step_id is None and not value.revoked and value.member_id == request.member_id
            for value in self.state.messages.values()
        )
        if pending >= self.state.limits.max_pending_messages:
            raise CollaborationConflict("The member inbox is full.")
        if len(request.content.model_dump_json().encode()) > self.state.limits.max_message_bytes:
            raise CollaborationConflict("The message exceeds the configured byte limit.")
        identity = stable_id(self.state.scope_id, actor_id, request.request_id, "message")
        value = MessageRecord(
            message_id=identity,
            actor_id=actor_id,
            member_id=request.member_id,
            envelope=request.content.model_copy(
                update={"input_id": identity, "source": actor_id, "kind": "observation"}
            ),
            work_id=None if work is None else work.proposal.work_id,
            target_run_id=None if work is None or not explicit else work.run_id,
            explicit=explicit,
        )
        self.put_message(value)
        self.event("message.accepted", request.member_id, message_id=identity)
        return value

    async def send(self, actor_id: str, request: MessageRequest, authority: ToolContext) -> CommandReceipt:
        """Accept a message without creating work or waking an idle member."""
        async with self.lock:
            key, command = self.command(actor_id, request.request_id, "send", request.model_dump(mode="json"))
            if command.receipt is not None:
                return command.receipt
            self.authorize(actor_id, authority)
            target = self.member(actor_id, request.member_id)
            if target.state != "open":
                return await self.reject(key, command, target.member_id, "member_closed")
            work = self.active_work(target.member_id)
            if request.expected_run_id is not None and (
                work is None or work.run_id != request.expected_run_id or not work.gate_open
            ):
                return await self.reject(key, command, target.member_id, "run_mismatch")
            message = await self.make_message(actor_id, request, work=None, explicit=False)
            receipt = CommandReceipt(
                request_id=request.request_id,
                accepted=True,
                route="message",
                member_id=request.member_id,
                message_id=message.message_id,
            )
            await self.acknowledge(key, command, receipt)
            if (work is not None and work.gate_open) or request.member_id == self.state.root_member_id:
                await self.deliver(message)
            return receipt

    async def deliver(self, message: MessageRecord) -> None:
        """Idempotently flush a committed outbox item into the member's Session."""
        if message.delivered or message.revoked:
            return
        await self.session(message.member_id).enqueue(
            message.envelope,
            run_id=message.target_run_id,
            invocation_id=message.work_id if message.explicit and message.target_run_id is None else None,
        )
        self.put_message(message.model_copy(update={"delivered": True}))
        self.event("message.delivered", message.member_id, message_id=message.message_id)
        await self.commit()

    async def schedule(self) -> None:
        """Dispatch runnable queued work in acceptance order under the admission lock."""
        if not self.accepting or self.state.state != "open" or self.task_group is None:
            return
        for work_id, work in tuple(self.state.works.items()):
            decisions = self.state.resumptions.get(work_id)
            resuming = work.phase == "paused" and decisions is not None
            if (work.phase != "queued" and not resuming) or work.proposal.authority.run_id in self.blocked_runs:
                continue
            if not resuming and self.active_work(work.proposal.member_id) is not None:
                continue
            if self.state.members[work.proposal.member_id].state != "open":
                continue
            active = sum(value.phase in {"dispatching", "running", "stopping"} for value in self.state.works.values())
            if active >= self.state.limits.max_active_runs:
                break
            self.put_work(transition(work, "dispatching"))
            self.state = self.state.model_copy(
                update={
                    "resumptions": {key: value for key, value in self.state.resumptions.items() if key != work_id},
                }
            )
            await self.commit()
            self.tasks[work_id] = self.task_group.create_task(
                self.execute(work_id, decisions), name=f"collaboration-{work_id}"
            )

    async def execute(self, work_id: str, decisions: tuple[ResumeDecision, ...] | None = None) -> None:
        """Own one Runner handle through outcome settlement and result publication."""
        handle: RunHandle | None = None
        try:
            async with self.lock:
                work = self.state.works[work_id]
                if not self.accepting or work.phase != "dispatching":
                    return
                permit = work.permit
                if permit is None:
                    raise CollaborationStorageError("A dispatched work requires its durable permit.")
                binding = await self.host.rebind_work(work.proposal, permit, self.endpoint(work.proposal.member_id))
                if isinstance(binding, WorkRejection):
                    await self.reject_dispatch(work, binding.code)
                    await self.schedule()
                    return
                self.validate_binding(binding, work.proposal.member_id, permit)
                if self.fingerprint(binding) != work.fingerprint:
                    await self.reject_dispatch(work, "binding_changed")
                    await self.schedule()
                    return
                if expired(permit.limits.deadline):
                    await self.reject_dispatch(work, "deadline_expired")
                    await self.schedule()
                    return
                handle = await complete_owned(self.start_handle(work, binding, decisions))
            async with asyncio.TaskGroup() as observers:
                observers.create_task(self.observe(work_id, handle))
                outcome = await self.await_outcome(handle)
            async with self.lock:
                await self.record_outcome(work_id, outcome)
                await self.schedule()
        except asyncio.CancelledError:
            handle = self.handles.get(work_id) if handle is None else handle
            if handle is not None:
                outcome = await complete_owned(handle.cancel(reason="collaboration_owner_cancelled"))
                async with self.lock:
                    await self.record_outcome(work_id, outcome)
            raise

        except Exception:
            handle = self.handles.get(work_id) if handle is None else handle
            if handle is not None and not handle.finished:
                await complete_owned(handle.cancel(reason="collaboration_system_fault"))
            async with self.lock:
                self.state = self.state.model_copy(update={"state": "faulted"})
                self.changed.notify_all()
            raise

    async def start_handle(
        self, work: WorkRecord, binding: WorkBinding, decisions: tuple[ResumeDecision, ...] | None
    ) -> RunHandle:
        """Retain the actual Run owner before a cancelled dispatcher can lose its acknowledgement."""
        work_id = work.proposal.work_id
        permit = work.permit
        if permit is None:
            raise CollaborationStorageError("Starting work requires its durable permit.")
        session = self.session(work.proposal.member_id)
        if decisions is None:
            for message in tuple(self.state.messages.values()):
                if message.member_id == work.proposal.member_id and (
                    message.work_id is None or message.work_id == work_id
                ):
                    await self.deliver(message)
            inputs = await session.pending_inputs()
            input_ids = tuple(
                value.envelope.input_id
                for value in inputs
                if value.invocation_id in {None, work_id} and value.target_run_id is None
            )
            work = work.model_copy(update={"input_ids": input_ids})
            self.put_work(work)
            await self.commit()
            handle = await binding.runner.start(
                binding.agent,
                session=session,
                invocation_id=work_id,
                input_ids=input_ids,
                input_mode="live",
                limits=permit.limits,
                context=binding.context,
                handoffs=binding.handoffs,
                parent_run_id=work.proposal.authority.run_id,
                parent_policy=child_policy(work.proposal.authority),
            )
        else:
            if work.run_id is None:
                raise CollaborationStorageError("Resuming work requires its original Run identity.")
            checkpoint = await session.checkpoint(work.run_id)
            if checkpoint is None:
                raise CollaborationConflict("The work has no resumable checkpoint.")
            handle = await binding.runner.resume(
                binding.agent,
                session=session,
                checkpoint=checkpoint,
                decisions=decisions,
                limits=permit.limits,
                handoffs=binding.handoffs,
                parent_policy=child_policy(work.proposal.authority),
            )
        self.handles[work_id] = handle
        work = transition(work, "running").model_copy(update={"run_id": handle.run_id, "gate_open": True})
        self.put_work(work)
        self.event("work.running", work.proposal.member_id, work_id=work_id)
        await self.commit()
        return handle

    async def observe(self, work_id: str, handle: RunHandle) -> None:
        """Project durable Run progress without copying private model content."""
        cursor = self.state.run_cursors.get(handle.run_id, 0)
        async for value in handle.events(after_sequence=cursor):
            async with self.lock:
                await self.project_event(work_id, handle.run_id, value.sequence, value.event)

    async def project_event(self, work_id: str, run_id: str, sequence: int, data: JsonObject) -> None:
        """Project each original Run event once during live execution or factual recovery."""
        if sequence <= self.state.run_cursors.get(run_id, 0):
            return
        work = self.state.works[work_id]
        kind = data.get("kind")
        if isinstance(kind, str) and kind in {
            "step.started",
            "step.completed",
            "message.completed",
            "tool.started",
            "tool.completed",
            "tool.interrupted",
            "run.failed",
            "run.cancelled",
            "run.paused",
        }:
            step = data.get("step_id")
            event = CollaborationEvent(
                sequence=len(self.state.events) + 1,
                event_id=stable_id(self.state.scope_id, "event", str(len(self.state.events) + 1)),
                scope_id=self.state.scope_id,
                kind=f"run.{kind}",
                member_id=work.proposal.member_id,
                work_id=work_id,
                run_id=run_id,
                source_sequence=sequence,
                step_id=step if isinstance(step, str) else None,
            )
            self.state = self.state.model_copy(update={"events": (*self.state.events, event)})
        await self.project_inputs(work.proposal.member_id)
        self.state = self.state.model_copy(update={"run_cursors": {**self.state.run_cursors, run_id: sequence}})
        await self.commit()

    async def replay_progress(self, work_id: str, run_id: str) -> None:
        """Catch up persisted progress without opening another model request."""
        work = self.state.works[work_id]
        session = self.session(work.proposal.member_id)
        while True:
            records = await session.store.read_events(
                session.session_id,
                run_id,
                after_sequence=self.state.run_cursors.get(run_id, 0),
                limit=256,
            )
            if not records:
                return
            for record in records:
                await self.project_event(work_id, run_id, record.sequence, record.data)

    async def await_outcome(self, handle: RunHandle) -> RunOutcome:
        """Isolate confirmed model-provider failures after Runner has committed cleanup."""
        try:
            return await handle.result()
        except (
            ModelAuthenticationError,
            ModelConfigurationError,
            ModelConnectionError,
            ModelPermissionError,
            ModelProviderOverloadedError,
            ModelProviderUnavailableError,
            ModelRateLimitError,
            ModelRequestError,
            ModelResponseError,
            ModelTimeoutError,
            ModelUnsupportedError,
            PermissionError,
            FileNotFoundError,
        ) as error:
            snapshot = await handle.session.store.run(handle.session.session_id, handle.run_id)
            if snapshot.receipt is None:
                raise CollaborationStorageError("A failed model Run did not commit its cleanup receipt.") from error
            return RunResult(snapshot.receipt, error if isinstance(error, PleiadError) else None)

    async def project_inputs(self, member_id: str) -> None:
        """Record consumption only when the authoritative Session has a model-step claim."""
        session = self.session(member_id)
        journal = await session.store.snapshot(session.session_id)
        for value in journal.inputs:
            message = self.state.messages.get(value.envelope.input_id)
            if message is not None and value.step_id is not None and message.consumed_step_id is None:
                self.put_message(
                    message.model_copy(
                        update={
                            "consumed_run_id": value.run_id,
                            "consumed_step_id": value.step_id,
                        }
                    )
                )
                self.event("message.consumed", member_id, message_id=message.message_id)

    async def finish_without_run(self, work: WorkRecord, reason: str) -> None:
        """Settle an admitted work proven not to have submitted an invocation."""
        snapshot = await self.session(work.proposal.member_id).run_for_invocation(work.proposal.work_id)
        if snapshot is not None:
            if snapshot.receipt is not None:
                await self.record_receipt(work.proposal.work_id, snapshot.receipt)
            else:
                self.budgets.settle(work.proposal.work_id, None)
                self.put_work(
                    transition(work, "terminal").model_copy(
                        update={
                            "run_id": snapshot.run_id,
                            "resolution": "unknown",
                            "reason": "reconciliation_required",
                        }
                    )
                )
                self.event("work.unknown", work.proposal.member_id, work_id=work.proposal.work_id)
                await self.commit()
            return
        owner = work.proposal.authority.run_id
        if owner is None:
            raise CollaborationStorageError("An admitted work requires an owning parent Run.")
        self.budgets.settle(
            work.proposal.work_id,
            UsageCharge(
                parent_run_id=owner,
                charge_id=work.proposal.work_id,
                model_steps=0,
                output_tokens=0,
            ),
        )
        self.put_work(transition(work, "terminal").model_copy(update={"reason": reason}))
        for message in tuple(self.state.messages.values()):
            if message.work_id == work.proposal.work_id and not message.delivered:
                self.put_message(message.model_copy(update={"revoked": True}))
        self.event("work.terminal", work.proposal.member_id, work_id=work.proposal.work_id)
        await self.commit()

    async def reject_dispatch(self, work: WorkRecord, reason: str) -> None:
        """Keep a rejected checkpoint paused while settling work that never acquired a Run."""
        if work.run_id is not None:
            self.put_work(transition(work, "paused").model_copy(update={"reason": reason}))
            self.event("work.resume_rejected", work.proposal.member_id, work_id=work.proposal.work_id)
            await self.commit()
        else:
            await self.finish_without_run(work, reason)

    async def record_outcome(self, work_id: str, outcome: RunOutcome) -> None:
        """Settle one stable charge and publish at most one bounded parent result."""
        work = self.state.works[work_id]
        if work.phase == "terminal" or (work.phase == "paused" and isinstance(outcome, RunSuspension)):
            return
        owner = work.proposal.authority.run_id
        if owner is None:
            raise CollaborationStorageError("A work outcome has no owning Run.")
        paused = isinstance(outcome, RunSuspension)
        await self.project_inputs(work.proposal.member_id)
        if not paused:
            for message in tuple(self.state.messages.values()):
                if message.work_id == work_id and message.explicit and message.consumed_step_id is None:
                    self.put_message(message.model_copy(update={"revoked": True}))
        self.budgets.settle(work_id, outcome.usage.charge(owner, work_id), interrupted=paused)
        work = transition(work, "paused" if paused else "terminal").model_copy(
            update={
                "run_id": outcome.run_id,
                "receipt": None if isinstance(outcome, RunSuspension) else outcome.receipt,
            }
        )
        self.put_work(work)
        self.event("work.paused" if paused else "work.terminal", work.proposal.member_id, work_id=work_id)
        if not isinstance(outcome, RunSuspension):
            identity = stable_id(work_id, "result")
            summary = (
                outcome.final_output[:2048] if outcome.final_output else f"Run {outcome.state.value}: {outcome.reason}"
            )
            reference = ResultReference(reference=f"collaboration:{self.state.scope_id}:{work_id}", summary=summary)
            self.put_message(
                MessageRecord(
                    message_id=identity,
                    actor_id=work.proposal.member_id,
                    member_id=work.proposal.actor_id,
                    envelope=InputEnvelope(
                        input_id=identity,
                        kind="delegate_result",
                        source=work.proposal.member_id,
                        content=reference,
                        causation_id=work_id,
                    ),
                )
            )
        await self.commit()
        if not paused:
            parent_work = self.active_work(work.proposal.actor_id)
            if work.proposal.actor_id == self.state.root_member_id or (
                parent_work is not None and parent_work.gate_open
            ):
                await self.deliver(self.state.messages[stable_id(work_id, "result")])

    async def before_finish(self, member_id: str, run_id: str) -> bool:
        """Atomically admit pending explicit followups or seal this Run's input gate."""
        async with self.lock:
            if member_id == self.state.root_member_id:
                journal = await self.root_session.store.snapshot(self.root_session.session_id)
                snapshot = next((value for value in journal.runs if value.run_id == run_id), None)
                return (
                    snapshot is not None
                    and snapshot.input_mode == "live"
                    and any(
                        value.step_id is None
                        and value.envelope.kind == "user"
                        and value.run_id in {None, run_id}
                        and value.target_run_id in {None, run_id}
                        and value.invocation_id in {None, snapshot.invocation_id}
                        for value in journal.inputs
                    )
                )
            work = self.active_work(member_id)
            if work is None or work.run_id != run_id:
                return False
            state = await self.session(member_id).store.snapshot(self.session(member_id).session_id)
            pending = {
                value.envelope.input_id
                for value in state.inputs
                if value.target_run_id == run_id and value.step_id is None
            }
            if any(
                value.explicit and value.target_run_id == run_id and value.message_id in pending
                for value in self.state.messages.values()
            ):
                return True
            self.put_work(work.model_copy(update={"gate_open": False}))
            await self.commit()
            return False

    async def stop_work(self, work_id: str, *, pause: bool = False) -> None:
        """Stop the observed work and await its writer cleanup before returning."""
        async with self.lock:
            work = self.state.works[work_id]
            if work.phase == "terminal":
                return
            if work.phase in {"prepared", "queued", "dispatching"} and work_id not in self.handles:
                if pause:
                    if work.phase == "dispatching":
                        self.put_work(transition(work, "queued"))
                    owner = work.proposal.authority.run_id
                    if owner is not None:
                        self.budgets.settle(
                            work_id,
                            UsageCharge(
                                parent_run_id=owner,
                                charge_id=work_id,
                                model_steps=0,
                                output_tokens=0,
                            ),
                            interrupted=True,
                        )
                    await self.commit()
                else:
                    await self.finish_without_run(work, "owner_closed")
                return
            handle = self.handles.get(work_id)
            if handle is None:
                if work.phase == "paused" and work.run_id is not None:
                    checkpoint = await self.session(work.proposal.member_id).checkpoint(work.run_id)
                    if checkpoint is None:
                        raise CollaborationStorageError("A paused work lost its checkpoint.")
                    if pause:
                        return
                    snapshot = await self.session(work.proposal.member_id).store.cancel_paused(
                        work.proposal.session_id, checkpoint=checkpoint, reason="collaboration_closed"
                    )
                    if snapshot.receipt is None:
                        raise CollaborationStorageError("Cancelling a paused work did not produce a receipt.")
                    await self.record_receipt(work_id, snapshot.receipt)
                    return
                raise CollaborationStorageError("An active work has no local owner handle.")
            self.put_work(transition(work, "stopping"))
            await self.commit()
        outcome = await (
            handle.pause(reason="parent_paused") if pause else handle.cancel(reason="collaboration_interrupted")
        )
        async with self.lock:
            await self.record_outcome(work_id, outcome)
            if not pause:
                await self.schedule()

    async def record_receipt(self, work_id: str, receipt: RunReceipt) -> None:
        """Project recovered receipts through the same exactly-once settlement path."""
        await self.record_outcome(work_id, RunResult(receipt))

    async def close(self) -> None:
        """Cancel all owned work and close membership while retaining durable history."""
        await complete_owned(self.shutdown_owned(close_members=True))

    async def shutdown_owned(self, *, close_members: bool) -> None:
        """Stop local admission and settle work without implicitly deleting membership.

        Context exit may preserve an open journal for a later exclusive deployment.
        Explicit close always terminates membership. Neither path restarts work,
        changes unknown outcomes, or closes borrowed root Sessions and Runners.
        """
        async with self.lock:
            self.accepting = False
            self.changed.notify_all()
            if self.state.state == "closed":
                return
            if close_members:
                self.state = self.state.model_copy(update={"state": "closing"})
                await self.commit()
            identities = tuple(self.state.works)
        for identity in reversed(identities):
            await self.stop_work(identity)
        tasks = tuple(value for value in self.tasks.values() if value is not asyncio.current_task())
        if tasks:
            await asyncio.gather(*tasks)
        async with self.lock:
            if close_members:
                self.state = self.state.model_copy(
                    update={
                        "state": "closed",
                        "members": {
                            key: value.model_copy(update={"state": "closed"})
                            for key, value in self.state.members.items()
                        },
                    }
                )
            await self.commit()
            self.entered = False

    async def recover(self, *, expected_epoch: int) -> None:
        """Reconcile original facts under exclusive host ownership without paid replay."""
        await complete_owned(self.recover_owned(expected_epoch))

    async def recover_owned(self, expected_epoch: int) -> None:
        """Retain the claimed owner epoch through a completed factual reconciliation."""
        if self.entered:
            raise CollaborationConflict("Recovery requires a detached group owner.")
        async with self.lock:
            self.state = await self.store.claim(self.state.scope_id, expected_epoch)
            if self.state.members[self.state.root_member_id].session_id != self.root_session.session_id:
                raise CollaborationConflict("Recovery must retain the original root Session.")
            self.budgets.import_snapshot(self.state.ledger)
            ordered = sorted(
                self.state.works.values(),
                key=lambda value: self.state.members[value.proposal.member_id].depth,
                reverse=True,
            )
            for work in ordered:
                snapshot = await self.session(work.proposal.member_id).run_for_invocation(work.proposal.work_id)
                if snapshot is not None:
                    await self.replay_progress(work.proposal.work_id, snapshot.run_id)
                if work.phase == "terminal":
                    continue
                if snapshot is not None and snapshot.receipt is not None:
                    await self.record_receipt(work.proposal.work_id, snapshot.receipt)
                elif snapshot is not None and snapshot.state is RunStatus.PAUSED and snapshot.checkpoint is not None:
                    owner = work.proposal.authority.run_id
                    if owner is None:
                        raise CollaborationStorageError("Recovered paused work lost its parent Run.")
                    self.budgets.settle(
                        work.proposal.work_id, snapshot.usage.charge(owner, work.proposal.work_id), interrupted=True
                    )
                    self.put_work(
                        work.model_copy(update={"phase": "paused", "run_id": snapshot.run_id, "gate_open": False})
                    )
                elif snapshot is None and work.phase in {"prepared", "queued", "dispatching"}:
                    await self.finish_without_run(work, "recovery_not_started")
                else:
                    self.budgets.settle(work.proposal.work_id, None)
                    self.put_work(
                        work.model_copy(
                            update={
                                "phase": "terminal",
                                "gate_open": False,
                                "resolution": "unknown",
                                "run_id": work.run_id if snapshot is None else snapshot.run_id,
                                "reason": "reconciliation_required",
                            }
                        )
                    )
                    self.event("work.unknown", work.proposal.member_id, work_id=work.proposal.work_id)
            for key, command in tuple(self.state.commands.items()):
                if command.receipt is None:
                    work_id = stable_id(self.state.scope_id, command.actor_id, command.request_id, "work")
                    prepared_work = self.state.works.get(work_id)
                    if prepared_work is None:
                        raise CollaborationStorageError("A prepared command lost its original work intent.")
                    receipt = CommandReceipt(
                        request_id=command.request_id,
                        accepted=False,
                        route="rejected",
                        member_id=prepared_work.proposal.member_id,
                        work_id=work_id,
                        code="recovery_interrupted",
                    )
                    self.state = self.state.model_copy(
                        update={
                            "commands": {**self.state.commands, key: command.model_copy(update={"receipt": receipt})},
                        }
                    )
            for member_id in self.state.members:
                await self.project_inputs(member_id)
            for message in tuple(self.state.messages.values()):
                if (
                    message.explicit
                    and message.consumed_step_id is None
                    and message.work_id is not None
                    and self.state.works[message.work_id].phase == "terminal"
                ):
                    self.put_message(message.model_copy(update={"revoked": True}))
            await self.commit()
            for message in tuple(self.state.messages.values()):
                if message.member_id == self.state.root_member_id and not message.explicit:
                    await self.deliver(message)
            self.recovered = True

    async def reconcile_work(
        self,
        work_id: str,
        *,
        runner: Runner,
        request_id: str,
        expected_epoch: int,
        expected_revision: int,
        evidence: tuple[ActionEvidence, ...] = (),
    ) -> None:
        """Apply host evidence to an unknown original Run without submitting another invocation.

        The host must retain exclusive deployment ownership and supply the same
        request identity and expected versions when retrying reconciliation.
        Conservative charges remain spent; reconciliation never replenishes them.
        """
        if not self.recovered:
            raise CollaborationConflict("Factual reconciliation requires an exclusively recovered scope.")
        async with self.lock:
            work = self.state.works[work_id]
            if work.run_id is None:
                raise CollaborationConflict("The work has no accepted invocation to reconcile.")
            result = await runner.reconcile(
                session=self.session(work.proposal.member_id),
                run_id=work.run_id,
                request_id=request_id,
                expected_epoch=expected_epoch,
                expected_revision=expected_revision,
                evidence=evidence,
            )
            if result.snapshot.receipt is None:
                raise CollaborationConflict("The original Run still requires its existing checkpoint resolution.")
            self.put_work(
                work.model_copy(
                    update={
                        "resolution": "unknown" if result.unsettled_actions else "clear",
                        "receipt": result.snapshot.receipt,
                    }
                )
            )
            self.event("work.reconciled", work.proposal.member_id, work_id=work_id)
            await self.commit()
