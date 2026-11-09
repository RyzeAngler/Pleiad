import asyncio
from collections.abc import Mapping
from types import TracebackType
from uuid import uuid4

from pleiad.budgets import BudgetLedger
from pleiad.contracts import JsonObject
from pleiad.events.subagents import (
    DelegationStatus,
    SubagentCompletedEvent,
    SubagentConsumedEvent,
    SubagentLifecycleEvent,
    SubagentStartedEvent,
    SubagentSubmittedEvent,
)
from pleiad.orchestration.errors import (
    DelegationNotFoundError,
    OrchestrationConfigurationError,
    OrchestrationStateError,
    SubagentBudgetError,
    SubagentNotFoundError,
)
from pleiad.orchestration.subagents.state import (
    DelegationRecord,
    SubagentSnapshot,
)
from pleiad.orchestration.subagents.subagent import Subagent
from pleiad.runtime.result import RunOutcome, RunResult, RunSuspension
from pleiad.sessions.checkpoint import RunCheckpoint
from pleiad.sessions.input import InputEnvelope
from pleiad.sessions.records import RunStatus
from pleiad.sessions.session import Session
from pleiad.tools.base import ToolScope
from pleiad.tools.contracts import ParentRunLimits, ToolContext
from pleiad.tools.interruption import ToolInterruption


class SubagentGroup:
    """Own synchronous and explicitly scoped asynchronous Subagent Runs."""

    def __init__(
        self, subagents: Mapping[str, Subagent] | tuple[Subagent, ...], *, budgets: BudgetLedger | None = None
    ) -> None:
        """Bind the host allowlist of Subagent Runs."""
        selected = (
            dict(subagents)
            if isinstance(subagents, Mapping)
            else {subagent.subagent_id: subagent for subagent in subagents}
        )
        if not isinstance(subagents, Mapping) and len(selected) != len(subagents):
            raise OrchestrationConfigurationError("Subagent IDs must be unique.")
        if any(key != subagent.subagent_id for key, subagent in selected.items()):
            raise OrchestrationConfigurationError("Subagent mapping keys must match Subagent IDs.")
        self.subagents = selected
        self.records: dict[str, DelegationRecord] = {}
        self.events: list[SubagentLifecycleEvent] = []
        self.delivered_event_ids: set[str] = set()
        self.tasks: dict[str, asyncio.Task[RunOutcome]] = {}
        self.signals: dict[str, asyncio.Event] = {}
        self.sessions: dict[str, Session] = {}
        self.task_group: asyncio.TaskGroup | None = None
        self.budgets = BudgetLedger() if budgets is None else budgets
        self.sequence = 0
        self.owner_run_id: str | None = None
        self.execution_scope = ToolScope(
            self.open_scope, self.wait_scope, self.close_scope, self.checkpoint_scope, self.restore_scope
        )

    async def open_scope(self, run_id: str) -> None:
        """Acquire the actual task group for one invoking Runtime owner."""
        self.bind_owner(ToolContext(call_id="scope", run_id=run_id))
        await self.__aenter__()

    async def wait_scope(self, run_id: str) -> None:
        """Await already accepted children without consuming their completion events."""
        if self.owner_run_id != run_id:
            raise OrchestrationStateError("The Subagent scope belongs to another Run.")
        outcomes = await asyncio.gather(*self.tasks.values(), return_exceptions=True)
        for outcome in outcomes:
            if isinstance(outcome, BaseException) and not isinstance(outcome, asyncio.CancelledError):
                raise outcome

    async def close_scope(self, run_id: str, cancelled: bool, suspended: bool) -> None:
        """Settle active children and preserve only explicitly suspended ownership."""
        if cancelled:
            for task in self.tasks.values():
                if not task.done():
                    task.cancel()
        try:
            await self.wait_scope(run_id)
        finally:
            try:
                if not suspended:
                    await self.cancel_paused(reason="parent_cancelled" if cancelled else "parent_completed")
            finally:
                await self.__aexit__(None, None, None)

    def checkpoint_scope(self, run_id: str) -> JsonObject | None:
        """Serialize this parent's coordination state alongside its Runtime checkpoint."""
        if self.owner_run_id != run_id:
            return None
        return self.snapshot().model_dump(mode="json")

    def restore_scope(self, run_id: str, value: JsonObject) -> None:
        """Rebind saved scope facts to the original Runtime owner."""
        snapshot = SubagentSnapshot.model_validate(value)
        if snapshot.owner_run_id != run_id:
            raise OrchestrationStateError("The Subagent checkpoint belongs to another parent Run.")
        self.restore(snapshot)

    async def __aenter__(self) -> "SubagentGroup":
        """Open a structured scope that owns submitted Subagent tasks."""
        if self.task_group is not None:
            raise RuntimeError("SubagentGroup scope is already open")
        self.task_group = asyncio.TaskGroup()
        await self.task_group.__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Wait for accepted work or cancel it when the parent scope fails."""
        if self.task_group is None:
            return
        if exc_type is not None:
            for task in self.tasks.values():
                task.cancel()
        task_group = self.task_group
        self.task_group = None
        await task_group.__aexit__(exc_type, exc_value, traceback)

    def subagent_for(self, subagent: str | Subagent) -> Subagent:
        """Resolve one explicitly bound Subagent or Agent-facing Subagent ID."""
        if isinstance(subagent, Subagent):
            bound = self.subagents.get(subagent.subagent_id)
            if bound is subagent:
                return subagent
            raise OrchestrationConfigurationError(
                f"Subagent is not bound to this SubagentGroup: {subagent.subagent_id}."
            )
        try:
            return self.subagents[subagent]
        except KeyError as error:
            raise SubagentNotFoundError(subagent) from error

    def subagent_for_agent(self, agent_name: str) -> Subagent:
        """Resolve the Subagent whose Agent has the requested stable name."""
        matches = tuple(subagent for subagent in self.subagents.values() if subagent.agent.name == agent_name)
        if len(matches) != 1:
            raise OrchestrationConfigurationError(f"Subagent Agent binding is ambiguous or missing: {agent_name}.")
        return matches[0]

    async def run_subagent(self, subagent: str | Subagent, task: str, context: ToolContext) -> RunOutcome:
        """Run one Subagent synchronously inside the caller's cancellation scope."""
        self.bind_owner(context)
        selected = self.subagent_for(subagent)
        if not task.strip():
            raise OrchestrationConfigurationError("Subagent task must not be blank.")
        selected.child_limits(context)
        selected.parent_policy(context)
        delegation_id = uuid4().hex
        child_context = self.reserve_budget(delegation_id, selected, context)
        self.records[delegation_id] = DelegationRecord(
            delegation_id=delegation_id,
            subagent_id=selected.subagent_id,
            task=task,
            status=DelegationStatus.RUNNING,
            parent_run_id=context.run_id,
        )
        self.signals[delegation_id] = asyncio.Event()
        self.record_lifecycle(
            SubagentSubmittedEvent,
            delegation_id,
            parent_run_id=context.run_id,
            subagent_id=selected.subagent_id,
        )
        self.record_lifecycle(
            SubagentStartedEvent,
            delegation_id,
            parent_run_id=context.run_id,
            subagent_id=selected.subagent_id,
        )
        session = selected.session(delegation_id)
        self.sessions[delegation_id] = session
        try:
            result = await self.invoke_subagent(selected, task, delegation_id, child_context)
        except asyncio.CancelledError:
            self.budgets.settle(delegation_id, None)
            self.records[delegation_id] = self.records[delegation_id].model_copy(
                update={"status": DelegationStatus.CANCELLED}
            )
            raise
        except Exception as error:
            self.budgets.settle(delegation_id, None)
            self.records[delegation_id] = self.records[delegation_id].model_copy(
                update={
                    "status": DelegationStatus.FAILED,
                    "error_code": "orchestration.subagent.system_failure",
                    "error_message": str(error),
                }
            )
            raise
        self.record_result(delegation_id, result)
        self.settle_budget(delegation_id, result)
        return result

    async def submit_subagent(self, subagent: str, task: str, context: ToolContext) -> JsonObject:
        """Accept one asynchronous Subagent and return only its delegation identity."""
        self.bind_owner(context)
        selected = self.subagent_for(subagent)
        if not task.strip():
            raise OrchestrationConfigurationError("Subagent task must not be blank.")
        if self.task_group is None:
            raise OrchestrationConfigurationError(
                "Asynchronous Subagent submission requires an open SubagentGroup scope."
            )
        selected.child_limits(context)
        selected.parent_policy(context)
        delegation_id = uuid4().hex
        child_context = self.reserve_budget(delegation_id, selected, context)
        selected.child_limits(child_context)
        selected.parent_policy(child_context)
        self.records[delegation_id] = DelegationRecord(
            delegation_id=delegation_id,
            subagent_id=selected.subagent_id,
            task=task,
            status=DelegationStatus.ACCEPTED,
            parent_run_id=context.run_id,
        )
        self.signals[delegation_id] = asyncio.Event()
        self.record_lifecycle(
            SubagentSubmittedEvent,
            delegation_id,
            parent_run_id=context.run_id,
            subagent_id=selected.subagent_id,
        )
        coroutine = self.run_submitted(delegation_id, selected, task, child_context)
        task_handle = self.task_group.create_task(coroutine)
        self.tasks[delegation_id] = task_handle
        return {"delegation_id": delegation_id, "status": DelegationStatus.ACCEPTED.value}

    async def run_submitted(
        self,
        delegation_id: str,
        subagent: Subagent,
        task: str,
        context: ToolContext,
    ) -> RunOutcome:
        """Execute accepted work and publish exactly one completion event."""
        record = self.records[delegation_id]
        self.records[delegation_id] = record.model_copy(update={"status": DelegationStatus.RUNNING})
        self.record_lifecycle(
            SubagentStartedEvent,
            delegation_id,
            parent_run_id=record.parent_run_id,
            subagent_id=subagent.subagent_id,
        )
        try:
            result = await self.invoke_subagent(subagent, task, delegation_id, context)
        except asyncio.CancelledError:
            self.budgets.settle(delegation_id, None)
            self.record_terminal(delegation_id, DelegationStatus.CANCELLED)
            raise
        except Exception as error:
            self.budgets.settle(delegation_id, None)
            self.record_terminal(
                delegation_id,
                DelegationStatus.FAILED,
                error_code="orchestration.subagent.system_failure",
                error_message=str(error),
            )
            raise
        self.record_result(delegation_id, result)
        self.settle_budget(delegation_id, result)
        return result

    def reserve_budget(self, delegation_id: str, subagent: Subagent, context: ToolContext) -> ToolContext:
        """Atomically reserve a bounded share of one parent Run budget."""
        limits = context.parent_limits
        parent_run_id = context.run_id
        if limits is None or parent_run_id is None:
            return context
        reservation = self.budgets.reserve(
            delegation_id, parent_run_id, limits, subagent.limits.max_model_steps, subagent.limits.max_output_tokens
        )
        if reservation is None:
            raise SubagentBudgetError()
        reserved_limits = ParentRunLimits(
            deadline=limits.deadline,
            remaining_model_steps=reservation.model_steps,
            remaining_output_tokens=reservation.output_tokens,
            tool_concurrency=limits.tool_concurrency,
        )
        return ToolContext.model_validate({**context.model_dump(), "parent_limits": reserved_limits})

    def settle_budget(self, delegation_id: str, result: RunOutcome) -> None:
        """Return only confirmed unused budget after a child reaches a terminal result."""
        reservation = self.budgets.reservations.get(delegation_id)
        if reservation is not None:
            self.budgets.settle(
                delegation_id,
                result.usage.charge(reservation.parent_run_id, delegation_id),
                interrupted=isinstance(result, RunSuspension),
            )

    def resume_context(self, delegation_id: str, context: ToolContext) -> ToolContext:
        """Reuse the original child reservation under the current parent deadline."""
        self.bind_owner(context)
        record = self.records[delegation_id]
        if record.parent_run_id != context.run_id:
            raise OrchestrationStateError("A Subagent cannot resume under a different parent Run.")
        reservation = self.budgets.reservations.get(delegation_id)
        if reservation is None:
            if context.parent_limits is not None:
                raise OrchestrationStateError("A paused Subagent budget reservation is missing.")
            return context
        limits = context.parent_limits
        if limits is None:
            raise OrchestrationStateError("A paused Subagent requires its parent Run limits.")
        reserved_limits = ParentRunLimits(
            deadline=limits.deadline,
            remaining_model_steps=reservation.model_steps,
            remaining_output_tokens=reservation.output_tokens,
            tool_concurrency=limits.tool_concurrency,
        )
        return ToolContext.model_validate({**context.model_dump(), "parent_limits": reserved_limits})

    async def wait_subagent(self, delegation_id: str) -> SubagentCompletedEvent:
        """Wait for one accepted Subagent completion without polling or sleeping."""
        while True:
            event = next(
                (
                    candidate
                    for candidate in reversed(self.events)
                    if isinstance(candidate, SubagentCompletedEvent) and candidate.delegation_id == delegation_id
                ),
                None,
            )
            if event is not None:
                if event.event_id not in self.delivered_event_ids:
                    self.delivered_event_ids.add(event.event_id)
                    record = self.records[delegation_id]
                    self.record_lifecycle(
                        SubagentConsumedEvent,
                        delegation_id,
                        parent_run_id=record.parent_run_id,
                        child_run_id=record.child_run_id,
                        subagent_id=record.subagent_id,
                        completion_event_id=event.event_id,
                    )
                return event
            if delegation_id not in self.records:
                raise DelegationNotFoundError(delegation_id)
            await self.signals[delegation_id].wait()

    def events_since(self, sequence: int = 0) -> tuple[SubagentLifecycleEvent, ...]:
        """Return Subagent lifecycle events in stable sequence order."""
        if sequence < 0:
            raise ValueError("sequence must not be negative")
        return tuple(
            sorted((event for event in self.events if event.sequence > sequence), key=lambda event: event.sequence)
        )

    def snapshot(self) -> SubagentSnapshot:
        """Return JSON-only coordination state for host-controlled recovery."""
        if any(
            record.status in {DelegationStatus.ACCEPTED, DelegationStatus.RUNNING} for record in self.records.values()
        ):
            raise OrchestrationStateError("A SubagentGroup can only checkpoint after its active children settle.")
        budget = None
        if self.owner_run_id is not None:
            paused = tuple(
                key
                for key, value in self.records.items()
                if value.status is DelegationStatus.INTERRUPTED and key in self.budgets.reservations
            )
            budget = self.budgets.checkpoint(self.owner_run_id, paused)
        return SubagentSnapshot(
            owner_run_id=self.owner_run_id,
            budget=budget,
            delegations=tuple(sorted(self.records.values(), key=lambda item: item.delegation_id)),
            events=tuple(sorted(self.events, key=lambda item: item.sequence)),
            delivered_event_ids=tuple(sorted(self.delivered_event_ids)),
        )

    def restore(self, snapshot: SubagentSnapshot) -> None:
        """Restore completed coordination facts after bindings are re-established."""
        if any(
            record.status in {DelegationStatus.ACCEPTED, DelegationStatus.RUNNING} for record in snapshot.delegations
        ):
            raise OrchestrationConfigurationError("Pending Subagent delegations require host-controlled reattachment.")
        if self.owner_run_id is not None and self.owner_run_id != snapshot.owner_run_id:
            raise OrchestrationStateError("A SubagentGroup cannot restore another parent Run's snapshot.")
        if snapshot.budget is not None:
            self.budgets.restore_checkpoint(snapshot.budget)
        self.owner_run_id = snapshot.owner_run_id
        self.records = {record.delegation_id: record for record in snapshot.delegations}
        self.events = list(snapshot.events)
        self.delivered_event_ids = set(snapshot.delivered_event_ids)
        self.sequence = max((event.sequence for event in self.events), default=0)
        self.signals = {record.delegation_id: asyncio.Event() for record in snapshot.delegations}
        for event in self.events:
            if isinstance(event, SubagentCompletedEvent):
                self.signals[event.delegation_id].set()

    async def cancel_paused(self, *, reason: str, context: ToolContext | None = None) -> SubagentSnapshot:
        """Cancel this group's safely paused children and settle their original reservations.

        Args:
            reason: Host-owned cancellation explanation.
            context: Optional identity check against the group's original parent Run.

        Returns:
            Updated coordination facts suitable for persistence or a repeated cancellation.

        Raises:
            OrchestrationStateError: If live children or incompatible terminal facts prevent settlement.
        """
        if not reason.strip():
            raise ValueError("Subagent cancellation requires a reason")
        if context is not None:
            self.bind_owner(context)
        if any(
            record.status in {DelegationStatus.ACCEPTED, DelegationStatus.RUNNING} for record in self.records.values()
        ):
            raise OrchestrationStateError("Subagent cancellation requires settled children or paused checkpoints.")
        for delegation_id, record in tuple(self.records.items()):
            if record.status is not DelegationStatus.INTERRUPTED:
                continue
            value = None if record.result is None else record.result.get("checkpoint")
            if not isinstance(value, dict):
                raise OrchestrationStateError("A paused Subagent is missing its checkpoint.")
            checkpoint = RunCheckpoint.model_validate(value)
            if checkpoint.run_id != record.child_run_id:
                raise OrchestrationStateError("The Subagent checkpoint does not match its original child Run.")
            subagent = self.subagent_for(record.subagent_id)
            session = self.store_session(delegation_id, subagent)
            if session.session_id != checkpoint.session_id:
                raise OrchestrationStateError("The Subagent checkpoint does not match its original Session.")
            snapshot = await session.store.run(session.session_id, checkpoint.run_id)
            if snapshot.receipt is None:
                result = await subagent.runner.cancel_paused(session=session, checkpoint=checkpoint, reason=reason)
            else:
                result = RunResult(snapshot.receipt)
            if result.state is not RunStatus.CANCELLED:
                raise OrchestrationStateError("The paused Subagent already reached a different terminal outcome.")
            self.record_result(delegation_id, result)
            self.settle_budget(delegation_id, result)
        return self.snapshot()

    def bind_owner(self, context: ToolContext) -> None:
        """Bind this group to exactly one explicit parent Run for its lifetime."""
        if context.run_id is None:
            raise OrchestrationConfigurationError("A SubagentGroup requires an explicit parent Run identity.")
        if self.owner_run_id is None:
            self.owner_run_id = context.run_id
        elif self.owner_run_id != context.run_id:
            raise OrchestrationStateError("A SubagentGroup cannot accept work from another parent Run.")

    def record_result(self, delegation_id: str, result: RunOutcome) -> None:
        """Publish a scoped child result or its safely paused checkpoint."""
        if isinstance(result, RunSuspension):
            status = DelegationStatus.INTERRUPTED
            output: JsonObject = {"checkpoint": result.checkpoint.model_dump(mode="json")}
            error_code = None
            error_message = None
        else:
            status = {
                RunStatus.SUCCEEDED: DelegationStatus.SUCCEEDED,
                RunStatus.FAILED: DelegationStatus.FAILED,
                RunStatus.CANCELLED: DelegationStatus.CANCELLED,
            }[result.state]
            output = result.structured_output if result.structured_output is not None else {"text": result.final_output}
            error_code = result.error.code if result.error is not None else None
            error_message = result.error.message if result.error is not None else None
        self.record_terminal(
            delegation_id,
            status,
            child_run_id=result.run_id,
            result=output,
            error_code=error_code,
            error_message=error_message,
        )

    def record_terminal(
        self,
        delegation_id: str,
        status: DelegationStatus,
        *,
        child_run_id: str | None = None,
        result: JsonObject | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        """Record one terminal state and notify waiters."""
        record = self.records[delegation_id]
        updated = record.model_copy(
            update={
                "status": status,
                "child_run_id": child_run_id,
                "result": result,
                "error_code": error_code,
                "error_message": error_message,
            }
        )
        self.records[delegation_id] = updated
        self.sequence += 1
        self.events.append(
            SubagentCompletedEvent(
                event_id=uuid4().hex,
                run_id=record.parent_run_id,
                delegation_id=delegation_id,
                sequence=self.sequence,
                parent_run_id=record.parent_run_id,
                child_run_id=child_run_id,
                subagent_id=record.subagent_id,
                status=status,
                result=result,
                error_code=error_code,
                error_message=error_message,
            )
        )
        self.signals[delegation_id].set()

    def record_lifecycle(
        self,
        event_type: type[SubagentSubmittedEvent] | type[SubagentStartedEvent] | type[SubagentConsumedEvent],
        delegation_id: str,
        *,
        parent_run_id: str | None,
        subagent_id: str,
        child_run_id: str | None = None,
        completion_event_id: str | None = None,
    ) -> None:
        """Append one non-terminal Subagent lifecycle fact."""
        self.sequence += 1
        common: dict[str, object] = {
            "run_id": parent_run_id,
            "delegation_id": delegation_id,
            "sequence": self.sequence,
            "parent_run_id": parent_run_id,
            "child_run_id": child_run_id,
            "subagent_id": subagent_id,
        }
        if event_type is SubagentConsumedEvent:
            if completion_event_id is None:
                raise ValueError("A consumed Subagent event requires its completion event ID.")
            self.events.append(SubagentConsumedEvent(**common, completion_event_id=completion_event_id))
            return
        self.events.append(event_type(**common))

    async def invoke_subagent(
        self, subagent: Subagent, task: str, delegation_id: str, context: ToolContext
    ) -> RunOutcome:
        """Own one child Handle until its terminal result or safe suspension."""
        session = self.store_session(delegation_id, subagent)
        input_id = f"{delegation_id}:input"
        await session.enqueue(InputEnvelope(input_id=input_id, kind="user", source="subagent", content=task))
        handle = await subagent.runner.start(
            subagent.agent,
            session=session,
            invocation_id=delegation_id,
            input_ids=(input_id,),
            input_mode="batch",
            limits=subagent.child_limits(context),
            context=subagent.context_blocks(task),
            handoffs=subagent.handoffs,
            parent_run_id=context.run_id,
            parent_policy=subagent.parent_policy(context),
        )
        async with handle:
            return await handle.result()

    def store_session(self, delegation_id: str, subagent: Subagent) -> Session:
        """Open the isolated Session retained for child pause and resume."""
        session = self.sessions.get(delegation_id)
        if session is None:
            session = subagent.session(delegation_id)
            self.sessions[delegation_id] = session
        return session

    def session_for_interruption(self, interruption: ToolInterruption, subagent: Subagent) -> Session:
        """Resolve a paused child Session from its validated delegation facts."""
        delegation_id = interruption.details.get("delegation_id")
        if not isinstance(delegation_id, str):
            raise OrchestrationStateError("A paused Subagent interruption is missing its delegation ID.")
        record = self.records.get(delegation_id)
        if record is None or record.subagent_id != subagent.subagent_id:
            raise OrchestrationStateError("A paused Subagent delegation mapping is missing or incompatible.")
        if record.status is not DelegationStatus.INTERRUPTED:
            raise OrchestrationStateError("A Subagent delegation can only resume from the interrupted state.")
        return self.store_session(delegation_id, subagent)
