import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from pleiad.events.contracts import Event
from pleiad.messages.message import Message, ToolMessage
from pleiad.models.result import ModelUsage
from pleiad.observability.tracing import Observability
from pleiad.sessions.checkpoint import RunCheckpoint
from pleiad.sessions.input import InputEnvelope, InputRecord
from pleiad.sessions.model_step import ModelStep
from pleiad.sessions.records import (
    EventRecord,
    ModelAttemptRecord,
    OperationRecord,
    RunReceipt,
    RunSnapshot,
    ToolCallRecord,
)
from pleiad.sessions.session import Session
from pleiad.sessions.store import complete_owned
from pleiad.sessions.usage import RunUsage


class SessionWriter:
    """Serialize one acquired Run's fenced journal mutations and revision cursor."""

    def __init__(self, session: Session, snapshot: RunSnapshot, *, observability: Observability | None = None) -> None:
        """Bind the Run cursor returned by an atomic start or resume transaction."""
        if session.session_id != snapshot.session_id:
            raise ValueError("writer Session identity must match the acquired Run")
        self.observability = Observability() if observability is None else observability
        self.session = session
        self.store = session.store
        self.snapshot = snapshot
        self.current_inputs: tuple[InputRecord, ...] = ()
        self.input_admission = True
        self.lock = asyncio.Lock()

    @asynccontextmanager
    async def mutation(self) -> AsyncIterator[None]:
        """Keep the revision cursor fenced until an admitted mutation fully settles."""
        async with self.lock:
            try:
                yield
            except asyncio.CancelledError:
                await complete_owned(self.refresh_snapshot())
                raise

    async def refresh_snapshot(self) -> None:
        """Recover the committed cursor before cancelled execution enters cleanup."""
        self.snapshot = await self.store.run(self.session_id, self.run_id)

    @property
    def session_id(self) -> str:
        """Return the stable interaction identity owned by this writer."""
        return self.session.session_id

    @property
    def run_id(self) -> str:
        """Return the Run whose facts this cursor may advance."""
        return self.snapshot.run_id

    @property
    def writer_epoch(self) -> int:
        """Return the acquired writer epoch used for every mutation."""
        return self.snapshot.writer_epoch

    @property
    def revision(self) -> int:
        """Return the last committed Run revision observed by this owner."""
        return self.snapshot.revision

    async def begin(
        self, agent_id: str, messages: tuple[Message, ...] = (), *, consume_inputs: bool = True
    ) -> ModelStep:
        """Create a safe-boundary step and atomically consume eligible inputs."""
        async with self.mutation():
            step, self.snapshot, self.current_inputs = await self.store.begin_step(
                self.session_id,
                run_id=self.run_id,
                expected_epoch=self.writer_epoch,
                expected_revision=self.revision,
                agent_id=agent_id,
                messages=messages,
                consume_inputs=consume_inputs and self.input_admission,
            )
            consumed_at = datetime.now(UTC)
            for item in self.current_inputs:
                self.observability.metrics.record_input_consumed(
                    item.envelope.kind, (consumed_at - item.accepted_at).total_seconds()
                )
            return step

    async def read(self) -> tuple[ModelStep, ...]:
        """Read protocol-complete model history from the Session journal."""
        return await self.session.history()

    async def active(self) -> ModelStep | None:
        """Read this Run's active model step."""
        return await self.store.active(self.session_id, self.run_id)

    async def append(self, step_id: str, messages: tuple[Message, ...]) -> ModelStep:
        """Persist an accepted model response or ordered tool observation."""
        async with self.mutation():
            step, self.snapshot = await self.store.change_step(
                self.session_id,
                run_id=self.run_id,
                expected_epoch=self.writer_epoch,
                expected_revision=self.revision,
                step_id=step_id,
                messages=messages,
                action="append",
            )
            return step

    async def complete(self, step_id: str, messages: tuple[Message, ...] = ()) -> ModelStep:
        """Settle a protocol-complete step without conflating Run completion."""
        async with self.mutation():
            step, self.snapshot = await self.store.change_step(
                self.session_id,
                run_id=self.run_id,
                expected_epoch=self.writer_epoch,
                expected_revision=self.revision,
                step_id=step_id,
                messages=messages,
                action="complete",
            )
            return step

    async def abort(self, step_id: str) -> None:
        """Retain an interrupted step and its original observations as aborted."""
        async with self.mutation():
            committed = await self.store.change_step(
                self.session_id,
                run_id=self.run_id,
                expected_epoch=self.writer_epoch,
                expected_revision=self.revision,
                step_id=step_id,
                action="abort",
            )
            self.snapshot = committed[1]

    async def read_tool_result(self, message_id: str) -> ToolMessage | None:
        """Read one original observation from protocol-complete history."""
        for step in await self.read():
            for message in step.messages:
                if message.message_id == message_id and isinstance(message, ToolMessage):
                    return message
        return None

    async def save_checkpoint(self, checkpoint: RunCheckpoint) -> RunCheckpoint:
        """Commit a recoverable paused position before notifying its observer."""
        async with self.mutation():
            self.snapshot = await self.store.save_checkpoint(
                self.session_id,
                checkpoint=checkpoint,
                expected_epoch=self.writer_epoch,
                expected_revision=self.revision,
            )
            saved = self.snapshot.checkpoint
            if saved is None:
                raise RuntimeError("the checkpoint transaction returned no checkpoint")
            return saved

    async def finish(self, receipt: RunReceipt) -> RunSnapshot:
        """Save a final receipt and release the Session for its next invocation."""
        async with self.mutation():
            self.snapshot = await self.store.finish(
                self.session_id, receipt=receipt, expected_epoch=self.writer_epoch, expected_revision=self.revision
            )
            return self.snapshot

    async def emit(self, event: Event) -> EventRecord:
        """Serialize event persistence with step, checkpoint, and receipt writes."""
        async with self.mutation():
            value, self.snapshot = await self.store.record_event(
                self.session_id,
                run_id=self.run_id,
                expected_epoch=self.writer_epoch,
                expected_revision=self.revision,
                event=event,
            )
            return value

    async def save_tool_call(self, record: ToolCallRecord) -> None:
        """Persist one tool intent or outcome within the acquired writer epoch."""
        async with self.mutation():
            self.snapshot = await self.store.save_tool_call(
                self.session_id, record=record, expected_epoch=self.writer_epoch, expected_revision=self.revision
            )

    async def save_operation(self, record: OperationRecord) -> None:
        """Save a backend operation receipt before model-visible acceptance."""
        async with self.mutation():
            self.snapshot = await self.store.save_operation(
                self.session_id, record=record, expected_epoch=self.writer_epoch, expected_revision=self.revision
            )

    async def events(self, *, after_sequence: int = 0, limit: int = 256) -> tuple[EventRecord, ...]:
        """Read committed events independently of this writer's lifetime."""
        return await self.store.read_events(self.session_id, self.run_id, after_sequence=after_sequence, limit=limit)

    async def admit_model(
        self,
        *,
        attempt_id: str,
        step_id: str,
        provider: str,
        model: str,
        request_digest: str,
        max_output_tokens: int,
        deadline: datetime,
        usage: RunUsage,
    ) -> ModelAttemptRecord | None:
        """Commit one model attempt and its actual authorized output allowance."""
        async with self.mutation():
            attempt, self.snapshot = await self.store.admit_model(
                self.session_id,
                run_id=self.run_id,
                attempt_id=attempt_id,
                step_id=step_id,
                provider=provider,
                model=model,
                request_digest=request_digest,
                max_output_tokens=max_output_tokens,
                deadline=deadline,
                usage=usage,
                expected_epoch=self.writer_epoch,
                expected_revision=self.revision,
            )
            return attempt

    async def complete_model(self, attempt_id: str, usage: ModelUsage, response_id: str | None) -> RunUsage:
        """Replace the original model allowance with its completed measurement once."""
        async with self.mutation():
            self.snapshot = await self.store.complete_model(
                self.session_id,
                run_id=self.run_id,
                attempt_id=attempt_id,
                usage=usage,
                response_id=response_id,
                expected_epoch=self.writer_epoch,
                expected_revision=self.revision,
            )
            return self.snapshot.usage

    async def save_usage(self, usage: RunUsage) -> None:
        """Persist model and tool charges before the next external action."""
        async with self.mutation():
            self.snapshot = await self.store.save_usage(
                self.session_id,
                run_id=self.run_id,
                usage=usage,
                expected_epoch=self.writer_epoch,
                expected_revision=self.revision,
            )

    async def waiting_event(self, waiting: bool) -> None:
        """Persist a background wait separately from step and tool outcomes."""
        async with self.mutation():
            self.snapshot = await self.store.waiting_event(
                self.session_id,
                run_id=self.run_id,
                waiting=waiting,
                expected_epoch=self.writer_epoch,
                expected_revision=self.revision,
            )

    async def request_operation_cancel(self, operation_id: str) -> None:
        """Persist owner cancellation intent before signalling the backend task."""
        async with self.mutation():
            self.snapshot = await self.store.request_operation_cancel(
                self.session_id,
                run_id=self.run_id,
                operation_id=operation_id,
                expected_epoch=self.writer_epoch,
                expected_revision=self.revision,
            )

    async def complete_operation(self, record: OperationRecord, envelope: InputEnvelope | None) -> None:
        """Commit backend completion and model input in one fenced transaction."""
        async with self.mutation():
            self.snapshot = await self.store.complete_operation(
                self.session_id,
                record=record,
                envelope=envelope,
                expected_epoch=self.writer_epoch,
                expected_revision=self.revision,
            )
