import asyncio
import fcntl
import logging
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

from pleiad.contracts import JsonObject
from pleiad.errors.base import ErrorType, PleiadError, SystemFault
from pleiad.logging.context import bind_log_context
from pleiad.observability.tracing import Observability
from pleiad.orchestration.graph.state import GraphStatus, NodeStatus
from pleiad.runtime.lifecycle.state import RunStatus
from pleiad.tools.contracts import ToolResult
from pleiad.tools.interruption import InterruptionResponse

from pleiad_weave.agent.weave import WeaveAgent
from pleiad_weave.application.dto import ApprovalRequest, ExecuteRequest
from pleiad_weave.application.errors import ApprovalConflictError, ErrorRecord
from pleiad_weave.application.events import (
    ExecutionEvent,
    ExecutionEventBus,
    ExecutionEventListener,
    current_trace_ids,
    execution_event_from_record,
)
from pleiad_weave.infrastructure.logging import shutdown_logging
from pleiad_weave.infrastructure.storage.models import (
    ApprovalDecision,
    ExecutionEventInput,
    ExecutionRecord,
    ExecutionResumeState,
    ExecutionStatus,
)
from pleiad_weave.infrastructure.storage.repository import ExecutionRepository

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ExecutionService:
    """Own durable product jobs and their process-local SDK invocation tasks."""

    weave: WeaveAgent
    repository: ExecutionRepository
    event_bus: ExecutionEventBus
    observability: Observability
    tasks: dict[str, asyncio.Task[ExecutionRecord]] = field(default_factory=dict, init=False)
    expiry_tasks: dict[str, asyncio.Task[None]] = field(default_factory=dict, init=False)
    state_signals: dict[str, asyncio.Event] = field(default_factory=dict, init=False)
    owner_descriptor: int | None = field(default=None, init=False)
    ownership_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)
    closed: bool = field(default=False, init=False)

    @property
    def model_name(self) -> str:
        """Expose configured product metadata without traversing SDK internals."""
        return self.weave.models["text"].model

    async def start(self) -> None:
        """Schedule queued jobs and explicitly fail jobs interrupted by a restart."""
        if self.closed:
            raise RuntimeError("Execution service is shut down")
        await self.ensure_owner()
        settled = await asyncio.to_thread(self.repository.settled_resume_states)
        for settled_record, settled_state in settled:
            await self.weave.recover_conversation(settled_record.conversation_id, settled_state.checkpoint.turn_id)
        records = await asyncio.to_thread(self.repository.list_nonterminal)
        settled_ids = await asyncio.to_thread(self.repository.settled_execution_ids)
        lost_ids = frozenset(record.execution_id for record in records if record.status is ExecutionStatus.RUNNING)
        recovery_ids = (settled_ids | lost_ids) - self.tasks.keys()
        if recovery_ids:
            await wait_owned_operation(asyncio.create_task(asyncio.to_thread(self.recover_graph_records, recovery_ids)))
        for record in records:
            if record.execution_id in self.tasks:
                continue
            if record.status is ExecutionStatus.RUNNING:
                recovery = await asyncio.to_thread(self.repository.resume_state, record.execution_id)
                await self.finish_paused(
                    record,
                    ExecutionStatus.FAILED,
                    None if recovery is None else recovery.checkpoint.turn_id,
                    error=ErrorRecord(
                        code="weave.execution_unresumable",
                        message="Process restarted without a persisted Run checkpoint.",
                        type=ErrorType.SYSTEM,
                    ),
                )
        for record in records:
            if record.status is ExecutionStatus.WAITING_APPROVAL:
                state = await asyncio.to_thread(self.repository.resume_state, record.execution_id)
                active = await self.weave.memory.session(record.conversation_id).active()
                if state is None or active is None or active.turn_id != state.checkpoint.turn_id:
                    await self.finish(
                        record,
                        ExecutionStatus.FAILED,
                        error=ErrorRecord(
                            code="weave.approval_state_invalid",
                            message="The paused Turn does not match its checkpoint.",
                            type=ErrorType.SYSTEM,
                        ),
                    )
                else:
                    await self.expire(record.execution_id)
                    latest = await asyncio.to_thread(self.repository.get, record.execution_id)
                    if latest is not None and latest.status is ExecutionStatus.WAITING_APPROVAL:
                        self.arm_expiry(record.execution_id, state.checkpoint.deadline)
            if record.status is ExecutionStatus.QUEUED and record.execution_id not in self.tasks:
                self.schedule(record.execution_id)
            if record.status is ExecutionStatus.RESUME_QUEUED:
                self.schedule(record.execution_id)

    def recover_graph_records(self, execution_ids: frozenset[str]) -> None:
        """Settle only unfinished Graph and Node attempts belonging to lost owners.

        A lost Graph cannot continue scheduling and is marked failed. A Node's
        external effect may already have happened, so its outcome stays unknown.
        Confirmed terminal records, identifiers, results, and artifacts survive.

        Args:
            execution_ids: Settled or abandoned Executions that have no live task.

        Raises:
            SystemFault: If subordinate records cannot be read or persisted.
        """
        finished_at = datetime.now(UTC)
        graph_repository = self.weave.graph_executor.graphs
        for record in graph_repository.list_records():
            if record.execution_id not in execution_ids or record.status not in {
                GraphStatus.RUNNING,
                GraphStatus.PAUSED,
            }:
                continue
            graph_repository.save(
                record.model_copy(
                    update={
                        "status": GraphStatus.FAILED,
                        "finished_at": finished_at if record.finished_at is None else record.finished_at,
                        "error": record.error
                        if record.error is not None
                        else ErrorRecord(
                            code="weave.graph_execution_lost",
                            message="The Graph owner is no longer active; Graph scheduling cannot be resumed.",
                            type=ErrorType.SYSTEM,
                        ),
                    }
                )
            )
        node_repository = self.weave.graph_executor.nodes
        for attempt in node_repository.list_records():
            if attempt.execution_id not in execution_ids or attempt.status not in {
                NodeStatus.PENDING,
                NodeStatus.READY,
                NodeStatus.RUNNING,
                NodeStatus.PAUSED,
            }:
                continue
            node_repository.save(
                attempt.model_copy(
                    update={
                        "status": NodeStatus.UNKNOWN,
                        "finished_at": finished_at if attempt.finished_at is None else attempt.finished_at,
                        "error": attempt.error
                        if attempt.error is not None
                        else ErrorRecord(
                            code="weave.node_execution_lost",
                            message="The Node outcome is unknown because its owner is no longer active.",
                            type=ErrorType.SYSTEM,
                        ),
                    }
                )
            )

    async def submit(self, request: ExecuteRequest) -> ExecutionRecord:
        """Persist and schedule an Execution before returning its durable identity."""
        if self.closed:
            raise RuntimeError("Execution service is shut down")
        await self.ensure_owner()
        task = asyncio.create_task(self.create_execution(request))
        try:
            queued = await asyncio.shield(task)
        except asyncio.CancelledError:
            queued = await task
            self.schedule(queued.execution_id)
            raise
        self.schedule(queued.execution_id)
        return queued

    async def create_execution(self, request: ExecuteRequest) -> ExecutionRecord:
        """Commit a queued job and its first event in one transaction."""
        record, event = await asyncio.to_thread(
            self.repository.create,
            ExecutionRecord(
                execution_id=uuid4().hex,
                conversation_id=request.conversation_id,
                objective=request.objective,
                input=request.input,
                status=ExecutionStatus.QUEUED,
            ),
            event=ExecutionEventInput(event_type="execution.queued", data={"objective": request.objective}),
        )
        self.event_bus.publish(execution_event_from_record(event))
        return record

    def schedule(self, execution_id: str) -> asyncio.Task[ExecutionRecord]:
        """Retain and observe the task until its product state has settled."""
        if self.closed:
            raise RuntimeError("Execution service is shut down")
        existing = self.tasks.get(execution_id)
        if existing is not None and not existing.done():
            return existing
        task = asyncio.create_task(self.run(execution_id), name=f"execution:{execution_id}")
        self.tasks[execution_id] = task
        self.signal(execution_id)
        task.add_done_callback(lambda completed: self.task_finished(execution_id, completed))
        return task

    def task_finished(self, execution_id: str, task: asyncio.Task[ExecutionRecord]) -> None:
        """Release a settled task and log system failures with their traceback."""
        if self.tasks.get(execution_id) is task:
            self.tasks.pop(execution_id, None)
        self.signal(execution_id)
        if not task.cancelled() and (error := task.exception()) is not None:
            logger.error(
                f"Execution task failed execution_id={execution_id}", exc_info=(type(error), error, error.__traceback__)
            )

    async def run(self, execution_id: str) -> ExecutionRecord:
        """Serialize one conversation and keep later jobs queued behind approvals."""
        await self.ensure_owner()
        record = await asyncio.to_thread(self.repository.get, execution_id)
        if record is None:
            raise LookupError(f"Execution not found: {execution_id}")
        async with self.weave.memory.session_lock(record.conversation_id):
            records = await asyncio.to_thread(self.repository.list_nonterminal)
            peers = tuple(item for item in records if item.conversation_id == record.conversation_id)
            owner = next(
                (
                    item
                    for item in peers
                    if item.status
                    in {
                        ExecutionStatus.RUNNING,
                        ExecutionStatus.WAITING_APPROVAL,
                        ExecutionStatus.RESUME_QUEUED,
                    }
                ),
                peers[0] if peers else None,
            )
            if owner is not None and owner.execution_id != execution_id:
                latest = await asyncio.to_thread(self.repository.get, execution_id)
                if latest is None:
                    raise LookupError(f"Execution not found: {execution_id}")
                return latest
            try:
                return await self.run_owned(execution_id)
            finally:
                await self.schedule_ready(record.conversation_id)

    async def schedule_ready(self, conversation_id: str) -> None:
        """Dispatch the next queued job only after its conversation owner settles."""
        if self.closed:
            return
        records = await asyncio.to_thread(self.repository.list_nonterminal)
        peers = tuple(item for item in records if item.conversation_id == conversation_id)
        if any(item.status in {ExecutionStatus.RUNNING, ExecutionStatus.WAITING_APPROVAL} for item in peers):
            return
        selected = next(
            (item for item in peers if item.status is ExecutionStatus.RESUME_QUEUED), peers[0] if peers else None
        )
        if selected is not None:
            self.schedule(selected.execution_id)

    async def run_owned(self, execution_id: str) -> ExecutionRecord:
        """Run one SDK invocation while centrally committing product transitions."""
        queued = await asyncio.to_thread(self.repository.get, execution_id)
        if queued is None:
            raise LookupError(f"Execution not found: {execution_id}")
        if queued.status not in {ExecutionStatus.QUEUED, ExecutionStatus.RESUME_QUEUED}:
            return queued
        with (
            bind_log_context(execution_id=execution_id, session_id=queued.conversation_id),
            self.observability.span(
                "weave.execution",
                attributes={"weave.execution_id": execution_id, "weave.session_id": queued.conversation_id},
            ),
        ):
            restored_run_id: str | None = None
            try:
                resuming = queued.status is ExecutionStatus.RESUME_QUEUED
                resume = await asyncio.to_thread(self.repository.resume_state, execution_id) if resuming else None
                if resuming and resume is None:
                    raise ApprovalConflictError("The Execution recovery state is missing.")
                if resume is not None and resume.approval_tool_codes != self.weave.approval_tool_codes:
                    raise ApprovalConflictError("The approval Tool policy changed before resume.")
                if resume is not None and resume.graph_budget is not None:
                    self.weave.graph_executor.restore_budget(resume.checkpoint.run_id, resume.graph_budget)
                    restored_run_id = resume.checkpoint.run_id
                running = await self.transition(
                    queued, ExecutionStatus.RUNNING, "execution.resumed" if resuming else "execution.started"
                )
                result = await self.weave.run(
                    running.objective,
                    running.input,
                    conversation_id=running.conversation_id,
                    execution_id=execution_id,
                    checkpoint=None if resume is None else resume.checkpoint,
                    resolutions=() if resume is None else self.resolutions(resume),
                )
                if result.status is RunStatus.INTERRUPTED:
                    if (
                        result.checkpoint is None
                        or not result.interruptions
                        or any(item.details.get("kind") != "weave.approval" for item in result.interruptions)
                    ):
                        raise ApprovalConflictError("The SDK interruption is not a supported Weave approval.")
                    state = ExecutionResumeState(
                        checkpoint=result.checkpoint,
                        approval_tool_codes=self.weave.approval_tool_codes,
                        graph_budget=self.weave.graph_executor.budget_state(result.run_id),
                        decisions=() if resume is None else resume.decisions,
                    )
                    self.weave.graph_executor.release_budget(result.run_id)
                    paused = await self.transition(
                        running, ExecutionStatus.WAITING_APPROVAL, "execution.waiting_approval", resume_state=state
                    )
                    self.arm_expiry(execution_id, result.checkpoint.deadline)
                    return paused
                output = result.structured_output
                if output is None and result.final_output:
                    output = {"text": result.final_output}
                status = ExecutionStatus.SUCCEEDED if result.status is RunStatus.SUCCEEDED else ExecutionStatus.FAILED
                error = None if result.error is None else ErrorRecord.from_exception(result.error)
                if result.status is not RunStatus.SUCCEEDED and error is None:
                    error = ErrorRecord(
                        code=f"weave.run_{result.status.value}",
                        message=f"SDK Run ended with status {result.status.value}.",
                        type=ErrorType.BUSINESS,
                    )
                return await self.finish(running, status, output=output, error=error)
            except asyncio.CancelledError:
                current = await asyncio.to_thread(self.repository.get, execution_id)
                if current is None:
                    raise LookupError("Execution disappeared during cancellation") from None
                if current.status in {ExecutionStatus.QUEUED, ExecutionStatus.RUNNING}:
                    cancelled_resume = await asyncio.to_thread(self.repository.resume_state, execution_id)
                    await self.finish_paused(
                        current,
                        ExecutionStatus.CANCELLED,
                        None if cancelled_resume is None else cancelled_resume.checkpoint.turn_id,
                    )
                raise
            except ApprovalConflictError as error:
                current = await asyncio.to_thread(self.repository.get, execution_id)
                if current is None:
                    raise LookupError("Execution disappeared during recovery validation") from error
                recovery = await asyncio.to_thread(self.repository.resume_state, execution_id)
                return await self.finish_paused(
                    current,
                    ExecutionStatus.FAILED,
                    None if recovery is None else recovery.checkpoint.turn_id,
                    error=ErrorRecord(
                        code="weave.approval_incompatible",
                        message=str(error),
                        type=ErrorType.SYSTEM,
                    ),
                )
            except PleiadError as error:
                current = await asyncio.to_thread(self.repository.get, execution_id)
                if current is None:
                    raise LookupError("Execution disappeared during failure") from error
                failed_resume = await asyncio.to_thread(self.repository.resume_state, execution_id)
                await self.finish_paused(
                    current,
                    ExecutionStatus.FAILED,
                    None if failed_resume is None else failed_resume.checkpoint.turn_id,
                    error=ErrorRecord.from_exception(error),
                )
                raise
            except Exception as error:
                current = await asyncio.to_thread(self.repository.get, execution_id)
                if current is None:
                    raise LookupError("Execution disappeared during failure") from error
                fault = SystemFault(
                    "weave.execution_unexpected", f"Unexpected execution failure: {type(error).__name__}."
                )
                unexpected_resume = await asyncio.to_thread(self.repository.resume_state, execution_id)
                await self.finish_paused(
                    current,
                    ExecutionStatus.FAILED,
                    None if unexpected_resume is None else unexpected_resume.checkpoint.turn_id,
                    error=ErrorRecord.from_exception(fault),
                )
                raise fault from error
            finally:
                if restored_run_id is not None:
                    self.weave.graph_executor.release_budget(restored_run_id)

    async def transition(
        self,
        current: ExecutionRecord,
        status: ExecutionStatus,
        event_type: str,
        *,
        output: JsonObject | None = None,
        error: ErrorRecord | None = None,
        resume_state: ExecutionResumeState | None = None,
        event_data: JsonObject | None = None,
    ) -> ExecutionRecord:
        """Complete a durable transition and dispatch its event before cancellation."""
        graph_ref = self.weave.graph_for_execution(current.execution_id)
        updated = current.model_copy(
            update={
                "status": status,
                "output": output,
                "error": error,
                "updated_at": datetime.now(UTC),
                "graph_id": current.graph_id if graph_ref is None else graph_ref[0],
                "graph_revision": current.graph_revision if graph_ref is None else graph_ref[1],
            }
        )
        trace_id, span_id = current_trace_ids(current.execution_id)
        task = asyncio.create_task(
            asyncio.to_thread(
                self.repository.transition,
                updated,
                expected_revision=current.revision,
                resume_state=resume_state,
                event=ExecutionEventInput(
                    event_type=event_type,
                    data={
                        "status": status.value,
                        "error": None if error is None else error.model_dump(mode="json"),
                        **({} if event_data is None else event_data),
                    },
                    trace_id=trace_id,
                    span_id=span_id,
                ),
            )
        )
        cancelled = False
        try:
            changed = await asyncio.shield(task)
        except asyncio.CancelledError:
            changed = await task
            cancelled = True
        if changed is None:
            latest = await asyncio.to_thread(self.repository.get, current.execution_id)
            if latest is None:
                raise LookupError("Execution disappeared during transition")
            saved = latest
        else:
            saved, event = changed
            self.event_bus.publish(execution_event_from_record(event))
        self.signal(current.execution_id)
        if cancelled:
            raise asyncio.CancelledError
        return saved

    async def finish(
        self,
        current: ExecutionRecord,
        status: ExecutionStatus,
        *,
        output: JsonObject | None = None,
        error: ErrorRecord | None = None,
    ) -> ExecutionRecord:
        """Commit the terminal product status independently from SDK Run state."""
        event_type = {
            ExecutionStatus.SUCCEEDED: "execution.completed",
            ExecutionStatus.CANCELLED: "execution.cancelled",
            ExecutionStatus.FAILED: "execution.failed",
        }[status]
        return await self.transition(current, status, event_type, output=output, error=error)

    async def wait(self, execution_id: str) -> ExecutionRecord | None:
        """Join an Execution without transferring cancellation ownership to a caller."""
        signal = self.state_signals.setdefault(execution_id, asyncio.Event())
        while True:
            signal.clear()
            task = self.tasks.get(execution_id)
            if task is not None:
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    if not task.cancelled():
                        raise
            record = await asyncio.to_thread(self.repository.get, execution_id)
            if (
                record is None
                or record.status not in {ExecutionStatus.QUEUED, ExecutionStatus.RESUME_QUEUED}
                or self.closed
            ):
                if (
                    record is None
                    or record.status
                    in {
                        ExecutionStatus.SUCCEEDED,
                        ExecutionStatus.FAILED,
                        ExecutionStatus.CANCELLED,
                    }
                    or self.closed
                ):
                    self.state_signals.pop(execution_id, None)
                return record
            await signal.wait()

    def signal(self, execution_id: str) -> None:
        """Wake callers after owned work or its persisted state changes."""
        event = self.state_signals.get(execution_id)
        if event is not None:
            event.set()

    async def approvals(self, execution_id: str | None = None) -> tuple[ApprovalRequest, ...]:
        """List persisted pending actions for this single workspace owner."""
        if execution_id is None:
            records = await asyncio.to_thread(self.repository.list_nonterminal)
        else:
            record = await asyncio.to_thread(self.repository.get, execution_id)
            if record is None:
                raise LookupError(f"Execution not found: {execution_id}")
            records = (record,)
        pending: list[ApprovalRequest] = []
        for record in records:
            if record.status is not ExecutionStatus.WAITING_APPROVAL:
                continue
            if self.owner_descriptor is not None:
                await self.expire(record.execution_id)
            latest = await asyncio.to_thread(self.repository.get, record.execution_id)
            if latest is None or latest.status is not ExecutionStatus.WAITING_APPROVAL:
                continue
            state = await asyncio.to_thread(self.repository.resume_state, record.execution_id)
            if state is None:
                raise ApprovalConflictError("The waiting Execution recovery state is missing.")
            pending.extend(
                ApprovalRequest(execution_id=record.execution_id, interruption=item, deadline=state.checkpoint.deadline)
                for item in state.checkpoint.interruptions
                if not any(decision.interruption_id == item.interruption_id for decision in state.decisions)
            )
        return tuple(pending)

    async def decide(
        self, execution_id: str, interruption_id: str, decision: Literal["approve", "reject"]
    ) -> ExecutionRecord:
        """Persist an idempotent owner decision before scheduling the original Run.

        Args:
            execution_id: Existing durable product job.
            interruption_id: Exact action shown by the pending approval endpoint.
            decision: Approve or reject that action; no replacement result is accepted.

        Returns:
            The persisted Execution after accepting the decision, or its existing state on an identical retry.

        Raises:
            LookupError: If the Execution does not exist.
            ApprovalConflictError: If the action is stale, expired, or already decided differently.
        """
        if self.closed:
            raise RuntimeError("Execution service is shut down")
        await self.ensure_owner()
        record = await asyncio.to_thread(self.repository.get, execution_id)
        if record is None:
            raise LookupError(f"Execution not found: {execution_id}")
        async with self.weave.memory.session_lock(record.conversation_id):
            current = await asyncio.to_thread(self.repository.get, execution_id)
            state = await asyncio.to_thread(self.repository.resume_state, execution_id)
            if current is None or state is None:
                raise ApprovalConflictError("The Execution has no persisted approval state.")
            existing = next((item for item in state.decisions if item.interruption_id == interruption_id), None)
            if existing is not None:
                if existing.decision != decision:
                    raise ApprovalConflictError("This action already has a different decision.")
                if current.status is ExecutionStatus.RESUME_QUEUED:
                    self.schedule(execution_id)
                return current
            if current.status is not ExecutionStatus.WAITING_APPROVAL:
                raise ApprovalConflictError("The Execution is not waiting for approval.")
            if not any(item.interruption_id == interruption_id for item in state.checkpoint.interruptions):
                raise ApprovalConflictError("The pending action does not belong to this Execution.")
            if state.checkpoint.deadline <= datetime.now(UTC):
                await self.expire_owned(current, state)
                raise ApprovalConflictError("The original Run deadline expired before this decision.")
            updated = state.model_copy(
                update={
                    "decisions": (
                        *state.decisions,
                        ApprovalDecision(interruption_id=interruption_id, decision=decision),
                    ),
                }
            )
            accepted = await self.transition(
                current,
                ExecutionStatus.RESUME_QUEUED,
                "execution.resume_queued",
                resume_state=updated,
                event_data={"interruption_id": interruption_id, "decision": decision},
            )
            if accepted.status is not ExecutionStatus.RESUME_QUEUED:
                raise ApprovalConflictError("The Execution changed before this decision was committed.")
            expiry = self.expiry_tasks.pop(execution_id, None)
            if expiry is not None:
                expiry.cancel()
            self.schedule(execution_id)
            return accepted

    @staticmethod
    def resolutions(state: ExecutionResumeState) -> tuple[InterruptionResponse, ...]:
        """Create SDK responses from persisted decisions without trusting client results."""
        decisions = {item.interruption_id: item for item in state.decisions}
        responses: list[InterruptionResponse] = []
        for interruption in state.checkpoint.interruptions:
            decision = decisions.get(interruption.interruption_id)
            if decision is None:
                continue
            approved = decision.decision == "approve"
            responses.append(
                InterruptionResponse(
                    interruption_id=interruption.interruption_id,
                    result=ToolResult(
                        call_id=interruption.call_id,
                        ok=approved,
                        output={"approved": True} if approved else None,
                        error_code=None if approved else "weave.approval_denied",
                        error_message=None if approved else "The workspace owner rejected this action.",
                        error_type=None if approved else ErrorType.BUSINESS,
                        started_at=decision.decided_at,
                        finished_at=decision.decided_at,
                    ),
                )
            )
        return tuple(responses)

    def arm_expiry(self, execution_id: str, deadline: datetime) -> None:
        """Own one independent deadline watcher for a persisted waiting job."""
        previous = self.expiry_tasks.pop(execution_id, None)
        if previous is not None:
            previous.cancel()
        task = asyncio.create_task(self.expire_at(execution_id, deadline), name=f"approval-deadline:{execution_id}")
        self.expiry_tasks[execution_id] = task
        task.add_done_callback(lambda completed: self.expiry_finished(execution_id, completed))

    def expiry_finished(self, execution_id: str, task: asyncio.Task[None]) -> None:
        """Observe deadline failures and release only the matching watcher."""
        if self.expiry_tasks.get(execution_id) is task:
            self.expiry_tasks.pop(execution_id, None)
        if not task.cancelled() and (error := task.exception()) is not None:
            logger.error(
                f"Approval deadline failed execution_id={execution_id}",
                exc_info=(type(error), error, error.__traceback__),
            )

    async def expire_at(self, execution_id: str, deadline: datetime) -> None:
        """Expire waiting approval without extending the original SDK deadline."""
        await asyncio.sleep(max(0.0, (deadline - datetime.now(UTC)).total_seconds()))
        await self.expire(execution_id)

    async def expire(self, execution_id: str) -> ExecutionRecord | None:
        """Settle an expired waiting job while serializing its conversation."""
        record = await asyncio.to_thread(self.repository.get, execution_id)
        if record is None or record.status is not ExecutionStatus.WAITING_APPROVAL:
            return record
        async with self.weave.memory.session_lock(record.conversation_id):
            current = await asyncio.to_thread(self.repository.get, execution_id)
            state = await asyncio.to_thread(self.repository.resume_state, execution_id)
            if (
                current is not None
                and current.status is ExecutionStatus.WAITING_APPROVAL
                and state is not None
                and state.checkpoint.deadline <= datetime.now(UTC)
            ):
                return await self.expire_owned(current, state)
            return current

    async def expire_owned(self, record: ExecutionRecord, state: ExecutionResumeState) -> ExecutionRecord:
        """Fail an expired approval and release its exact Turn before queued work."""
        return await self.finish_paused(
            record,
            ExecutionStatus.FAILED,
            state.checkpoint.turn_id,
            error=ErrorRecord(
                code="weave.approval_expired",
                message="The original Run deadline expired while awaiting approval.",
                type=ErrorType.BUSINESS,
            ),
        )

    async def finish_paused(
        self,
        record: ExecutionRecord,
        status: ExecutionStatus,
        turn_id: str | None,
        *,
        error: ErrorRecord | None = None,
    ) -> ExecutionRecord:
        """Finish the persisted status and its owned Turn before propagating cancellation."""
        caller = asyncio.current_task()

        async def settle() -> ExecutionRecord:
            completed = await self.finish(record, status, error=error)
            await self.weave.recover_conversation(record.conversation_id, turn_id)
            expiry = self.expiry_tasks.pop(record.execution_id, None)
            if expiry is not None and expiry is not caller:
                expiry.cancel()
            await self.schedule_ready(record.conversation_id)
            return completed

        return await wait_owned_operation(asyncio.create_task(settle()))

    async def cancel(self, execution_id: str) -> ExecutionRecord | None:
        """Cancel owned work and await its durable terminal transition."""
        await self.ensure_owner()
        task = self.tasks.get(execution_id)
        if task is not None:
            task.cancel()
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                if not task.cancelled():
                    raise
        current = await asyncio.to_thread(self.repository.get, execution_id)
        if current is not None and current.status is ExecutionStatus.QUEUED:
            cancelled = await self.finish(current, ExecutionStatus.CANCELLED)
            if cancelled.status is ExecutionStatus.RUNNING:
                return await self.cancel(execution_id)
            await self.schedule_ready(current.conversation_id)
            return cancelled
        if current is not None and current.status in {
            ExecutionStatus.QUEUED,
            ExecutionStatus.RUNNING,
            ExecutionStatus.WAITING_APPROVAL,
            ExecutionStatus.RESUME_QUEUED,
        }:
            async with self.weave.memory.session_lock(current.conversation_id):
                latest = await asyncio.to_thread(self.repository.get, execution_id)
                if latest is None:
                    return None
                if latest.status not in {
                    ExecutionStatus.QUEUED,
                    ExecutionStatus.RUNNING,
                    ExecutionStatus.WAITING_APPROVAL,
                    ExecutionStatus.RESUME_QUEUED,
                }:
                    return latest
                state = await asyncio.to_thread(self.repository.resume_state, execution_id)
                current = await self.finish_paused(
                    latest, ExecutionStatus.CANCELLED, None if state is None else state.checkpoint.turn_id
                )
        return current

    async def shutdown(self) -> None:
        """Cancel all owned jobs, close model clients, and restore process logging."""
        if self.closed:
            return
        self.closed = True
        try:
            expiry_tasks = tuple(self.expiry_tasks.values())
            for expiry in expiry_tasks:
                expiry.cancel()
            if expiry_tasks:
                await asyncio.gather(*expiry_tasks, return_exceptions=True)
            for signal in self.state_signals.values():
                signal.set()
            tasks = tuple(self.tasks.items())
            for _, task in tasks:
                task.cancel()
            for execution_id, task in tasks:
                try:
                    await task
                except asyncio.CancelledError:
                    if not task.cancelled():
                        raise
                except PleiadError:
                    logger.exception(f"Execution failed during shutdown execution_id={execution_id}")
                current = await asyncio.to_thread(self.repository.get, execution_id)
                if current is not None and current.status in {ExecutionStatus.QUEUED, ExecutionStatus.RUNNING}:
                    await self.finish(current, ExecutionStatus.CANCELLED)
        finally:
            try:
                try:
                    await self.weave.aclose()
                finally:
                    shutdown_logging()
            finally:
                if self.owner_descriptor is not None:
                    os.close(self.owner_descriptor)
                    self.owner_descriptor = None

    async def ensure_owner(self) -> None:
        """Acquire the workspace's single lifetime owner before scheduling or recovery."""
        if self.closed:
            raise RuntimeError("Execution service is shut down")
        async with self.ownership_lock:
            if self.owner_descriptor is None:
                task = asyncio.create_task(asyncio.to_thread(self.acquire_owner))
                cancelled = False
                while True:
                    try:
                        descriptor = await asyncio.shield(task)
                    except asyncio.CancelledError:
                        if task.cancelled():
                            raise
                        cancelled = True
                        continue
                    except (OSError, SystemFault) as error:
                        if cancelled:
                            raise asyncio.CancelledError from error
                        raise
                    self.owner_descriptor = descriptor
                    if cancelled:
                        raise asyncio.CancelledError
                    return

    def acquire_owner(self) -> int:
        """Hold a kernel lock without deleting or replacing the shared lock file."""
        descriptor = os.open(self.repository.path.with_suffix(".owner.lock"), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            os.close(descriptor)
            raise SystemFault(
                "weave.workspace_in_use",
                "Another service owns this workspace. Use that service's API or TUI to approve actions.",
            ) from error
        except OSError as error:
            os.close(descriptor)
            raise SystemFault(
                "weave.workspace_lock_failed", "The workspace owner lock could not be acquired."
            ) from error
        return descriptor

    def get(self, execution_id: str) -> ExecutionRecord | None:
        """Read a persisted product summary."""
        return self.repository.get(execution_id)

    async def events(self, execution_id: str, *, after_sequence: int = 0) -> tuple[ExecutionEvent, ...]:
        """Replay committed product events after an exclusive sequence cursor."""
        records = await asyncio.to_thread(self.repository.read_events, execution_id, after_sequence=after_sequence)
        return tuple(execution_event_from_record(record) for record in records)

    def subscribe_events(self, listener: ExecutionEventListener) -> None:
        """Subscribe a UI to future product events."""
        self.event_bus.subscribe(listener)

    def unsubscribe_events(self, listener: ExecutionEventListener) -> None:
        """Remove a UI subscription before it is destroyed."""
        self.event_bus.unsubscribe(listener)


async def wait_owned_operation[ResultT](task: asyncio.Task[ResultT]) -> ResultT:
    """Finish owned work before propagating repeated caller cancellation.

    Args:
        task: Work whose completion or cleanup belongs to the caller.

    Returns:
        The completed operation result when the caller was not cancelled.

    Raises:
        asyncio.CancelledError: After owned work settles if cancellation arrived.
        Exception: The operation's original failure when no cancellation arrived.
    """
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.cancelled():
                raise
            cancelled = True
            continue
        except Exception as failure:
            if cancelled:
                raise asyncio.CancelledError from failure
            raise
        if cancelled:
            raise asyncio.CancelledError
        return result
