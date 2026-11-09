import asyncio
from abc import ABC, abstractmethod
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

from pleiad.events.contracts import Event
from pleiad.messages.message import AssistantMessage, Message, UserMessage
from pleiad.models.result import ModelUsage
from pleiad.sessions.checkpoint import RunCheckpoint
from pleiad.sessions.errors import SessionConflictError, StaleWriterError
from pleiad.sessions.input import InputEnvelope, InputReceipt, InputRecord
from pleiad.sessions.model_step import ModelStep
from pleiad.sessions.records import (
    EventRecord,
    ModelAttemptRecord,
    OperationRecord,
    ReconcileRecord,
    RunReceipt,
    RunSnapshot,
    RunStart,
    RunStatus,
    SessionMessage,
    SessionPage,
    ToolCallRecord,
)
from pleiad.sessions.recovery import ActionEvidence, ActionRef, ReconcileResult
from pleiad.sessions.state import SessionState, operation_input_matches
from pleiad.sessions.usage import RunUsage


async def complete_owned[T](operation: Coroutine[object, object, T]) -> T:
    """Settle an admitted storage operation before propagating caller cancellation.

    Args:
        operation: The single operation whose transaction or read resources this scope owns.

    Returns:
        The operation's result when the caller remains active.

    Raises:
        asyncio.CancelledError: After the admitted operation settles if its caller was cancelled.
    """
    task = asyncio.create_task(operation)
    cancellation: asyncio.CancelledError | None = None
    while True:
        try:
            result = await asyncio.shield(task)
        except asyncio.CancelledError as error:
            if task.cancelled():
                raise
            cancellation = error
        except BaseException as error:
            if cancellation is not None:
                raise cancellation from error
            raise
        else:
            if cancellation is not None:
                raise cancellation
            return result


def descendant_session_ids(states: tuple[SessionState, ...], parent_run_ids: tuple[str, ...]) -> tuple[str, ...]:
    """Resolve complete recursive Session ownership from a consistent backend snapshot.

    Args:
        states: Validated journals read within one backend transaction.
        parent_run_ids: Root Run identities whose descendants the host owns.

    Returns:
        Sorted identities of recursively owned child Sessions.

    Raises:
        ValueError: If a supplied parent identity is blank.
        SessionConflictError: If a descendant Session also holds an independent Run.
    """
    if any(not value.strip() for value in parent_run_ids):
        raise ValueError("Parent Run identities must not be blank")
    owned_runs = set(parent_run_ids)
    while True:
        children = {
            run.run_id
            for state in states
            for run in state.runs
            if run.parent_run_id in owned_runs and run.run_id not in owned_runs
        }
        if not children:
            break
        owned_runs.update(children)
    descendants = {state.session_id for state in states if any(run.parent_run_id in owned_runs for run in state.runs)}
    for state in states:
        if state.session_id in descendants and any(run.run_id not in owned_runs for run in state.runs):
            raise SessionConflictError("A descendant Session also contains independently owned Runs.")
    return tuple(sorted(descendants))


def updated_state(state: SessionState, **updates: object) -> SessionState:
    """Validate a committed journal revision after applying typed transitions."""
    return SessionState.model_validate({**state.model_dump(), **updates, "revision": state.revision + 1})


def replace_run(state: SessionState, snapshot: RunSnapshot, **updates: object) -> SessionState:
    """Replace exactly one Run in a new validated Session revision."""
    return updated_state(
        state,
        runs=tuple(snapshot if value.run_id == snapshot.run_id else value for value in state.runs),
        **updates,
    )


def run_snapshot(state: SessionState, run_id: str) -> RunSnapshot:
    """Find an accepted Run without inventing a missing record."""
    for value in state.runs:
        if value.run_id == run_id:
            return value
    raise SessionConflictError("The Run does not belong to this Session.")


def fenced_run(state: SessionState, run_id: str, expected_epoch: int, expected_revision: int) -> RunSnapshot:
    """Require the current writer and exact Run revision before any mutation."""
    snapshot = run_snapshot(state, run_id)
    if snapshot.writer_epoch != expected_epoch or snapshot.revision != expected_revision:
        raise StaleWriterError()
    if state.current_run_id != run_id or state.writer_epoch != expected_epoch:
        raise StaleWriterError()
    if snapshot.state not in {RunStatus.RUNNING, RunStatus.WAITING_EVENT}:
        raise SessionConflictError("The Run has no active writer.")
    return snapshot


def operation_update(
    state: SessionState,
    snapshot: RunSnapshot,
    record: OperationRecord,
    envelope: InputEnvelope | None,
) -> tuple[SessionState, RunSnapshot]:
    """Validate ownership and persist related operation, tool, and input facts."""
    record = OperationRecord.model_validate(record.model_dump())
    call = next(
        (value for value in state.tool_calls if value.run_id == record.run_id and value.call_id == record.call_id), None
    )
    if call is None or call.operation_id not in {None, record.operation_id}:
        raise SessionConflictError("A background operation requires its original tool intent.")
    if record.result is not None and record.result.call_id != record.call_id:
        raise SessionConflictError("A background result cannot change its tool-call identity.")
    previous = next((value for value in state.operations if value.operation_id == record.operation_id), None)
    if previous is not None:
        if (previous.run_id, previous.call_id) != (record.run_id, record.call_id):
            raise SessionConflictError("A background operation cannot change ownership.")
        if previous.state in {"succeeded", "failed", "cancelled"} and previous != record:
            raise SessionConflictError("A settled background operation cannot change its outcome.")
        if previous.submission is not None and previous.submission != record.submission:
            raise SessionConflictError("A resolved backend request cannot change or disappear.")
        if previous.backend_receipt is not None and previous.backend_receipt != record.backend_receipt:
            raise SessionConflictError("Concrete backend acceptance cannot change or disappear.")
        if previous.state == "cancel_requested" and record.state in {"prepared", "running"}:
            record = record.model_copy(update={"state": "cancel_requested"})
    if record.backend_receipt is not None and (previous is None or previous.submission is None):
        raise SessionConflictError("Backend acceptance requires its previously committed resolved request.")
    operations = tuple(record if value.operation_id == record.operation_id else value for value in state.operations)
    if previous is None:
        operations = (*operations, record)
    tool_state = "running" if record.state == "cancel_requested" else record.state
    tool = ToolCallRecord.model_validate(
        {
            **call.model_dump(),
            "operation_id": record.operation_id,
            "state": tool_state,
            "result": record.result,
            "result_ref": record.result_ref,
        }
    )
    calls = tuple(
        tool if value.run_id == call.run_id and value.call_id == call.call_id else value for value in state.tool_calls
    )
    inputs = state.inputs
    if envelope is not None:
        if not operation_input_matches(record, envelope):
            raise SessionConflictError("An operation completion input must match its original identity and outcome.")
        existing = next((value for value in inputs if value.envelope.input_id == envelope.input_id), None)
        if existing is not None and existing.payload_digest != envelope.payload_digest():
            raise SessionConflictError("An operation completion input already has different content.")
        if existing is None:
            inputs = (
                *inputs,
                InputRecord(
                    envelope=envelope,
                    payload_digest=envelope.payload_digest(),
                    sequence=len(inputs) + 1,
                    accepted_at=datetime.now(UTC),
                    run_id=record.run_id,
                ),
            )
    if operations == state.operations and calls == state.tool_calls and inputs == state.inputs:
        return state, snapshot
    latest = snapshot.model_copy(
        update={
            "revision": snapshot.revision + 1,
            "input_ids": tuple(value.envelope.input_id for value in inputs if value.run_id == record.run_id),
        }
    )
    return replace_run(state, latest, operations=operations, tool_calls=calls, inputs=inputs), latest


class SessionStore(ABC):
    """Own atomic input, Run, history, and recovery transactions across backends."""

    @abstractmethod
    async def transact[T](self, session_id: str, mutation: Callable[[SessionState], tuple[SessionState, T]]) -> T:
        """Apply a synchronous validated transition atomically to one journal."""

    @abstractmethod
    async def snapshot(self, session_id: str) -> SessionState:
        """Read one complete immutable journal snapshot."""

    @abstractmethod
    async def delete(self, session_id: str) -> None:
        """Remove a stopped Session atomically within the backend."""

    @abstractmethod
    async def descendant_sessions(self, parent_run_ids: tuple[str, ...]) -> tuple[str, ...]:
        """Read Sessions recursively owned by the supplied committed parent Run identities."""

    async def enqueue(self, session_id: str, value: InputEnvelope) -> InputReceipt:
        """Accept one input or return its original idempotent receipt."""
        digest = value.payload_digest()

        def accept(state: SessionState) -> tuple[SessionState, InputReceipt]:
            for record in state.inputs:
                if record.envelope.input_id == value.input_id:
                    if record.payload_digest != digest:
                        raise SessionConflictError("The input ID already has different content.")
                    return state, record.receipt(duplicate=True)
            if value.kind == "operation_result":
                raise SessionConflictError("Operation results must be committed with their original backend outcome.")
            record = InputRecord(
                envelope=value,
                payload_digest=digest,
                sequence=len(state.inputs) + 1,
                accepted_at=datetime.now(UTC),
            )
            return updated_state(state, inputs=(*state.inputs, record)), record.receipt(duplicate=False)

        return await self.transact(session_id, accept)

    async def start(
        self,
        session_id: str,
        *,
        invocation_id: str,
        parameters_digest: str,
        input_ids: tuple[str, ...],
        input_mode: Literal["batch", "live"],
        agent_id: str,
        parent_run_id: str | None = None,
    ) -> RunStart:
        """Atomically claim inputs and create an idempotent invocation writer."""
        if not invocation_id or not agent_id or not input_ids or len(input_ids) != len(set(input_ids)):
            raise SessionConflictError("A Run requires an invocation, Agent, and unique nonempty input IDs.")

        if parent_run_id is not None and not parent_run_id.strip():
            raise SessionConflictError("A child Run requires a nonblank parent identity.")

        def create(state: SessionState) -> tuple[SessionState, RunStart]:
            existing = next((value for value in state.runs if value.invocation_id == invocation_id), None)
            if existing is not None:
                if (
                    existing.parameters_digest != parameters_digest
                    or existing.initial_input_ids != input_ids
                    or existing.input_mode != input_mode
                    or existing.agent_id != agent_id
                    or existing.parent_run_id != parent_run_id
                ):
                    raise SessionConflictError("The invocation ID already has different parameters.")
                return state, RunStart(snapshot=existing, duplicate=True)
            if state.current_run_id is not None:
                raise SessionConflictError("The Session already has an active or paused Run.")
            records = tuple(
                next((value for value in state.inputs if value.envelope.input_id == identity), None)
                for identity in input_ids
            )
            if any(value is None or value.run_id is not None for value in records):
                raise SessionConflictError("Run inputs must be accepted and unclaimed.")
            sequences = tuple(value.sequence for value in records if value is not None)
            if sequences != tuple(sorted(sequences)):
                raise SessionConflictError("Run inputs must follow acceptance order.")
            run_id = uuid4().hex
            epoch = state.writer_epoch + 1
            snapshot = RunSnapshot(
                session_id=session_id,
                run_id=run_id,
                invocation_id=invocation_id,
                parameters_digest=parameters_digest,
                agent_id=agent_id,
                parent_run_id=parent_run_id,
                input_ids=input_ids,
                initial_input_ids=input_ids,
                input_mode=input_mode,
                state=RunStatus.RUNNING,
                writer_epoch=epoch,
                revision=1,
                accepted_at=datetime.now(UTC),
            )
            inputs = tuple(
                value.model_copy(update={"run_id": run_id}) if value.envelope.input_id in input_ids else value
                for value in state.inputs
            )
            return updated_state(
                state, inputs=inputs, runs=(*state.runs, snapshot), current_run_id=run_id, writer_epoch=epoch
            ), RunStart(snapshot=snapshot, duplicate=False)

        return await self.transact(session_id, create)

    async def run_for_invocation(self, session_id: str, invocation_id: str) -> RunSnapshot | None:
        """Recover the Run accepted for an invocation after a lost acknowledgement."""
        return next(
            (value for value in (await self.snapshot(session_id)).runs if value.invocation_id == invocation_id), None
        )

    async def run(self, session_id: str, run_id: str) -> RunSnapshot:
        """Read a committed Run snapshot by its SDK identity."""
        return run_snapshot(await self.snapshot(session_id), run_id)

    async def history(self, session_id: str) -> tuple[ModelStep, ...]:
        """Select only protocol-complete model history for another model request."""
        return tuple(value for value in (await self.snapshot(session_id)).steps if value.completed)

    async def active(self, session_id: str, run_id: str) -> ModelStep | None:
        """Read the current uncompleted step owned by the requested Run."""
        return next(
            (
                value
                for value in (await self.snapshot(session_id)).steps
                if value.run_id == run_id and value.state == "active"
            ),
            None,
        )

    async def begin_step(
        self,
        session_id: str,
        *,
        run_id: str,
        expected_epoch: int,
        expected_revision: int,
        agent_id: str,
        messages: tuple[Message, ...] = (),
        consume_inputs: bool = True,
    ) -> tuple[ModelStep, RunSnapshot, tuple[InputRecord, ...]]:
        """Commit ordered input consumption and a new model step together."""

        def begin(state: SessionState) -> tuple[SessionState, tuple[ModelStep, RunSnapshot, tuple[InputRecord, ...]]]:
            snapshot = fenced_run(state, run_id, expected_epoch, expected_revision)
            if any(value.state == "active" for value in state.steps):
                raise SessionConflictError("The previous model step must be settled before another begins.")
            if any(not isinstance(value, UserMessage) for value in messages):
                raise SessionConflictError("Initial step messages must contain User inputs only.")
            selected: list[InputRecord] = []
            for value in state.inputs:
                if not consume_inputs or value.step_id is not None:
                    continue
                operation = next(
                    (item for item in state.operations if item.operation_id == value.envelope.causation_id), None
                )
                owned_operation = (
                    value.envelope.kind == "operation_result" and operation is not None and operation.run_id == run_id
                )
                external_input = value.envelope.kind != "operation_result"
                if (value.run_id == run_id and (external_input or owned_operation)) or (
                    value.run_id is None and (owned_operation or (external_input and snapshot.input_mode == "live"))
                ):
                    selected.append(value)
            message_ids = {value.message.message_id for value in state.messages}
            initial = (*tuple(value.envelope.message() for value in selected), *messages)
            if any(value.message_id in message_ids for value in initial):
                raise SessionConflictError("A step message ID already belongs to Session history.")
            step = ModelStep(
                step_id=uuid4().hex,
                run_id=run_id,
                sequence=len(state.steps) + 1,
                run_sequence=sum(value.run_id == run_id for value in state.steps) + 1,
                agent_id=agent_id,
                input_ids=tuple(value.envelope.input_id for value in selected),
                messages=initial,
            )
            selected_ids = set(step.input_ids)
            inputs = tuple(
                value.model_copy(update={"run_id": run_id, "step_id": step.step_id})
                if value.envelope.input_id in selected_ids
                else value
                for value in state.inputs
            )
            updated = snapshot.model_copy(
                update={
                    "revision": snapshot.revision + 1,
                    "input_ids": tuple(value.envelope.input_id for value in inputs if value.run_id == run_id),
                }
            )
            entries = tuple(
                SessionMessage(
                    sequence=len(state.messages) + index + 1, run_id=run_id, step_id=step.step_id, message=value
                )
                for index, value in enumerate(initial)
            )
            return replace_run(
                state, updated, inputs=inputs, steps=(*state.steps, step), messages=(*state.messages, *entries)
            ), (step, updated, tuple(selected))

        return await self.transact(session_id, begin)

    async def change_step(
        self,
        session_id: str,
        *,
        run_id: str,
        expected_epoch: int,
        expected_revision: int,
        step_id: str,
        messages: tuple[Message, ...] = (),
        action: Literal["append", "complete", "abort"],
    ) -> tuple[ModelStep, RunSnapshot]:
        """Append, complete, or retain an aborted step under writer fencing."""

        def change(state: SessionState) -> tuple[SessionState, tuple[ModelStep, RunSnapshot]]:
            snapshot = fenced_run(state, run_id, expected_epoch, expected_revision)
            step = next((value for value in state.steps if value.step_id == step_id and value.run_id == run_id), None)
            if step is None:
                raise SessionConflictError("The model step does not belong to this Run.")
            if action == "abort":
                if step.state == "completed":
                    raise SessionConflictError("A completed model step cannot be aborted.")
                updated = step.model_copy(update={"state": "aborted"})
            elif action == "complete":
                updated = step.complete(messages)
            else:
                updated = step.append(messages)
            other_ids = {value.message.message_id for value in state.messages if value.step_id != step_id}
            if any(value.message_id in other_ids for value in updated.messages):
                raise SessionConflictError("The message ID belongs to another model step.")
            suffix = updated.messages[len(step.messages) :]
            entries = tuple(
                SessionMessage(sequence=len(state.messages) + index + 1, run_id=run_id, step_id=step_id, message=value)
                for index, value in enumerate(suffix)
            )
            latest = snapshot.model_copy(update={"revision": snapshot.revision + 1})
            return replace_run(
                state,
                latest,
                steps=tuple(updated if value.step_id == step_id else value for value in state.steps),
                messages=(*state.messages, *entries),
            ), (updated, latest)

        return await self.transact(session_id, change)

    async def save_checkpoint(
        self, session_id: str, *, checkpoint: RunCheckpoint, expected_epoch: int, expected_revision: int
    ) -> RunSnapshot:
        """Pause a Run only after persisting the exact checkpoint position."""

        def save(state: SessionState) -> tuple[SessionState, RunSnapshot]:
            snapshot = fenced_run(state, checkpoint.run_id, expected_epoch, expected_revision)
            if checkpoint.session_id != session_id:
                raise SessionConflictError("The checkpoint belongs to another Session.")
            if checkpoint.parent_run_id != snapshot.parent_run_id:
                raise SessionConflictError("The checkpoint belongs to another parent Run.")
            active = next(
                (value for value in state.steps if value.run_id == snapshot.run_id and value.state == "active"), None
            )
            if checkpoint.step_id != (active.step_id if active is not None else None):
                raise SessionConflictError("The checkpoint step does not match persisted history.")
            if any(
                value.run_id == snapshot.run_id and value.state in {"prepared", "running", "cancel_requested"}
                for value in state.operations
            ):
                raise SessionConflictError("A Run cannot pause with unsettled background operations.")
            if any(value.completed_at is None for value in snapshot.model_attempts):
                raise SessionConflictError("A Run cannot pause with an uncompleted model admission.")
            revision = snapshot.revision + 1
            saved = checkpoint.model_copy(update={"revision": revision, "writer_epoch": expected_epoch})
            latest = snapshot.model_copy(
                update={"revision": revision, "state": RunStatus.PAUSED, "checkpoint": saved, "usage": saved.usage}
            )
            return replace_run(state, latest), latest

        return await self.transact(session_id, save)

    async def resume(self, session_id: str, checkpoint: RunCheckpoint) -> RunSnapshot:
        """Acquire a new epoch only for the current unmodified paused checkpoint."""

        def resume_run(state: SessionState) -> tuple[SessionState, RunSnapshot]:
            snapshot = run_snapshot(state, checkpoint.run_id)
            if (
                snapshot.state is not RunStatus.PAUSED
                or snapshot.checkpoint != checkpoint
                or state.current_run_id != checkpoint.run_id
            ):
                raise SessionConflictError("The paused checkpoint has already changed or been settled.")
            epoch = state.writer_epoch + 1
            latest = snapshot.model_copy(
                update={"revision": snapshot.revision + 1, "writer_epoch": epoch, "state": RunStatus.RUNNING}
            )
            return replace_run(state, latest, writer_epoch=epoch), latest

        return await self.transact(session_id, resume_run)

    async def cancel_paused(self, session_id: str, *, checkpoint: RunCheckpoint, reason: str) -> RunSnapshot:
        """Cancel a paused position atomically against a concurrent resume."""
        if not reason.strip():
            raise ValueError("paused cancellation requires a reason")

        def cancel(state: SessionState) -> tuple[SessionState, RunSnapshot]:
            if checkpoint.session_id != session_id:
                raise SessionConflictError("The checkpoint belongs to another Session.")
            snapshot = run_snapshot(state, checkpoint.run_id)
            if snapshot.state is RunStatus.CANCELLED and snapshot.receipt is not None:
                if snapshot.receipt.reason != reason:
                    raise SessionConflictError("The Run was already cancelled with a different reason.")
                return state, snapshot
            if (
                checkpoint.session_id != session_id
                or snapshot.state is not RunStatus.PAUSED
                or snapshot.checkpoint != checkpoint
                or snapshot.revision != checkpoint.revision
                or snapshot.writer_epoch != checkpoint.writer_epoch
                or state.current_run_id != snapshot.run_id
            ):
                raise SessionConflictError("The paused checkpoint changed before cancellation.")
            epoch = state.writer_epoch + 1
            receipt = RunReceipt(
                run_id=snapshot.run_id,
                input_ids=snapshot.input_ids,
                state="cancelled",
                reason=reason,
                usage=snapshot.usage,
                final_sequence=len(state.messages),
                completed_at=datetime.now(UTC),
            )
            latest = snapshot.model_copy(
                update={
                    "state": RunStatus.CANCELLED,
                    "revision": snapshot.revision + 1,
                    "writer_epoch": epoch,
                    "receipt": receipt,
                    "checkpoint": None,
                }
            )
            steps = tuple(
                value.model_copy(update={"state": "aborted"})
                if value.run_id == snapshot.run_id and value.state == "active"
                else value
                for value in state.steps
            )
            calls = tuple(
                value.model_copy(update={"state": "cancelled"})
                if value.run_id == snapshot.run_id and value.state in {"prepared", "running"}
                else value
                for value in state.tool_calls
            )
            return replace_run(
                state,
                latest,
                steps=steps,
                tool_calls=calls,
                current_run_id=None,
                writer_epoch=epoch,
            ), latest

        return await self.transact(session_id, cancel)

    async def finish(
        self, session_id: str, *, receipt: RunReceipt, expected_epoch: int, expected_revision: int, paused: bool = False
    ) -> RunSnapshot:
        """Persist a terminal receipt before releasing the Session for another Run."""

        def finish_run(state: SessionState) -> tuple[SessionState, RunSnapshot]:
            if paused:
                snapshot = run_snapshot(state, receipt.run_id)
                if (
                    snapshot.state is not RunStatus.PAUSED
                    or snapshot.writer_epoch != expected_epoch
                    or snapshot.revision != expected_revision
                ):
                    raise StaleWriterError()
            else:
                snapshot = fenced_run(state, receipt.run_id, expected_epoch, expected_revision)
            if any(value.run_id == receipt.run_id and value.state == "active" for value in state.steps):
                raise SessionConflictError("The active step must be completed or aborted before a Run finishes.")
            if any(
                value.run_id == receipt.run_id and value.state in {"prepared", "running", "cancel_requested"}
                for value in state.operations
            ):
                raise SessionConflictError("The Run still owns unsettled background operations.")
            saved = receipt.model_copy(update={"input_ids": snapshot.input_ids, "final_sequence": len(state.messages)})
            latest = snapshot.model_copy(
                update={
                    "revision": snapshot.revision + 1,
                    "state": RunStatus(saved.state),
                    "receipt": saved,
                    "usage": saved.usage,
                    "checkpoint": None,
                }
            )
            return replace_run(state, latest, current_run_id=None), latest

        return await self.transact(session_id, finish_run)

    async def record_event(
        self, session_id: str, *, run_id: str, expected_epoch: int, expected_revision: int, event: Event
    ) -> tuple[EventRecord, RunSnapshot]:
        """Append a replayable event after its associated facts have committed."""

        def record(state: SessionState) -> tuple[SessionState, tuple[EventRecord, RunSnapshot]]:
            snapshot = run_snapshot(state, run_id)
            if snapshot.writer_epoch != expected_epoch or snapshot.revision != expected_revision:
                raise StaleWriterError()
            previous = next((value for value in state.events if value.event_id == event.event_id), None)
            data = event.model_dump(mode="json")
            if previous is not None:
                if previous.run_id != run_id or previous.data != data:
                    raise SessionConflictError("The event ID already has different content.")
                return state, (previous, snapshot)
            if event.run_id != run_id:
                raise SessionConflictError("The event belongs to another Run.")
            value = EventRecord(
                sequence=sum(item.run_id == run_id for item in state.events) + 1,
                event_id=event.event_id,
                run_id=run_id,
                kind=event.kind,
                timestamp=event.timestamp,
                data=data,
            )
            return updated_state(state, events=(*state.events, value)), (value, snapshot)

        return await self.transact(session_id, record)

    async def read_events(
        self, session_id: str, run_id: str, *, after_sequence: int = 0, limit: int = 256
    ) -> tuple[EventRecord, ...]:
        """Replay committed Run events after an exclusive cursor."""
        if after_sequence < 0 or not 1 <= limit <= 1000:
            raise ValueError("events require a nonnegative cursor and limit between 1 and 1000")
        return tuple(
            value
            for value in (await self.snapshot(session_id)).events
            if value.run_id == run_id and value.sequence > after_sequence
        )[:limit]

    async def read(self, session_id: str, *, after_sequence: int = 0, limit: int = 50) -> SessionPage:
        """Read bounded message history including retained aborted observations."""
        if after_sequence < 0 or not 1 <= limit <= 1000:
            raise ValueError("history requires a nonnegative cursor and limit between 1 and 1000")
        state = await self.snapshot(session_id)
        messages = tuple(value for value in state.messages if value.sequence > after_sequence)[:limit]
        return SessionPage(
            session_id=session_id,
            messages=messages,
            next_sequence=messages[-1].sequence if messages else after_sequence,
            last_sequence=len(state.messages),
            pending_inputs=tuple(value for value in state.inputs if value.run_id is None),
        )

    async def save_tool_call(
        self, session_id: str, *, record: ToolCallRecord, expected_epoch: int, expected_revision: int
    ) -> RunSnapshot:
        """Persist a tool intent or confirmed result under the current writer."""

        def save(state: SessionState) -> tuple[SessionState, RunSnapshot]:
            snapshot = fenced_run(state, record.run_id, expected_epoch, expected_revision)
            step = next(
                (value for value in state.steps if value.step_id == record.step_id and value.run_id == record.run_id),
                None,
            )
            if step is None:
                raise SessionConflictError("The tool call must identify its original model step.")
            if not any(
                isinstance(value, AssistantMessage) and any(call.id == record.call_id for call in value.tool_calls)
                for value in step.messages
            ):
                raise SessionConflictError("The tool intent must match a persisted Assistant declaration.")
            previous = next(
                (
                    value
                    for value in state.tool_calls
                    if value.run_id == record.run_id and value.call_id == record.call_id
                ),
                None,
            )
            if previous is not None and (
                previous.step_id,
                previous.tool_code,
                previous.binding_digest,
                previous.arguments_digest,
            ) != (record.step_id, record.tool_code, record.binding_digest, record.arguments_digest):
                raise SessionConflictError("A tool intent cannot change its binding or arguments.")
            if previous is not None and previous.state in {"succeeded", "failed", "cancelled"} and previous != record:
                raise SessionConflictError("A settled tool intent cannot change its outcome.")
            calls = tuple(
                record if value.run_id == record.run_id and value.call_id == record.call_id else value
                for value in state.tool_calls
            )
            if previous is None:
                calls = (*calls, record)
            latest = snapshot.model_copy(update={"revision": snapshot.revision + 1})
            return replace_run(state, latest, tool_calls=calls), latest

        return await self.transact(session_id, save)

    async def tool_calls(self, session_id: str, run_id: str) -> tuple[ToolCallRecord, ...]:
        """Read original tool intentions and their latest confirmed outcomes."""
        return tuple(value for value in (await self.snapshot(session_id)).tool_calls if value.run_id == run_id)

    async def save_operation(
        self, session_id: str, *, record: OperationRecord, expected_epoch: int, expected_revision: int
    ) -> RunSnapshot:
        """Save an operation intention or acceptance before publishing started."""
        if record.state in {"succeeded", "failed", "cancelled"}:
            raise SessionConflictError("Terminal operations require the atomic completion contract.")

        def save(state: SessionState) -> tuple[SessionState, RunSnapshot]:
            snapshot = fenced_run(state, record.run_id, expected_epoch, expected_revision)
            return operation_update(state, snapshot, record, None)

        return await self.transact(session_id, save)

    async def request_operation_cancel(
        self, session_id: str, *, run_id: str, operation_id: str, expected_epoch: int, expected_revision: int
    ) -> RunSnapshot:
        """Record cancellation intent atomically while preserving already settled facts."""

        def request(state: SessionState) -> tuple[SessionState, RunSnapshot]:
            snapshot = fenced_run(state, run_id, expected_epoch, expected_revision)
            operation = next(
                (value for value in state.operations if value.run_id == run_id and value.operation_id == operation_id),
                None,
            )
            if operation is None:
                raise SessionConflictError("Cancellation must identify an owned operation")
            if operation.state in {"succeeded", "failed", "cancelled", "unknown"}:
                return state, snapshot
            return operation_update(state, snapshot, operation.model_copy(update={"state": "cancel_requested"}), None)

        return await self.transact(session_id, request)

    async def complete_operation(
        self,
        session_id: str,
        *,
        record: OperationRecord,
        envelope: InputEnvelope | None,
        expected_epoch: int,
        expected_revision: int,
    ) -> RunSnapshot:
        """Commit a backend outcome, its tool fact, and optional input atomically."""
        if record.state not in {"succeeded", "failed", "cancelled", "unknown"}:
            raise SessionConflictError("An operation completion requires a settled or unknown outcome.")

        def complete(state: SessionState) -> tuple[SessionState, RunSnapshot]:
            snapshot = fenced_run(state, record.run_id, expected_epoch, expected_revision)
            return operation_update(state, snapshot, record, envelope)

        return await self.transact(session_id, complete)

    async def operations(self, session_id: str, run_id: str) -> tuple[OperationRecord, ...]:
        """Read backend outcomes by their original Run ownership."""
        return tuple(value for value in (await self.snapshot(session_id)).operations if value.run_id == run_id)

    async def admit_model(
        self,
        session_id: str,
        *,
        run_id: str,
        attempt_id: str,
        step_id: str,
        provider: str,
        model: str,
        request_digest: str,
        max_output_tokens: int,
        deadline: datetime,
        usage: RunUsage,
        expected_epoch: int,
        expected_revision: int,
    ) -> tuple[ModelAttemptRecord | None, RunSnapshot]:
        """Persist request authority and conservative usage before external admission."""

        def admit(state: SessionState) -> tuple[SessionState, tuple[ModelAttemptRecord | None, RunSnapshot]]:
            snapshot = fenced_run(state, run_id, expected_epoch, expected_revision)
            previous = next((value for value in snapshot.model_attempts if value.attempt_id == attempt_id), None)
            if previous is not None:
                if (
                    previous.step_id,
                    previous.provider,
                    previous.model,
                    previous.request_digest,
                    previous.max_output_tokens,
                    previous.deadline,
                    previous.usage_before,
                ) != (step_id, provider, model, request_digest, max_output_tokens, deadline, usage):
                    raise SessionConflictError("A model attempt cannot change its request or authority.")
                return state, (previous, snapshot)
            active = next((value for value in state.steps if value.step_id == step_id), None)
            if active is None or active.run_id != run_id or active.state != "active":
                raise SessionConflictError("A model attempt requires its original active step.")
            if any(value.step_id == step_id or value.completed_at is None for value in snapshot.model_attempts):
                raise SessionConflictError("A Run cannot admit a second model attempt for its pending step.")
            if (
                usage.model_calls != snapshot.usage.model_calls
                or usage.budget_output_tokens - usage.child_budget_output_tokens
                != snapshot.usage.budget_output_tokens - snapshot.usage.child_budget_output_tokens
                or usage.tool_calls < snapshot.usage.tool_calls
            ):
                raise SessionConflictError("Model admission must retain already committed usage.")
            now = datetime.now(UTC)
            attempt = (
                None
                if deadline <= now
                else ModelAttemptRecord(
                    attempt_id=attempt_id,
                    step_id=step_id,
                    provider=provider,
                    model=model,
                    request_digest=request_digest,
                    max_output_tokens=max_output_tokens,
                    deadline=deadline,
                    usage_before=usage,
                    admitted_at=now,
                )
            )
            latest = snapshot.model_copy(
                update={
                    "revision": snapshot.revision + 1,
                    "usage": usage if attempt is None else usage.admit_model(max_output_tokens),
                    "model_attempts": snapshot.model_attempts
                    if attempt is None
                    else (*snapshot.model_attempts, attempt),
                }
            )
            return replace_run(state, latest), (attempt, latest)

        return await self.transact(session_id, admit)

    async def complete_model(
        self,
        session_id: str,
        *,
        run_id: str,
        attempt_id: str,
        usage: ModelUsage,
        response_id: str | None,
        expected_epoch: int,
        expected_revision: int,
    ) -> RunSnapshot:
        """Replace one admitted allowance with its actual completed provider usage."""

        def complete(state: SessionState) -> tuple[SessionState, RunSnapshot]:
            snapshot = fenced_run(state, run_id, expected_epoch, expected_revision)
            attempt = next((value for value in snapshot.model_attempts if value.attempt_id == attempt_id), None)
            if attempt is None:
                raise SessionConflictError("A completed model result requires its admitted attempt.")
            if attempt.completed_at is not None:
                if attempt.result_usage != usage or attempt.response_id != response_id:
                    raise SessionConflictError("A model attempt already has different completed usage.")
                return state, snapshot
            measured = attempt.usage_before.add_model(usage, attempt.max_output_tokens)
            measured = measured.model_copy(
                update={
                    "budget_output_tokens": measured.budget_output_tokens
                    - measured.child_budget_output_tokens
                    + snapshot.usage.child_budget_output_tokens,
                    "child_model_calls": snapshot.usage.child_model_calls,
                    "child_budget_output_tokens": snapshot.usage.child_budget_output_tokens,
                    "tool_calls": snapshot.usage.tool_calls,
                }
            )
            completed = attempt.model_copy(
                update={"completed_at": datetime.now(UTC), "result_usage": usage, "response_id": response_id}
            )
            latest = snapshot.model_copy(
                update={
                    "revision": snapshot.revision + 1,
                    "usage": measured,
                    "model_attempts": tuple(
                        completed if value.attempt_id == attempt_id else value for value in snapshot.model_attempts
                    ),
                }
            )
            return replace_run(state, latest), latest

        return await self.transact(session_id, complete)

    async def save_usage(
        self, session_id: str, *, run_id: str, usage: RunUsage, expected_epoch: int, expected_revision: int
    ) -> RunSnapshot:
        """Persist accepted usage under the same Run writer and revision."""

        def save(state: SessionState) -> tuple[SessionState, RunSnapshot]:
            snapshot = fenced_run(state, run_id, expected_epoch, expected_revision)
            if (
                usage.model_calls < snapshot.usage.model_calls
                or usage.budget_output_tokens < snapshot.usage.budget_output_tokens
                or usage.tool_calls < snapshot.usage.tool_calls
            ):
                raise SessionConflictError("Committed Run usage cannot decrease.")
            latest = snapshot.model_copy(update={"revision": snapshot.revision + 1, "usage": usage})
            return replace_run(state, latest), latest

        return await self.transact(session_id, save)

    async def waiting_event(
        self, session_id: str, *, run_id: str, waiting: bool, expected_epoch: int, expected_revision: int
    ) -> RunSnapshot:
        """Record whether the active Run is waiting on its owned operations."""

        def change(state: SessionState) -> tuple[SessionState, RunSnapshot]:
            snapshot = fenced_run(state, run_id, expected_epoch, expected_revision)
            status = RunStatus.WAITING_EVENT if waiting else RunStatus.RUNNING
            if snapshot.state is status:
                return state, snapshot
            if waiting and not any(
                value.run_id == run_id and value.state in {"prepared", "running", "cancel_requested"}
                for value in state.operations
            ):
                raise SessionConflictError("Waiting for an event requires a Run-owned background operation.")
            latest = snapshot.model_copy(update={"revision": snapshot.revision + 1, "state": status})
            return replace_run(state, latest), latest

        return await self.transact(session_id, change)

    async def reconcile(
        self,
        session_id: str,
        *,
        run_id: str,
        request_id: str,
        parameters_digest: str,
        expected_epoch: int,
        expected_revision: int,
        evidence: tuple[ActionEvidence, ...] = (),
    ) -> ReconcileResult:
        """Fence a lost owner and atomically reconcile only supplied local evidence."""

        def reconcile_run(state: SessionState) -> tuple[SessionState, ReconcileResult]:
            previous = next((value for value in state.reconciliations if value.request_id == request_id), None)
            if previous is not None:
                if previous.run_id != run_id or previous.parameters_digest != parameters_digest:
                    raise SessionConflictError("The reconciliation ID already has different parameters.")
                result = ReconcileResult.model_validate(previous.result)
                return state, result.model_copy(update={"duplicate": True})
            snapshot = run_snapshot(state, run_id)
            if snapshot.writer_epoch != expected_epoch or snapshot.revision != expected_revision:
                raise StaleWriterError()
            terminal = snapshot.receipt is not None
            if not terminal and state.current_run_id != run_id:
                raise StaleWriterError()
            if len({value.evidence_id for value in evidence}) != len(evidence):
                raise SessionConflictError("Reconciliation evidence IDs must be unique.")
            calls = list(state.tool_calls)
            operations = list(state.operations)
            for value in evidence:
                index = next(
                    (
                        index
                        for index, call in enumerate(calls)
                        if call.run_id == run_id and call.call_id == value.action.call_id
                    ),
                    None,
                )
                if value.action.run_id != run_id or index is None:
                    raise SessionConflictError("Evidence must identify an original tool intent.")
                call = calls[index]
                if (call.binding_digest, call.arguments_digest, call.operation_id) != (
                    value.binding_digest,
                    value.arguments_digest,
                    value.action.operation_id,
                ):
                    raise SessionConflictError("Evidence binding or arguments differ from the original intent.")
                if value.result is not None and value.result.call_id != call.call_id:
                    raise SessionConflictError("Evidence result identity differs from its tool intent.")
                if value.acceptance is not None and value.action.operation_id != value.acceptance.operation_id:
                    raise SessionConflictError("Evidence acceptance must retain its operation identity.")
                observed = "cancelled" if value.observed_state == "not_started" else value.observed_state
                if call.state in {"succeeded", "failed", "cancelled"} and call.state != observed:
                    raise SessionConflictError("Evidence conflicts with an already confirmed tool outcome.")
                calls[index] = ToolCallRecord.model_validate(
                    {
                        **call.model_dump(),
                        "state": observed,
                        "result": value.result,
                        "result_ref": value.result_ref,
                        "backend_evidence_ref": value.backend_evidence_ref,
                    }
                )
                if value.action.operation_id is not None:
                    operation_index = next(
                        (
                            index
                            for index, operation in enumerate(operations)
                            if operation.operation_id == value.action.operation_id
                            and operation.run_id == run_id
                            and operation.call_id == call.call_id
                        ),
                        None,
                    )
                    if operation_index is None:
                        raise SessionConflictError("Evidence operation must belong to its original tool call.")
                    operation = operations[operation_index]
                    acceptance = operation.backend_receipt if value.acceptance is None else value.acceptance
                    if acceptance is not None and (
                        operation.submission is None or operation.submission.request_digest != acceptance.request_digest
                    ):
                        raise SessionConflictError("Evidence acceptance differs from the prepared backend request.")
                    if operation.backend_receipt is not None and acceptance != operation.backend_receipt:
                        raise SessionConflictError("Evidence cannot replace concrete backend acceptance.")
                    if value.observed_state == "not_started" and acceptance is not None:
                        raise SessionConflictError("An accepted operation cannot be reconciled as never admitted.")
                    operations[operation_index] = OperationRecord.model_validate(
                        {
                            **operation.model_dump(),
                            "backend_receipt": acceptance,
                            "state": observed,
                            "result": value.result,
                            "result_ref": value.result_ref,
                            "ended_at": value.observed_at if observed in {"succeeded", "failed", "cancelled"} else None,
                        }
                    )
            checkpoint = snapshot.checkpoint
            pending_calls = set() if checkpoint is None else {value.call_id for value in checkpoint.interruptions}
            unsettled = tuple(
                ActionRef(run_id=run_id, call_id=value.call_id, operation_id=value.operation_id)
                for value in calls
                if value.run_id == run_id
                and (
                    value.state in {"running", "unknown"}
                    or (
                        value.state == "prepared"
                        and (value.call_id not in pending_calls or value.operation_id is not None)
                    )
                    or (
                        value.call_id in pending_calls
                        and value.state in {"succeeded", "failed", "cancelled"}
                        and value.result is None
                    )
                )
            )
            epoch = snapshot.writer_epoch if terminal else state.writer_epoch + 1
            revision = snapshot.revision + 1
            steps = state.steps
            active_step_id = next(
                (value.step_id for value in steps if value.run_id == run_id and value.state == "active"), None
            )
            if terminal:
                disposition: Literal["terminal", "paused", "interrupted"] = "terminal"
                latest = snapshot.model_copy(update={"revision": revision})
                current = state.current_run_id
            elif (
                checkpoint is not None
                and checkpoint.step_id == active_step_id
                and all(value.completed_at is not None for value in snapshot.model_attempts)
                and not unsettled
            ):
                disposition = "paused"
                active_step = next((value for value in steps if value.step_id == checkpoint.step_id), None)
                declared = (
                    ()
                    if active_step is None
                    else tuple(
                        call.id
                        for message in active_step.messages
                        if isinstance(message, AssistantMessage)
                        for call in message.tool_calls
                    )
                )
                results = {value.call_id: value for value in checkpoint.tool_results}
                for call in calls:
                    if (
                        call.run_id == run_id
                        and call.step_id == checkpoint.step_id
                        and call.call_id in declared
                        and call.state in {"succeeded", "failed", "cancelled"}
                        and call.result is not None
                    ):
                        results[call.call_id] = call.result
                pending = tuple(value for value in checkpoint.interruptions if value.call_id not in results)
                charges = {value.charge_id: value for value in checkpoint.usage_charges}
                for tool_result in results.values():
                    for charge in tool_result.usage_charges:
                        prior_charge = charges.get(charge.charge_id)
                        if charge.parent_run_id != run_id:
                            raise SessionConflictError("Reconciled child usage must retain its parent charge.")
                        if prior_charge is not None:
                            if (
                                charge.model_steps <= prior_charge.model_steps
                                and charge.output_tokens <= prior_charge.output_tokens
                            ):
                                continue
                            if (
                                charge.model_steps < prior_charge.model_steps
                                or charge.output_tokens < prior_charge.output_tokens
                            ):
                                raise SessionConflictError("Reconciled child usage has incompatible cumulative facts.")
                        charges[charge.charge_id] = charge
                usage = snapshot.usage.model_copy(
                    update={
                        "tool_calls": max(
                            snapshot.usage.tool_calls,
                            checkpoint.usage.tool_calls + len(results) - len(checkpoint.tool_results),
                        )
                    }
                )
                usage = usage.with_children(tuple(charges.values()))
                saved = checkpoint.model_copy(
                    update={
                        "revision": revision,
                        "writer_epoch": epoch,
                        "interruptions": pending,
                        "tool_results": tuple(results[identity] for identity in declared if identity in results),
                        "usage": usage,
                        "usage_charges": tuple(charges.values()),
                    }
                )
                latest = snapshot.model_copy(
                    update={
                        "revision": revision,
                        "writer_epoch": epoch,
                        "state": RunStatus.PAUSED,
                        "checkpoint": saved,
                        "usage": usage,
                    }
                )
                current = run_id
            else:
                disposition = "interrupted"
                receipt = RunReceipt(
                    run_id=run_id,
                    input_ids=snapshot.input_ids,
                    state="failed",
                    reason="interrupted",
                    usage=snapshot.usage,
                    completed_at=datetime.now(UTC),
                    final_sequence=len(state.messages),
                )
                latest = snapshot.model_copy(
                    update={
                        "revision": revision,
                        "writer_epoch": epoch,
                        "state": RunStatus.FAILED,
                        "receipt": receipt,
                        "checkpoint": None,
                    }
                )
                steps = tuple(
                    value.model_copy(update={"state": "aborted"})
                    if value.run_id == run_id and value.state == "active"
                    else value
                    for value in state.steps
                )
                current = None
            result = ReconcileResult(
                disposition=disposition, snapshot=latest, unsettled_actions=unsettled, duplicate=False
            )
            record = ReconcileRecord(
                request_id=request_id,
                run_id=run_id,
                parameters_digest=parameters_digest,
                evidence_ids=tuple(value.evidence_id for value in evidence),
                snapshot=latest,
                result=result.model_dump(mode="json"),
                accepted_at=datetime.now(UTC),
            )
            return replace_run(
                state,
                latest,
                tool_calls=tuple(calls),
                operations=tuple(operations),
                steps=steps,
                current_run_id=current,
                writer_epoch=state.writer_epoch if terminal else epoch,
                reconciliations=(*state.reconciliations, record),
            ), result

        return await self.transact(session_id, reconcile_run)
