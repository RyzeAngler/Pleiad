import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, uuid5

from pleiad.messages.results import OperationResultReference
from pleiad.sessions.input import InputEnvelope
from pleiad.sessions.records import OperationRecord
from pleiad.sessions.writer import SessionWriter
from pleiad.tools.base import BackgroundAdmission, BaseTool
from pleiad.tools.contracts import ToolAcceptance, ToolCall, ToolContext, ToolResult, ToolSubmission
from pleiad.tools.errors import BackgroundNotAcceptedError, ToolCancelledError, ToolTimeoutError
from pleiad.tools.interruption import ToolAuthorization, ToolInterruption
from pleiad.tools.runtime import ToolRuntime


class OperationScope:
    """Own accepted background tools and their durable completion notifications."""

    def __init__(self, writer: SessionWriter, runtime: ToolRuntime, concurrency: int) -> None:
        """Bind the Run writer and resource slots retained until actual settlement."""
        self.writer = writer
        self.runtime = runtime
        self.semaphore = asyncio.Semaphore(concurrency)
        self.group = asyncio.TaskGroup()
        self.tasks: dict[str, asyncio.Task[ToolResult]] = {}
        self.changed = asyncio.Event()

    async def __aenter__(self) -> "OperationScope":
        """Enter the scope that owns all related backend tasks."""
        await self.group.__aenter__()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        """Stop remaining backend work and wait for its confirmed cleanup."""
        try:
            for operation_id, task in self.tasks.items():
                if not task.done():
                    await self.writer.request_operation_cancel(operation_id)
        finally:
            for task in self.tasks.values():
                if not task.done():
                    task.cancel()
            await self.group.__aexit__(None, None, None)

    @property
    def running(self) -> bool:
        """Return whether this Run still owns unsettled backend work."""
        return any(not task.done() for task in self.tasks.values())

    async def start(
        self, tool: BaseTool, call: ToolCall, context: ToolContext, *, authorization: ToolAuthorization | None = None
    ) -> ToolResult:
        """Accept an operation and return its single model-visible startup receipt."""
        operation_id = uuid5(NAMESPACE_URL, f"pleiad:{self.writer.run_id}:{call.call_id}").hex
        now = datetime.now(UTC)
        started = self.writer.observability.start_time()
        record = OperationRecord(
            operation_id=operation_id, run_id=self.writer.run_id, call_id=call.call_id, state="prepared"
        )
        await self.writer.save_operation(record)
        accepted = asyncio.Event()
        admission = BackgroundAdmission(
            operation_id, on_prepare=self.submission_writer(record), on_accept=self.acceptance_writer(record, accepted)
        )
        task = self.group.create_task(
            self.execute(tool, call, context, record, admission, accepted, authorization, started),
            name=f"pleiad-operation-{operation_id}",
        )
        self.tasks[operation_id] = task
        try:
            remaining = (
                None
                if context.parent_limits is None
                else max(0.0, (context.parent_limits.deadline - datetime.now(UTC)).total_seconds())
            )
            async with asyncio.timeout(remaining):
                await accepted.wait()
        except TimeoutError:
            await self.writer.request_operation_cancel(operation_id)
            task.cancel()
            return self.runtime.failure(
                call.call_id, now, ToolTimeoutError("The Run deadline expired before backend admission.")
            )
        completed = await task if task.done() else None
        receipt = admission.receipt
        if receipt is None:
            return await task if completed is None else completed
        return ToolResult(
            call_id=call.call_id,
            ok=True,
            output={
                "status": "started",
                "operation_id": operation_id,
                "backend": receipt.backend,
                "reference": receipt.reference,
            },
            started_at=now,
            finished_at=datetime.now(UTC),
        )

    def submission_writer(self, record: OperationRecord) -> Callable[[ToolSubmission], Awaitable[None]]:
        """Bind the exact resolved request to its existing operation intention."""

        async def prepare(submission: ToolSubmission) -> None:
            await self.writer.save_operation(record.model_copy(update={"submission": submission}))

        return prepare

    def acceptance_writer(
        self, record: OperationRecord, accepted: asyncio.Event
    ) -> Callable[[ToolAcceptance], Awaitable[None]]:
        """Persist concrete backend admission before permitting model-visible startup."""

        async def accept(receipt: ToolAcceptance) -> None:
            running = record.model_copy(
                update={
                    "state": "running",
                    "backend_receipt": receipt,
                    "submission": ToolSubmission(
                        operation_id=receipt.operation_id, request_digest=receipt.request_digest
                    ),
                }
            )
            await self.writer.save_operation(running)
            self.writer.observability.metrics.record_operation_started()
            accepted.set()

        return accept

    async def execute(
        self,
        tool: BaseTool,
        call: ToolCall,
        context: ToolContext,
        record: OperationRecord,
        admission: BackgroundAdmission,
        accepted: asyncio.Event,
        authorization: ToolAuthorization | None,
        started: float,
    ) -> ToolResult:
        """Execute admitted work or return its confirmed pre-admission rejection."""
        try:
            async with self.semaphore:
                result = await self.runtime.execute(
                    tool, call, context, admission=admission, authorization=authorization
                )
            if isinstance(result, ToolInterruption):
                raise ValueError("A background tool must resolve authorization before backend admission")
            receipt = admission.receipt
            if receipt is None and result.ok:
                result = self.runtime.failure(call.call_id, result.started_at, BackgroundNotAcceptedError())
            completed = record.model_copy(
                update={
                    "state": "succeeded" if result.ok else "failed",
                    "result": result,
                    "backend_receipt": receipt,
                    "submission": admission.submission,
                    "result_ref": f"operation:{record.operation_id}:result",
                    "ended_at": datetime.now(UTC),
                }
            )
            envelope = (
                None
                if receipt is None
                else InputEnvelope(
                    input_id=f"operation:{record.operation_id}:result",
                    kind="operation_result",
                    source="pleiad.operation",
                    causation_id=record.operation_id,
                    content=OperationResultReference.from_result(record.operation_id, result).input_reference(),
                )
            )
            await self.writer.complete_operation(completed, envelope)
            self.writer.observability.metrics.record_operation_finished(
                completed.state, self.writer.observability.duration(started), accepted=accepted.is_set()
            )
            return result
        except asyncio.CancelledError as error:
            operations = await self.writer.store.operations(self.writer.session_id, self.writer.run_id)
            persisted = next(value for value in operations if value.operation_id == record.operation_id)
            if persisted.state in {"succeeded", "failed", "cancelled"}:
                self.writer.observability.metrics.record_operation_finished(
                    persisted.state, self.writer.observability.duration(started), accepted=accepted.is_set()
                )
                raise
            confirmed = not admission.entered or (isinstance(error, ToolCancelledError) and error.confirmed_stop)
            receipt = admission.receipt
            settled = record.model_copy(
                update={
                    "state": "cancelled" if confirmed else "unknown",
                    "ended_at": datetime.now(UTC),
                    "backend_receipt": receipt,
                    "submission": admission.submission,
                }
            )
            await self.writer.complete_operation(settled, None)
            self.writer.observability.metrics.record_operation_finished(
                settled.state, self.writer.observability.duration(started), accepted=accepted.is_set()
            )
            raise
        except Exception:
            receipt = admission.receipt
            await self.writer.complete_operation(
                record.model_copy(
                    update={
                        "state": "unknown",
                        "ended_at": datetime.now(UTC),
                        "backend_receipt": receipt,
                        "submission": admission.submission,
                    }
                ),
                None,
            )
            self.writer.observability.metrics.record_operation_finished(
                "unknown", self.writer.observability.duration(started), accepted=accepted.is_set()
            )
            raise
        finally:
            accepted.set()
            self.changed.set()

    async def wait(self) -> None:
        """Wait for completion or newly accepted live input at a safe boundary."""
        await self.writer.waiting_event(True)
        try:
            while self.running:
                self.changed.clear()
                if await self.ready_inputs():
                    return
                try:
                    async with asyncio.timeout(0.05):
                        await self.changed.wait()
                    return
                except TimeoutError:
                    continue
        finally:
            await self.writer.waiting_event(False)

    async def ready_inputs(self) -> bool:
        """Check eligible inbox facts without admitting another model step."""
        journal = await self.writer.store.snapshot(self.writer.session_id)
        pending = tuple(
            value for value in journal.inputs if value.step_id is None and value.run_id in {None, self.writer.run_id}
        )
        operations = await self.writer.store.operations(self.writer.session_id, self.writer.run_id)
        owned = {operation.operation_id for operation in operations}
        return any(
            (value.envelope.kind == "operation_result" and value.envelope.causation_id in owned)
            or (value.envelope.kind != "operation_result" and self.writer.snapshot.input_mode == "live")
            for value in pending
        )

    async def settle(self) -> None:
        """Finish already admitted backend work before saving a safe suspension."""
        for task in self.tasks.values():
            await task
