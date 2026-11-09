import asyncio
import hashlib
import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event as ThreadEvent
from typing import Literal

import pytest

from pleiad.context.budget import ContextBudget
from pleiad.contracts import CapabilityGrants
from pleiad.events.contracts import Event
from pleiad.messages.message import AssistantMessage, ToolCall, ToolMessage
from pleiad.messages.results import OperationResultReference
from pleiad.models.result import ModelUsage
from pleiad.sessions.backends.in_memory import InMemorySessionStore
from pleiad.sessions.backends.sqlite import SqliteSessionStore
from pleiad.sessions.checkpoint import RunCheckpoint
from pleiad.sessions.errors import (
    ModelStepInvalidError,
    SessionConflictError,
    SessionDataCorruptedError,
    StaleWriterError,
)
from pleiad.sessions.input import InputEnvelope
from pleiad.sessions.records import OperationRecord, RunReceipt, RunStatus, ToolCallRecord
from pleiad.sessions.recovery import ActionEvidence, ActionRef
from pleiad.sessions.session import Session
from pleiad.sessions.state import SessionState
from pleiad.sessions.store import SessionStore
from pleiad.sessions.usage import RunUsage
from pleiad.sessions.writer import SessionWriter
from pleiad.tools.contracts import ToolAcceptance, ToolResult, ToolSubmission
from pleiad.tools.interruption import ToolInterruption


@pytest.fixture(params=["memory", "sqlite"])
def store(request: pytest.FixtureRequest, tmp_path: Path) -> SessionStore:
    return InMemorySessionStore() if request.param == "memory" else SqliteSessionStore(tmp_path / "sessions.sqlite3")


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def envelope(identity: str, content: str = "question") -> InputEnvelope:
    return InputEnvelope(input_id=identity, kind="user", source="test.user", content=content)


async def start_writer(store: SessionStore, *, mode: Literal["batch", "live"] = "batch") -> SessionWriter:
    session = Session("chat", store)
    await session.enqueue(envelope("first"))
    started = await store.start(
        "chat",
        invocation_id="first-run",
        parameters_digest=digest("first-run"),
        input_ids=("first",),
        input_mode=mode,
        agent_id="agent",
    )
    assert not started.duplicate
    return SessionWriter(session, started.snapshot)


def checkpoint(writer: SessionWriter, step_id: str | None) -> RunCheckpoint:
    return RunCheckpoint(
        session_id=writer.session_id,
        run_id=writer.run_id,
        revision=writer.revision,
        writer_epoch=writer.writer_epoch,
        root_agent="agent",
        active_agent="agent",
        agent_fingerprint=digest("agent"),
        step_id=step_id,
        deadline=datetime.now(UTC) + timedelta(seconds=60),
        execution_timeout_seconds=60,
        max_model_steps=10,
        max_output_tokens=1024,
        tool_concurrency=2,
        max_handoffs=4,
        grants=CapabilityGrants(),
        usage=RunUsage(),
        context_budget=ContextBudget(window_tokens=4096, output_reserve=1024, compact_at=2500, recent_tokens=1024),
    )


def test_input_retries_preserve_original_receipt_and_reject_changed_payload(store: SessionStore) -> None:
    async def scenario() -> None:
        session = Session("chat", store)
        first = await session.enqueue(envelope("same"))
        retried = await asyncio.gather(*(session.enqueue(envelope("same")) for attempt in range(10)))
        assert all(
            value.duplicate and value.sequence == first.sequence and value.accepted_at == first.accepted_at
            for value in retried
        )
        with pytest.raises(SessionConflictError, match="different content"):
            await session.enqueue(envelope("same", "changed"))
        records = await session.pending_inputs()
        assert len(records) == 1 and records[0].envelope.content == "question"

    asyncio.run(scenario())


def test_start_retry_recovers_accepted_run_without_claiming_inputs_twice(store: SessionStore) -> None:
    async def scenario() -> None:
        writer = await start_writer(store)
        retried = await store.start(
            "chat",
            invocation_id="first-run",
            parameters_digest=digest("first-run"),
            input_ids=("first",),
            input_mode="batch",
            agent_id="agent",
        )
        assert retried.duplicate and retried.snapshot == writer.snapshot
        assert await writer.session.run_for_invocation("first-run") == writer.snapshot
        with pytest.raises(SessionConflictError, match="different parameters"):
            await store.start(
                "chat",
                invocation_id="first-run",
                parameters_digest=digest("changed"),
                input_ids=("first",),
                input_mode="batch",
                agent_id="agent",
            )
        with pytest.raises(SessionConflictError, match="active or paused"):
            await store.start(
                "chat",
                invocation_id="second-run",
                parameters_digest=digest("second"),
                input_ids=("first",),
                input_mode="batch",
                agent_id="agent",
            )
        inputs = (await store.snapshot("chat")).inputs
        assert len(inputs) == 1 and inputs[0].run_id == writer.run_id and inputs[0].step_id is None

    asyncio.run(scenario())


def test_start_claim_is_atomic_when_one_input_is_missing_or_out_of_order(store: SessionStore) -> None:
    async def scenario() -> None:
        session = Session("chat", store)
        await session.enqueue(envelope("first"))
        await session.enqueue(envelope("second"))
        for input_ids in (("first", "missing"), ("second", "first")):
            with pytest.raises(SessionConflictError):
                await store.start(
                    "chat",
                    invocation_id="invalid",
                    parameters_digest=digest("invalid"),
                    input_ids=input_ids,
                    input_mode="batch",
                    agent_id="agent",
                )
        assert tuple(value.envelope.input_id for value in await session.pending_inputs()) == ("first", "second")
        assert await session.run_for_invocation("invalid") is None

    asyncio.run(scenario())


def test_step_consumes_ordered_inputs_with_model_visible_provenance(store: SessionStore) -> None:
    async def scenario() -> None:
        session = Session("chat", store)
        for identity in ("first", "second"):
            await session.enqueue(envelope(identity, identity))
        started = await store.start(
            "chat",
            invocation_id="batch",
            parameters_digest=digest("batch"),
            input_ids=("first", "second"),
            input_mode="batch",
            agent_id="agent",
        )
        writer = SessionWriter(session, started.snapshot)
        step = await writer.begin("agent")
        assert step.input_ids == ("first", "second")
        assert tuple(value.message_id for value in step.messages) == step.input_ids
        payloads = [json.loads(value.content) for value in step.messages if isinstance(value.content, str)]
        assert [value["source"] for value in payloads] == ["test.user", "test.user"]
        assert [value["content"] for value in payloads] == ["first", "second"]
        assert all(value.step_id == step.step_id for value in (await store.snapshot("chat")).inputs)

    asyncio.run(scenario())


@pytest.mark.parametrize("mode", ["batch", "live"])
def test_late_input_is_consumed_only_at_a_live_step_boundary(
    store: SessionStore, mode: Literal["batch", "live"]
) -> None:
    async def scenario() -> None:
        writer = await start_writer(store, mode=mode)
        first = await writer.begin("agent")
        await writer.session.enqueue(envelope("late", "supplement"))
        assert tuple(value.envelope.input_id for value in writer.current_inputs) == ("first",)
        await writer.complete(first.step_id, (AssistantMessage(content="first answer"),))
        second = await writer.begin("agent")
        assert second.input_ids == (("late",) if mode == "live" else ())
        assert tuple(value.envelope.input_id for value in await writer.session.pending_inputs()) == (
            () if mode == "live" else ("late",)
        )

    asyncio.run(scenario())


def test_aborted_step_retains_observations_and_consumption_without_entering_model_history(store: SessionStore) -> None:
    async def scenario() -> None:
        writer = await start_writer(store)
        step = await writer.begin("agent")
        assistant = AssistantMessage(tool_calls=(ToolCall(id="call", name="lookup", arguments="{}"),))
        await writer.append(step.step_id, (assistant,))
        await writer.abort(step.step_id)
        page = await writer.session.read()
        assert [value.message.message_id for value in page.messages] == ["first", assistant.message_id]
        assert await writer.read() == () and await writer.active() is None
        next_step = await writer.begin("agent")
        assert next_step.sequence == 2 and next_step.run_sequence == 2 and next_step.input_ids == ()
        original = (await store.snapshot("chat")).steps[0]
        assert original.state == "aborted" and original.messages[-1] == assistant

    asyncio.run(scenario())


def test_incomplete_tool_pairing_rolls_back_completion(store: SessionStore) -> None:
    async def scenario() -> None:
        writer = await start_writer(store)
        step = await writer.begin("agent")
        assistant = AssistantMessage(tool_calls=(ToolCall(id="call", name="lookup", arguments="{}"),))
        before = writer.revision
        with pytest.raises(ModelStepInvalidError) as failure:
            await writer.complete(step.step_id, (assistant,))
        assert getattr(failure.value, "code", None) == "session.step.invalid"
        assert writer.revision == before and await writer.active() == step
        assert tuple(value.message.message_id for value in (await writer.session.read()).messages) == ("first",)

    asyncio.run(scenario())


def test_resume_fences_the_old_writer_and_preserves_paused_event_checkpoint(store: SessionStore) -> None:
    async def scenario() -> None:
        writer = await start_writer(store)
        step = await writer.begin("agent")
        saved = await writer.save_checkpoint(checkpoint(writer, step.step_id))
        await writer.emit(Event(kind="run.paused", run_id=writer.run_id))
        persisted = await writer.session.run_for_invocation("first-run")
        assert persisted is not None and persisted.checkpoint == saved
        resumed = await store.resume("chat", saved)
        assert resumed.writer_epoch > writer.writer_epoch
        with pytest.raises(StaleWriterError):
            await writer.append(step.step_id, (AssistantMessage(content="late response"),))
        current = SessionWriter(writer.session, resumed)
        await current.complete(step.step_id, (AssistantMessage(content="current response"),))
        assert (await current.read())[0].messages[-1].content == "current response"

    asyncio.run(scenario())


def test_concurrent_resume_and_paused_cancel_commit_one_control_outcome(store: SessionStore) -> None:
    async def scenario() -> None:
        writer = await start_writer(store)
        step = await writer.begin("agent")
        saved = await writer.save_checkpoint(checkpoint(writer, step.step_id))
        barrier = asyncio.Barrier(2)

        async def resume() -> str:
            await barrier.wait()
            try:
                await store.resume("chat", saved)
            except SessionConflictError:
                return "conflict"
            return "resumed"

        async def cancel() -> str:
            await barrier.wait()
            try:
                await store.cancel_paused("chat", checkpoint=saved, reason="user cancelled")
            except SessionConflictError:
                return "conflict"
            return "cancelled"

        results = await asyncio.gather(resume(), cancel())
        assert results.count("conflict") == 1
        snapshot = await store.run("chat", writer.run_id)
        assert snapshot.state in {RunStatus.RUNNING, RunStatus.CANCELLED}
        if snapshot.receipt is not None:
            assert snapshot.receipt.state == "cancelled"
            assert (await store.snapshot("chat")).steps[0].state == "aborted"

    asyncio.run(scenario())


def test_receipt_and_event_replay_survive_sqlite_reopen(tmp_path: Path) -> None:
    async def scenario() -> None:
        database = tmp_path / "sessions.sqlite3"
        writer = await start_writer(SqliteSessionStore(database))
        step = await writer.begin("agent")
        await writer.complete(step.step_id, (AssistantMessage(content="answer"),))
        await writer.finish(
            RunReceipt(
                run_id=writer.run_id,
                input_ids=("first",),
                state="succeeded",
                final_output="answer",
                completed_at=datetime.now(UTC),
            )
        )
        event = await writer.emit(Event(kind="run.completed", run_id=writer.run_id))
        restored = Session("chat", SqliteSessionStore(database))
        snapshot = await restored.run_for_invocation("first-run")
        assert snapshot is not None and snapshot.receipt is not None
        assert snapshot.receipt.final_output == "answer"
        assert await restored.store.read_events("chat", writer.run_id) == (event,)
        assert await restored.store.read_events("chat", writer.run_id, after_sequence=event.sequence) == ()
        assert [value.message.content for value in (await restored.read(after_sequence=1, limit=1)).messages] == [
            "answer"
        ]

    asyncio.run(scenario())


def test_clear_rejects_active_runs_and_removes_only_a_stopped_session(store: SessionStore) -> None:
    async def scenario() -> None:
        writer = await start_writer(store)
        other = Session("other", store)
        await other.enqueue(envelope("retained"))
        with pytest.raises(SessionConflictError, match="active or paused"):
            await writer.session.clear()
        step = await writer.begin("agent")
        await writer.complete(step.step_id, (AssistantMessage(content="answer"),))
        await writer.finish(
            RunReceipt(run_id=writer.run_id, input_ids=("first",), state="succeeded", completed_at=datetime.now(UTC))
        )
        await writer.session.clear()
        assert (await writer.session.read()).messages == ()
        assert await writer.session.run_for_invocation("first-run") is None
        assert [value.envelope.input_id for value in await other.pending_inputs()] == ["retained"]

    asyncio.run(scenario())


def test_background_completion_generates_one_owned_input_atomically(store: SessionStore) -> None:
    async def scenario() -> None:
        writer = await start_writer(store)
        step = await writer.begin("agent")
        await writer.append(
            step.step_id, (AssistantMessage(tool_calls=(ToolCall(id="call", name="command", arguments="{}"),)),)
        )
        await writer.save_tool_call(
            ToolCallRecord(
                run_id=writer.run_id,
                step_id=step.step_id,
                call_id="call",
                tool_code="command",
                binding_digest="binding",
                arguments_digest="arguments",
                authorization_ref="host",
                operation_id="operation",
            )
        )
        acceptance = ToolAcceptance(
            operation_id="operation", backend="external", reference="external-action", request_digest=digest("request")
        )
        submission = ToolSubmission(operation_id="operation", request_digest=acceptance.request_digest)
        prepared = OperationRecord(
            operation_id="operation", run_id=writer.run_id, call_id="call", state="prepared", submission=submission
        )
        await writer.save_operation(prepared)
        await writer.save_operation(prepared.model_copy(update={"state": "running", "backend_receipt": acceptance}))
        await writer.complete(step.step_id, (ToolMessage(tool_call_id="call", content="started"),))
        result = ToolResult(
            call_id="call",
            ok=True,
            output={"text": "done"},
            started_at=datetime.now(UTC),
            finished_at=datetime.now(UTC),
        )
        completed = OperationRecord(
            operation_id="operation",
            run_id=writer.run_id,
            call_id="call",
            state="succeeded",
            backend_receipt=acceptance,
            submission=submission,
            result=result,
            ended_at=datetime.now(UTC),
        )
        completion_input = InputEnvelope(
            input_id="operation:operation:result",
            kind="operation_result",
            source="pleiad.operation",
            content=OperationResultReference.from_result("operation", result).input_reference(),
            causation_id="operation",
        )
        with pytest.raises(SessionConflictError):
            await writer.complete_operation(
                completed, completion_input.model_copy(update={"content": result.model_dump_json()})
            )
        await writer.complete_operation(completed, completion_input)
        await writer.complete_operation(completed, completion_input)
        assert await writer.session.pending_inputs() == ()
        snapshot = await store.snapshot("chat")
        assert snapshot.inputs[-1].run_id == writer.run_id and snapshot.inputs[-1].step_id is None
        assert (await writer.session.enqueue(completion_input)).duplicate
        with pytest.raises(SessionConflictError):
            await writer.session.enqueue(completion_input.model_copy(update={"input_id": "forged-operation-result"}))
        next_step = await writer.begin("agent")
        assert next_step.input_ids == ("operation:operation:result",)
        assert len((await store.snapshot("chat")).inputs) == 2
        assert "done" in str(next_step.messages[0].content)

    asyncio.run(scenario())


def test_reconcile_fences_lost_owner_and_retains_unknown_external_actions(store: SessionStore) -> None:
    async def scenario() -> None:
        writer = await start_writer(store)
        step = await writer.begin("agent")
        await writer.append(
            step.step_id, (AssistantMessage(tool_calls=(ToolCall(id="call", name="remote", arguments="{}"),)),)
        )
        await writer.save_tool_call(
            ToolCallRecord(
                run_id=writer.run_id,
                step_id=step.step_id,
                call_id="call",
                tool_code="remote",
                binding_digest="binding",
                arguments_digest="arguments",
                authorization_ref="host",
                state="unknown",
            )
        )
        result = await store.reconcile(
            "chat",
            run_id=writer.run_id,
            request_id="recover",
            parameters_digest=digest("recover"),
            expected_epoch=writer.writer_epoch,
            expected_revision=writer.revision,
        )
        assert result.disposition == "interrupted"
        assert result.snapshot.receipt is not None and result.snapshot.receipt.reason == "interrupted"
        assert result.unsettled_actions == (ActionRef(run_id=writer.run_id, call_id="call"),)
        retry = await store.reconcile(
            "chat",
            run_id=writer.run_id,
            request_id="recover",
            parameters_digest=digest("recover"),
            expected_epoch=writer.writer_epoch,
            expected_revision=writer.revision,
        )
        assert retry.duplicate and retry.snapshot == result.snapshot
        with pytest.raises(StaleWriterError):
            await writer.complete(step.step_id, (ToolMessage(tool_call_id="call", content="late"),))
        receipt = result.snapshot.receipt
        confirmed = await store.reconcile(
            "chat",
            run_id=writer.run_id,
            request_id="confirmed",
            parameters_digest=digest("confirmed"),
            expected_epoch=result.snapshot.writer_epoch,
            expected_revision=result.snapshot.revision,
            evidence=(
                ActionEvidence(
                    evidence_id="evidence",
                    action=ActionRef(run_id=writer.run_id, call_id="call"),
                    binding_digest="binding",
                    arguments_digest="arguments",
                    backend_evidence_ref="trusted.receipt",
                    observed_at=datetime.now(UTC),
                    observed_state="succeeded",
                    result=ToolResult(
                        call_id="call",
                        ok=True,
                        output={"confirmed": True},
                        started_at=datetime.now(UTC),
                        finished_at=datetime.now(UTC),
                    ),
                ),
            ),
        )
        assert confirmed.snapshot.receipt == receipt and confirmed.unsettled_actions == ()
        assert (await store.tool_calls("chat", writer.run_id))[0].state == "succeeded"

    asyncio.run(scenario())


def test_sqlite_rejects_old_formats_before_mutating_user_history(tmp_path: Path) -> None:
    database = tmp_path / "old.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            "CREATE TABLE short_memories(memory_id TEXT PRIMARY KEY); INSERT INTO short_memories VALUES('retained');"
        )
    original = database.read_bytes()
    with pytest.raises(SessionDataCorruptedError, match="Unsupported SessionStore schema"):
        asyncio.run(Session("chat", SqliteSessionStore(database)).enqueue(envelope("first")))
    assert database.read_bytes() == original


def test_reconcile_keeps_a_persisted_pre_action_decision_safely_paused(store: SessionStore) -> None:
    async def scenario() -> None:
        writer = await start_writer(store)
        step = await writer.begin("agent")
        await writer.append(
            step.step_id, (AssistantMessage(tool_calls=(ToolCall(id="approval", name="approve", arguments="{}"),)),)
        )
        await writer.save_tool_call(
            ToolCallRecord(
                run_id=writer.run_id,
                step_id=step.step_id,
                call_id="approval",
                tool_code="approve",
                binding_digest="binding",
                arguments_digest="arguments",
                authorization_ref="host",
            )
        )
        pending = ToolInterruption(
            interruption_id="decision",
            call_id="approval",
            tool_code="approve",
            tool_name="approve",
            arguments={},
            reason="Awaiting host decision",
        )
        saved = await writer.save_checkpoint(
            checkpoint(writer, step.step_id).model_copy(update={"interruptions": (pending,)})
        )

        recovered = await store.reconcile(
            "chat",
            run_id=writer.run_id,
            request_id="recover-paused",
            parameters_digest=digest("recover-paused"),
            expected_epoch=saved.writer_epoch,
            expected_revision=saved.revision,
        )

        assert recovered.disposition == "paused" and recovered.unsettled_actions == ()
        current = recovered.snapshot.checkpoint
        assert current is not None and current.interruptions == (pending,)
        assert current.writer_epoch > saved.writer_epoch and current.revision > saved.revision
        resumed = await store.resume("chat", current)
        assert resumed.state is RunStatus.RUNNING

    asyncio.run(scenario())


def test_reconcile_retains_deferred_action_results_committed_after_the_old_checkpoint(store: SessionStore) -> None:
    """Recovery preserves real completed actions beyond a previously interrupted prefix."""

    async def scenario() -> None:
        writer = await start_writer(store)
        step = await writer.begin("agent")
        await writer.append(
            step.step_id,
            (
                AssistantMessage(
                    tool_calls=tuple(
                        ToolCall(id=identity, name="action", arguments="{}") for identity in ("first", "deferred")
                    )
                ),
            ),
        )
        first = ToolCallRecord(
            run_id=writer.run_id,
            step_id=step.step_id,
            call_id="first",
            tool_code="action",
            binding_digest="binding",
            arguments_digest="arguments",
            authorization_ref="host",
        )
        await writer.save_tool_call(first)
        pending = ToolInterruption(
            interruption_id="decision",
            call_id="first",
            tool_code="action",
            tool_name="action",
            arguments={},
            reason="Wait",
        )
        saved = await writer.save_checkpoint(
            checkpoint(writer, step.step_id).model_copy(update={"interruptions": (pending,)})
        )
        writer = SessionWriter(writer.session, await store.resume("chat", saved))
        now = datetime.now(UTC)
        for intent in (first, first.model_copy(update={"call_id": "deferred"})):
            result = ToolResult(
                call_id=intent.call_id, ok=True, output={"actual": intent.call_id}, started_at=now, finished_at=now
            )
            await writer.save_tool_call(intent.model_copy(update={"state": "succeeded", "result": result}))
        await writer.save_usage(RunUsage(tool_calls=2))
        recovered = await store.reconcile(
            "chat",
            run_id=writer.run_id,
            request_id="recover-deferred",
            parameters_digest=digest("recover-deferred"),
            expected_epoch=writer.writer_epoch,
            expected_revision=writer.revision,
        )
        assert recovered.disposition == "paused"
        current = recovered.snapshot.checkpoint
        assert current is not None and current.interruptions == ()
        assert [value.call_id for value in current.tool_results] == ["first", "deferred"]
        assert [value.output for value in current.tool_results] == [{"actual": "first"}, {"actual": "deferred"}]
        assert current.usage.tool_calls == 2

    asyncio.run(scenario())


def test_reconcile_does_not_resume_an_old_checkpoint_after_its_step_has_completed(store: SessionStore) -> None:
    """A lost owner cannot restore a safe position superseded by a later active step."""

    async def scenario() -> None:
        writer = await start_writer(store)
        first = await writer.begin("agent")
        saved = await writer.save_checkpoint(checkpoint(writer, first.step_id))
        writer = SessionWriter(writer.session, await store.resume("chat", saved))
        await writer.complete(first.step_id, (AssistantMessage(content="Actual first response"),))
        second = await writer.begin("agent")
        recovered = await store.reconcile(
            "chat",
            run_id=writer.run_id,
            request_id="recover-later-step",
            parameters_digest=digest("recover-later-step"),
            expected_epoch=writer.writer_epoch,
            expected_revision=writer.revision,
        )
        assert recovered.disposition == "interrupted"
        assert recovered.snapshot.checkpoint is None
        assert recovered.snapshot.receipt is not None
        assert recovered.snapshot.receipt.reason == "interrupted"
        history = (await store.snapshot("chat")).steps
        assert history[0].completed and history[0].messages[-1].content == "Actual first response"
        assert history[1].step_id == second.step_id and history[1].state == "aborted"

    asyncio.run(scenario())


class CommitBarrierStore(SqliteSessionStore):
    """Pause the SQLite thread after commit while its caller still awaits the result."""

    def __init__(self, database: Path) -> None:
        super().__init__(database)
        self.block_next = False
        self.committed = ThreadEvent()
        self.release = ThreadEvent()
        self.thread_finished = ThreadEvent()

    def transact_sync[T](self, session_id: str, mutation: Callable[[SessionState], tuple[SessionState, T]]) -> T:
        result = super().transact_sync(session_id, mutation)
        if self.block_next:
            self.block_next = False
            self.committed.set()
            if not self.release.wait(timeout=5):
                raise TimeoutError("The test did not release its committed SQLite transaction")
            self.thread_finished.set()
        return result


@pytest.mark.parametrize("mutation", ["event", "step"])
def test_cancellation_after_sqlite_commit_refreshes_the_writer_before_cleanup(tmp_path: Path, mutation: str) -> None:
    """A cancelled write drains its thread and retains the committed fencing revision."""

    async def scenario() -> None:
        store = CommitBarrierStore(tmp_path / "cancelled-commit.sqlite3")
        writer = await start_writer(store)
        revision = writer.revision
        store.block_next = True

        async def perform_write() -> None:
            if mutation == "event":
                await writer.emit(Event(kind="test.committed", run_id=writer.run_id))
            else:
                await writer.begin("agent")

        task = asyncio.create_task(perform_write())
        try:
            assert await asyncio.to_thread(store.committed.wait, 3)
            task.cancel()
            await asyncio.wait((task,), timeout=0)
            assert not task.done() and not store.thread_finished.is_set()
            task.cancel()
        finally:
            store.release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert store.thread_finished.is_set()
        committed = await store.run(writer.session_id, writer.run_id)
        assert writer.snapshot == committed
        assert writer.revision == revision + (1 if mutation == "step" else 0)
        if mutation == "event":
            events = await writer.events()
            assert len(events) == 1 and events[0].kind == "test.committed"
        else:
            active = await writer.active()
            assert active is not None
            await writer.abort(active.step_id)
        terminal = await writer.finish(
            RunReceipt(
                run_id=writer.run_id,
                input_ids=writer.snapshot.input_ids,
                state="cancelled",
                reason="test_cancelled",
                completed_at=datetime.now(UTC),
            )
        )
        assert terminal.state is RunStatus.CANCELLED and terminal.receipt is not None
        assert (await store.snapshot(writer.session_id)).current_run_id is None
        assert await writer.session.history() == ()

    asyncio.run(scenario())


def test_descendant_lookup_keeps_recursive_ownership_separate_from_unrelated_sessions(store: SessionStore) -> None:
    async def scenario() -> None:
        async def accept(session_id: str, parent_run_id: str | None) -> str:
            await store.enqueue(
                session_id, InputEnvelope(input_id="input", content="accepted work", kind="user", source="host")
            )
            result = await store.start(
                session_id,
                invocation_id="work",
                parameters_digest=digest(session_id),
                input_ids=("input",),
                input_mode="batch",
                agent_id="agent",
                parent_run_id=parent_run_id,
            )
            return result.snapshot.run_id

        root = await accept("root", None)
        child = await accept("child", root)
        await accept("grandchild", child)
        await accept("unrelated", None)

        assert await store.descendant_sessions((root,)) == ("child", "grandchild")
        assert await store.descendant_sessions((child,)) == ("grandchild",)
        assert await store.descendant_sessions(()) == ()
        assert (await store.run("child", child)).parent_run_id == root
        with pytest.raises(SessionConflictError, match="different parameters"):
            await store.start(
                "child",
                invocation_id="work",
                parameters_digest=digest("child"),
                input_ids=("input",),
                input_mode="batch",
                agent_id="agent",
                parent_run_id="another-root",
            )

    asyncio.run(scenario())


def test_descendant_lookup_refuses_to_claim_a_session_reused_by_an_independent_parent(store: SessionStore) -> None:
    async def scenario() -> None:
        session = Session("reused", store)
        await session.enqueue(envelope("one"))
        first = await store.start(
            session.session_id,
            invocation_id="first",
            parameters_digest=digest("first"),
            input_ids=("one",),
            input_mode="batch",
            agent_id="agent",
            parent_run_id="owned-parent",
        )
        writer = SessionWriter(session, first.snapshot)
        await writer.finish(
            RunReceipt(run_id=writer.run_id, input_ids=("one",), state="cancelled", completed_at=datetime.now(UTC))
        )
        await session.enqueue(envelope("two"))
        await store.start(
            session.session_id,
            invocation_id="second",
            parameters_digest=digest("second"),
            input_ids=("two",),
            input_mode="batch",
            agent_id="agent",
            parent_run_id="independent-parent",
        )

        with pytest.raises(SessionConflictError, match="independently owned"):
            await store.descendant_sessions(("owned-parent",))
        assert len((await store.snapshot(session.session_id)).runs) == 2

    asyncio.run(scenario())


def test_a_checkpoint_cannot_reassign_an_accepted_run_to_another_parent(store: SessionStore) -> None:
    async def scenario() -> None:
        writer = await start_writer(store)
        step = await writer.begin("agent")
        before = await store.snapshot("chat")

        with pytest.raises(SessionConflictError, match="another parent Run"):
            await writer.save_checkpoint(
                checkpoint(writer, step.step_id).model_copy(update={"parent_run_id": "foreign"})
            )

        assert await store.snapshot("chat") == before

    asyncio.run(scenario())


@pytest.mark.parametrize("observed_state", ["running", "succeeded"])
def test_reconciliation_recovers_actual_backend_admission_after_receipt_commit_was_lost(
    store: SessionStore, observed_state: Literal["running", "succeeded"]
) -> None:
    """A query receipt repairs admission facts without restarting its original external action."""

    async def scenario() -> None:
        writer = await start_writer(store)
        step = await writer.begin("agent")
        await writer.append(
            step.step_id, (AssistantMessage(tool_calls=(ToolCall(id="call", name="command", arguments="{}"),)),)
        )
        await writer.save_tool_call(
            ToolCallRecord(
                run_id=writer.run_id,
                step_id=step.step_id,
                call_id="call",
                tool_code="command",
                binding_digest="binding",
                arguments_digest="arguments",
                authorization_ref="grant",
            )
        )
        await writer.save_operation(
            OperationRecord(
                operation_id="stable-operation",
                run_id=writer.run_id,
                call_id="call",
                state="prepared",
                submission=ToolSubmission(operation_id="stable-operation", request_digest=digest("request")),
            )
        )
        acceptance = ToolAcceptance(
            operation_id="stable-operation",
            backend="external",
            reference="accepted-container",
            request_digest=digest("request"),
        )
        evidence = ActionEvidence(
            evidence_id="queried-receipt",
            action=ActionRef(run_id=writer.run_id, call_id="call", operation_id="stable-operation"),
            binding_digest="binding",
            arguments_digest="arguments",
            backend_evidence_ref="external-query",
            observed_at=datetime.now(UTC),
            observed_state=observed_state,
            acceptance=acceptance,
        )
        with pytest.raises(SessionConflictError, match="prepared backend request"):
            await store.reconcile(
                "chat",
                run_id=writer.run_id,
                request_id="mismatched-request",
                parameters_digest=digest("mismatched-request"),
                expected_epoch=writer.writer_epoch,
                expected_revision=writer.revision,
                evidence=(
                    evidence.model_copy(
                        update={
                            "acceptance": acceptance.model_copy(update={"request_digest": digest("different request")})
                        }
                    ),
                ),
            )
        assert (await store.operations("chat", writer.run_id))[0].backend_receipt is None
        recovered = await store.reconcile(
            "chat",
            run_id=writer.run_id,
            request_id="recover-admission",
            parameters_digest=digest("recover-admission"),
            expected_epoch=writer.writer_epoch,
            expected_revision=writer.revision,
            evidence=(evidence,),
        )
        operation = (await store.operations("chat", writer.run_id))[0]
        assert operation.backend_receipt == acceptance and operation.state == observed_state
        assert recovered.disposition == "interrupted"
        assert recovered.snapshot.receipt is not None and recovered.snapshot.receipt.reason == "interrupted"
        assert bool(recovered.unsettled_actions) is (observed_state == "running")
        with pytest.raises(SessionConflictError):
            await store.reconcile(
                "chat",
                run_id=writer.run_id,
                request_id="contradict-admission",
                parameters_digest=digest("contradict-admission"),
                expected_epoch=recovered.snapshot.writer_epoch,
                expected_revision=recovered.snapshot.revision,
                evidence=(
                    evidence.model_copy(
                        update={"evidence_id": "absence", "observed_state": "not_started", "acceptance": None}
                    ),
                ),
            )
        assert (await store.operations("chat", writer.run_id))[0] == operation

    asyncio.run(scenario())


def test_late_backend_admission_keeps_an_already_requested_cancellation(store: SessionStore) -> None:
    """Acceptance can become known during cancellation while the stop request remains pending."""

    async def scenario() -> None:
        writer = await start_writer(store)
        step = await writer.begin("agent")
        await writer.append(
            step.step_id, (AssistantMessage(tool_calls=(ToolCall(id="call", name="command", arguments="{}"),)),)
        )
        await writer.save_tool_call(
            ToolCallRecord(
                run_id=writer.run_id,
                step_id=step.step_id,
                call_id="call",
                tool_code="command",
                binding_digest="binding",
                arguments_digest="arguments",
                authorization_ref="grant",
            )
        )
        prepared = OperationRecord(
            operation_id="operation",
            run_id=writer.run_id,
            call_id="call",
            state="prepared",
            submission=ToolSubmission(operation_id="operation", request_digest=digest("request")),
        )
        await writer.save_operation(prepared)
        await writer.request_operation_cancel("operation")
        acceptance = ToolAcceptance(
            operation_id="operation", backend="external", reference="accepted-action", request_digest=digest("request")
        )
        await writer.save_operation(prepared.model_copy(update={"state": "running", "backend_receipt": acceptance}))
        cancelling = (await store.operations("chat", writer.run_id))[0]
        assert cancelling.state == "cancel_requested" and cancelling.backend_receipt == acceptance
        cancelled = cancelling.model_copy(update={"state": "cancelled", "ended_at": datetime.now(UTC)})
        await writer.complete_operation(cancelled, None)
        await writer.request_operation_cancel("operation")
        assert (await store.operations("chat", writer.run_id))[0] == cancelled

    asyncio.run(scenario())


@pytest.mark.parametrize("identity", ["operation_id", "run_id", "call_id"])
def test_sqlite_refuses_an_operation_that_lost_its_persisted_run_or_call_ownership(
    tmp_path: Path, identity: str
) -> None:
    async def scenario() -> None:
        database = tmp_path / "corrupted-operation.sqlite3"
        store = SqliteSessionStore(database)
        writer = await start_writer(store)
        step = await writer.begin("agent")
        await writer.append(
            step.step_id, (AssistantMessage(tool_calls=(ToolCall(id="call", name="command", arguments="{}"),)),)
        )
        await writer.save_tool_call(
            ToolCallRecord(
                run_id=writer.run_id,
                step_id=step.step_id,
                call_id="call",
                tool_code="command",
                binding_digest="original-binding",
                arguments_digest="original-arguments",
                authorization_ref="host",
            )
        )
        await writer.save_operation(
            OperationRecord(operation_id="operation", run_id=writer.run_id, call_id="call", state="prepared")
        )
        journal = await store.snapshot("chat")
        corrupted = journal.model_copy(
            update={"operations": (journal.operations[0].model_copy(update={identity: "unrelated"}),)}
        )
        with sqlite3.connect(database) as connection:
            connection.execute(
                "UPDATE session_journals SET payload=? WHERE session_id='chat'", (corrupted.model_dump_json(),)
            )
        with pytest.raises(SessionDataCorruptedError, match="persisted Session journal is invalid"):
            await SqliteSessionStore(database).snapshot("chat")

    asyncio.run(scenario())


@pytest.mark.parametrize("reported", [True, False], ids=["known-provider-usage", "missing-provider-usage"])
def test_model_admission_retains_unknown_cost_and_settles_its_reported_measurement_once(
    store: SessionStore, reported: bool
) -> None:
    """A completed request replaces its original allowance while retaining one call."""

    async def scenario() -> None:
        writer = await start_writer(store)
        step = await writer.begin("agent")
        baseline = writer.snapshot.usage
        deadline = datetime.now(UTC) + timedelta(seconds=60)
        attempt = await writer.admit_model(
            attempt_id="attempt",
            step_id=step.step_id,
            provider="fake",
            model="model",
            request_digest=digest("request"),
            max_output_tokens=17,
            deadline=deadline,
            usage=baseline,
        )
        assert attempt is not None and attempt.completed_at is None
        assert writer.snapshot.usage.model_calls == 1
        assert writer.snapshot.usage.input_tokens is None and writer.snapshot.usage.output_tokens is None
        assert writer.snapshot.usage.budget_output_tokens == 17
        repeated = await writer.admit_model(
            attempt_id="attempt",
            step_id=step.step_id,
            provider="fake",
            model="model",
            request_digest=digest("request"),
            max_output_tokens=17,
            deadline=deadline,
            usage=baseline,
        )
        assert repeated == attempt and len(writer.snapshot.model_attempts) == 1
        measurement = ModelUsage(input_tokens=3, output_tokens=2, total_tokens=5) if reported else ModelUsage()
        measured = await writer.complete_model(attempt.attempt_id, measurement, "response")
        assert measured.model_calls == 1 and measured.budget_output_tokens == (2 if reported else 17)
        assert measured.output_tokens == (2 if reported else None)
        assert await writer.complete_model(attempt.attempt_id, measurement, "response") == measured
        with pytest.raises(SessionConflictError, match="different completed usage"):
            await writer.complete_model(
                attempt.attempt_id, ModelUsage(input_tokens=3, output_tokens=4, total_tokens=7), "response"
            )
        saved = await store.run(writer.session_id, writer.run_id)
        assert saved.usage == measured and saved.model_attempts[0].result_usage == measurement
        assert saved.model_attempts[0].completed_at is not None
        with pytest.raises(ValueError, match="retain actual or conservative model charges"):
            await writer.save_checkpoint(
                checkpoint(writer, step.step_id).model_copy(
                    update={
                        "usage": measured.model_copy(update={"budget_output_tokens": measured.budget_output_tokens - 1})
                    }
                )
            )

    asyncio.run(scenario())


def test_expired_model_authority_does_not_admit_or_charge_an_external_attempt(store: SessionStore) -> None:
    """Deadline expiry before admission preserves a zero-call request history."""

    async def scenario() -> None:
        writer = await start_writer(store)
        step = await writer.begin("agent")
        attempt = await writer.admit_model(
            attempt_id="expired",
            step_id=step.step_id,
            provider="fake",
            model="model",
            request_digest=digest("request"),
            max_output_tokens=17,
            deadline=datetime.now(UTC) - timedelta(seconds=1),
            usage=writer.snapshot.usage,
        )
        assert attempt is None and writer.snapshot.model_attempts == ()
        assert writer.snapshot.usage == RunUsage()

    asyncio.run(scenario())


@pytest.mark.parametrize("phase", ["admission", "completion"])
def test_model_charge_survives_cancellation_after_its_sqlite_commit(tmp_path: Path, phase: str) -> None:
    """Acknowledgement loss retains either the admitted cap or its actual completed cost."""

    async def scenario() -> None:
        store = CommitBarrierStore(tmp_path / "model-commit.sqlite3")
        writer = await start_writer(store)
        step = await writer.begin("agent")
        baseline = writer.snapshot.usage
        deadline = datetime.now(UTC) + timedelta(seconds=60)

        async def admit() -> None:
            await writer.admit_model(
                attempt_id="attempt",
                step_id=step.step_id,
                provider="fake",
                model="model",
                request_digest=digest("request"),
                max_output_tokens=17,
                deadline=deadline,
                usage=baseline,
            )

        if phase == "completion":
            await admit()
        store.block_next = True

        async def mutate() -> None:
            if phase == "admission":
                await admit()
            else:
                await writer.complete_model(
                    "attempt", ModelUsage(input_tokens=3, output_tokens=2, total_tokens=5), "response"
                )

        async with asyncio.TaskGroup() as group:
            task = group.create_task(mutate())
            try:
                assert await asyncio.to_thread(store.committed.wait, 3)
                task.cancel()
            finally:
                store.release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert store.thread_finished.is_set()
        saved = await store.run(writer.session_id, writer.run_id)
        assert writer.snapshot == saved
        assert saved.usage.model_calls == 1 and saved.usage.budget_output_tokens == (17 if phase == "admission" else 2)
        assert saved.usage.output_tokens == (None if phase == "admission" else 2)
        assert (saved.model_attempts[0].completed_at is None) is (phase == "admission")
        await writer.abort(step.step_id)
        await writer.finish(
            RunReceipt(
                run_id=writer.run_id,
                input_ids=writer.snapshot.input_ids,
                state="cancelled",
                usage=saved.usage,
                completed_at=datetime.now(UTC),
            )
        )
        assert (await store.run(writer.session_id, writer.run_id)).receipt is not None

    asyncio.run(scenario())


def test_reconciliation_rejects_a_same_step_checkpoint_after_model_admission_started(store: SessionStore) -> None:
    """A pre-model pause cannot resume an already admitted request after ownership loss."""

    async def scenario() -> None:
        writer = await start_writer(store)
        step = await writer.begin("agent")
        paused = await writer.save_checkpoint(checkpoint(writer, step.step_id))
        writer = SessionWriter(writer.session, await store.resume(writer.session_id, paused))
        attempt = await writer.admit_model(
            attempt_id="resumed-model",
            step_id=step.step_id,
            provider="fake",
            model="model",
            request_digest=digest("request"),
            max_output_tokens=17,
            deadline=datetime.now(UTC) + timedelta(seconds=60),
            usage=writer.snapshot.usage,
        )
        assert attempt is not None
        with pytest.raises(SessionConflictError, match="uncompleted model admission"):
            await writer.save_checkpoint(checkpoint(writer, step.step_id))
        recovered = await store.reconcile(
            writer.session_id,
            run_id=writer.run_id,
            request_id="lost-provider-owner",
            parameters_digest=digest("lost-provider-owner"),
            expected_epoch=writer.writer_epoch,
            expected_revision=writer.revision,
        )
        assert recovered.disposition == "interrupted" and recovered.snapshot.checkpoint is None
        assert recovered.snapshot.receipt is not None and recovered.snapshot.receipt.reason == "interrupted"
        usage = recovered.snapshot.receipt.usage
        assert usage.model_calls == 1 and usage.output_tokens is None and usage.budget_output_tokens == 17
        assert recovered.snapshot.usage == usage
        with pytest.raises(StaleWriterError):
            await writer.complete_model(
                "resumed-model", ModelUsage(input_tokens=3, output_tokens=2, total_tokens=5), "late"
            )
        assert (await store.run(writer.session_id, writer.run_id)).usage == usage

    asyncio.run(scenario())
