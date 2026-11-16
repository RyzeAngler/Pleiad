from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from pleiad.contracts import JsonObject
from pleiad.limits import expired
from pleiad.orchestration.collaboration.contracts import (
    CloseRequest,
    CommandReceipt,
    EventCursor,
    EventPage,
    InterruptRequest,
    MessageRequest,
    MessageStatus,
    SpawnRequest,
)
from pleiad.orchestration.collaboration.errors import CollaborationConflict, CollaborationStorageError
from pleiad.orchestration.collaboration.state import MemberRecord, WorkRecord, transition
from pleiad.runtime.checkpoint import ResumeDecision
from pleiad.sessions.records import RunReceipt, RunStatus
from pleiad.sessions.store import complete_owned
from pleiad.tools.base import ToolScope
from pleiad.tools.contracts import ToolContext

if TYPE_CHECKING:
    from pleiad.orchestration.collaboration.group import CollaborationGroup


class MemberEndpoint:
    """Bind model-visible commands to a trusted member and its owned lifecycle."""

    def __init__(self, group: CollaborationGroup, member_id: str) -> None:
        """Create one stable Tool scope shared by all tools for this member."""
        self.group = group
        self.member_id = member_id
        self.execution_scope = ToolScope(
            self.open_run, self.wait_owned, self.close_run, self.checkpoint, self.restore, self.before_finish
        )

    async def spawn(self, request: SpawnRequest, authority: ToolContext) -> CommandReceipt:
        """Admit a new member and finish acceptance despite caller cancellation."""
        return await complete_owned(self.group.submit(self.member_id, request, authority))

    async def followup(self, request: MessageRequest, authority: ToolContext) -> CommandReceipt:
        """Attach to an open Run or admit a new invocation on the same Session."""
        return await complete_owned(self.group.submit(self.member_id, request, authority))

    async def send_message(self, request: MessageRequest, authority: ToolContext) -> CommandReceipt:
        """Accept bounded input without changing execution or budget ownership."""
        return await complete_owned(self.group.send(self.member_id, request, authority))

    async def open_run(self, run_id: str) -> None:
        """Attach a real Runner lifecycle and reopen paused owner admission."""
        async with self.group.lock:
            if not self.group.entered or self.group.state.state not in {"open", "closing"}:
                raise CollaborationConflict("The collaboration scope has no active owner.")
            if self.member_id == self.group.state.root_member_id:
                if not self.group.accepting or self.group.state.state != "open":
                    raise CollaborationConflict("The collaboration scope is closing.")
                self.group.root_runs.add(run_id)
            self.group.blocked_runs.discard(run_id)
            await self.group.schedule()

    async def wait_owned(self, run_id: str) -> None:
        """Settle children owned by this Run before budget admission is retried."""
        async with self.group.changed:
            while any(
                value.proposal.authority.run_id == run_id
                and (
                    value.phase in {"queued", "dispatching", "running", "stopping"}
                    or key in self.group.state.resumptions
                )
                for key, value in self.group.state.works.items()
            ):
                if self.group.state.state == "faulted":
                    raise CollaborationStorageError("The collaboration scope failed while waiting for child work.")
                await self.group.changed.wait()

    async def close_run(self, run_id: str, cancelled: bool, suspended: bool) -> None:
        """Close owned child admission and settle or pause every descendant work."""
        await complete_owned(self.close_run_owned(run_id, cancelled, suspended))

    async def close_run_owned(self, run_id: str, cancelled: bool, suspended: bool) -> None:
        """Settle descendants outside the already-cancelled parent Runner task."""
        async with self.group.lock:
            self.group.blocked_runs.add(run_id)
            identities = tuple(
                key for key, work in self.group.state.works.items() if work.proposal.authority.run_id == run_id
            )
        for identity in reversed(identities):
            await self.group.stop_work(identity, pause=suspended and not cancelled)

    def checkpoint(self, run_id: str) -> JsonObject:
        """Reference the durable scope without copying its transcript into a Run."""
        return {"scope_id": self.group.state.scope_id, "member_id": self.member_id, "run_id": run_id}

    def restore(self, run_id: str, state: JsonObject) -> None:
        """Reject a checkpoint rebound to a different member or scope."""
        if state != self.checkpoint(run_id):
            raise CollaborationConflict("The collaboration checkpoint binding changed.")

    async def before_finish(self, run_id: str) -> bool:
        """Participate in the generic model-step completion boundary."""
        return await self.group.before_finish(self.member_id, run_id)

    async def list_members(self) -> tuple[MemberRecord, ...]:
        """Read scope membership without private transcripts or state changes."""
        async with self.group.lock:
            self.group.member(self.member_id, self.member_id)
            return tuple(self.group.state.members.values())

    async def status(self, member_id: str) -> tuple[WorkRecord, ...]:
        """Read the authorized descendant's persistent work facts."""
        async with self.group.lock:
            self.group.member(self.member_id, member_id, control=True)
            return tuple(value for value in self.group.state.works.values() if value.proposal.member_id == member_id)

    async def message_status(self, message_id: str) -> MessageStatus:
        """Read acceptance and consumption facts without acknowledging or waking input."""
        async with self.group.lock:
            message = self.group.state.messages.get(message_id)
            if message is None:
                raise CollaborationConflict("Unknown message identity.")
            if self.member_id not in {message.actor_id, message.member_id}:
                self.group.member(self.member_id, message.member_id, control=True)
            session = self.group.session(message.member_id)
            journal = await session.store.snapshot(session.session_id)
            consumed = next((value for value in journal.inputs if value.envelope.input_id == message_id), None)
            if consumed is not None and consumed.step_id is not None:
                return MessageStatus(
                    message_id=message_id,
                    member_id=message.member_id,
                    state="consumed",
                    run_id=consumed.run_id,
                    step_id=consumed.step_id,
                )
            if message.revoked:
                state = "revoked"
            elif self.group.active_work(message.member_id) is None:
                state = "retained"
            else:
                state = "delivered" if message.delivered else "received"
            return MessageStatus(message_id=message_id, member_id=message.member_id, state=state)

    async def read_result(self, work_id: str) -> RunReceipt | None:
        """Read the original durable result separately from event observation."""
        async with self.group.lock:
            work = self.group.state.works.get(work_id)
            if work is None:
                raise CollaborationConflict("Unknown work identity.")
            self.group.member(self.member_id, work.proposal.member_id, control=True)
            return work.receipt

    async def wait(
        self, member_ids: tuple[str, ...], *, after: EventCursor, timeout_seconds: float = 30.0, limit: int = 100
    ) -> EventPage:
        """Observe wait-any events with an independent cursor and no consumption effects."""
        if not member_ids:
            raise CollaborationConflict("Wait requires at least one strict descendant target.")
        if (
            timeout_seconds < 0
            or timeout_seconds > 60
            or limit < 1
            or limit > self.group.state.limits.max_event_page_size
        ):
            raise CollaborationConflict("Wait bounds exceed the configured limits.")
        if after.scope_id != self.group.state.scope_id:
            raise CollaborationConflict("The event cursor belongs to another scope.")
        async with self.group.changed:
            if after.sequence > len(self.group.state.events):
                raise CollaborationConflict("The event cursor is ahead of this scope's journal.")
            for member_id in member_ids:
                self.group.member(self.member_id, member_id, control=True)
            deadline = asyncio.get_running_loop().time() + timeout_seconds
            while True:
                events = tuple(
                    value
                    for value in self.group.state.events
                    if value.sequence > after.sequence and (not member_ids or value.member_id in member_ids)
                )[:limit]
                if events:
                    return EventPage(
                        events=events, cursor=EventCursor(scope_id=after.scope_id, sequence=events[-1].sequence)
                    )
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0 or not self.group.accepting or self.group.state.state != "open":
                    return EventPage(events=(), cursor=after, timed_out=remaining <= 0)
                try:
                    async with asyncio.timeout(remaining):
                        await self.group.changed.wait()
                except TimeoutError:
                    return EventPage(events=(), cursor=after, timed_out=True)

    async def interrupt(self, request: InterruptRequest, authority: ToolContext) -> CommandReceipt:
        """Interrupt the exact observed invocation and wait for resource settlement."""
        return await complete_owned(self.interrupt_owned(request, authority))

    async def interrupt_owned(self, request: InterruptRequest, authority: ToolContext) -> CommandReceipt:
        """Persist a control intent before cancelling its original handle."""
        async with self.group.lock:
            key, command = self.group.command(
                self.member_id, request.request_id, "interrupt", request.model_dump(mode="json")
            )
            if command.receipt is not None:
                receipt = command.receipt
            else:
                self.group.authorize(self.member_id, authority)
                self.group.member(self.member_id, request.member_id, control=True)
                work = self.group.active_work(request.member_id)
                if (
                    work is None
                    or work.proposal.work_id != request.expected_work_id
                    or work.run_id != request.expected_run_id
                ):
                    return await self.group.reject(key, command, request.member_id, "run_mismatch")
                self.group.put_work(transition(work, "stopping"))
                self.group.event("work.interrupt_requested", request.member_id, work_id=request.expected_work_id)
                receipt = await self.group.acknowledge(
                    key,
                    command,
                    CommandReceipt(
                        request_id=request.request_id,
                        accepted=True,
                        route="interrupt",
                        member_id=request.member_id,
                        work_id=request.expected_work_id,
                        run_id=request.expected_run_id,
                    ),
                )
        if receipt.accepted and receipt.work_id is not None:
            await self.group.stop_work(receipt.work_id)
        return receipt

    async def close_member(self, request: CloseRequest, authority: ToolContext) -> CommandReceipt:
        """Close a descendant subtree and preserve its queryable history."""
        return await complete_owned(self.close_member_owned(request, authority))

    async def close_member_owned(self, request: CloseRequest, authority: ToolContext) -> CommandReceipt:
        """Commit closed admission before awaiting subtree cleanup."""
        async with self.group.lock:
            key, command = self.group.command(
                self.member_id, request.request_id, "close", request.model_dump(mode="json")
            )
            if command.receipt is not None and self.group.state.members[request.member_id].state == "closed":
                return command.receipt
            if command.receipt is None:
                self.group.authorize(self.member_id, authority)
            target = self.group.member(self.member_id, request.member_id, control=True)
            if target.state == "closed":
                return await self.group.acknowledge(
                    key,
                    command,
                    CommandReceipt(
                        request_id=request.request_id,
                        accepted=True,
                        route="close",
                        member_id=request.member_id,
                    ),
                )
            members = {
                key
                for key in self.group.state.members
                if key == request.member_id or self.group.descendant(request.member_id, key)
            }
            self.group.state = self.group.state.model_copy(
                update={
                    "members": {
                        key: value.model_copy(update={"state": "closing"}) if key in members else value
                        for key, value in self.group.state.members.items()
                    }
                }
            )
            receipt = await self.group.acknowledge(
                key,
                command,
                CommandReceipt(
                    request_id=request.request_id,
                    accepted=True,
                    route="close",
                    member_id=request.member_id,
                ),
            )
            works = tuple(key for key, value in self.group.state.works.items() if value.proposal.member_id in members)
        for work_id in reversed(works):
            await self.group.stop_work(work_id)
        async with self.group.lock:
            if self.group.state.members[request.member_id].state == "closed":
                return receipt
            self.group.state = self.group.state.model_copy(
                update={
                    "members": {
                        key: value.model_copy(update={"state": "closed"}) if key in members else value
                        for key, value in self.group.state.members.items()
                    }
                }
            )
            self.group.event("member.closed", request.member_id)
            for message in tuple(self.group.state.messages.values()):
                if message.member_id in members and message.consumed_step_id is None:
                    self.group.put_message(message.model_copy(update={"revoked": True}))
            await self.group.commit()
        return receipt

    async def resume_work(
        self, work_id: str, authority: ToolContext, *, decisions: tuple[ResumeDecision, ...] = ()
    ) -> None:
        """Resume the original paused Run after current authorization is rechecked."""
        await complete_owned(self.resume_owned(work_id, authority, decisions))

    async def resume_owned(self, work_id: str, authority: ToolContext, decisions: tuple[ResumeDecision, ...]) -> None:
        """Commit resumed scheduling and retain its task despite caller cancellation."""
        async with self.group.lock:
            if authority.run_id in self.group.blocked_runs or (
                self.group.recovered
                and self.member_id == self.group.state.root_member_id
                and authority.run_id not in self.group.root_runs
            ):
                if not self.group.accepting or self.group.state.state != "open" or authority.run_id is None:
                    raise CollaborationConflict("The parent scope cannot authorize checkpoint resumption.")
                parent_session = self.group.session(self.member_id)
                parent = await parent_session.store.run(parent_session.session_id, authority.run_id)
                if parent.state is not RunStatus.PAUSED or parent.checkpoint is None:
                    raise CollaborationConflict("Resume planning requires the original paused parent Run.")
                if expired(parent.checkpoint.deadline):
                    raise CollaborationConflict("The original parent deadline expired.")
                self.group.blocked_runs.add(authority.run_id)
            else:
                self.group.authorize(self.member_id, authority)
            work = self.group.state.works[work_id]
            self.group.member(self.member_id, work.proposal.member_id, control=True)
            if work.phase != "paused" or work.proposal.authority.run_id != authority.run_id:
                raise CollaborationConflict("Resumption requires the original paused work owner.")
            if work.permit is None or work.permit.limits.grants is None:
                raise CollaborationStorageError("The paused work lost its effective permit.")
            if work.permit.limits.grants.intersect(authority.grants) != work.permit.limits.grants:
                raise CollaborationConflict("The resumed caller cannot expand its current grants.")
            if self.group.task_group is None:
                raise CollaborationConflict("The group has no active task owner.")
            previous = self.group.state.resumptions.get(work_id)
            if previous is not None and previous != decisions:
                raise CollaborationConflict("The prepared resumption already has different host decisions.")
            self.group.state = self.group.state.model_copy(
                update={
                    "resumptions": {**self.group.state.resumptions, work_id: decisions},
                }
            )
            self.group.event("work.resume_requested", work.proposal.member_id, work_id=work_id)
            await self.group.commit()
            await self.group.schedule()
