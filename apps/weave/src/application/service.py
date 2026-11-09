import asyncio
import fcntl
import logging
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import Literal

from pleiad.contracts import ContractModel, JsonObject
from pleiad.errors.base import ErrorType, PleiadError, SystemFault
from pleiad.events.contracts import Event
from pleiad.events.message import MessageCompletedEvent
from pleiad.events.runtime import ModelStepCompletedEvent
from pleiad.logging.context import bind_log_context
from pleiad.messages.message import ToolMessage
from pleiad.orchestration.graph.state import NodeResume, NodeStatus
from pleiad.runtime.checkpoint import ResumeDecision
from pleiad.runtime.handle import RunHandle
from pleiad.runtime.limits import RunLimits
from pleiad.runtime.result import RunResult, RunSuspension
from pleiad.runtime.runner import Runner
from pleiad.sessions.checkpoint import RunCheckpoint
from pleiad.sessions.records import ReconcileRecord, RunStatus
from pleiad.sessions.recovery import ActionEvidence, ActionRef
from pleiad.sessions.session import Session
from pleiad.sessions.store import SessionStore
from pleiad.skills.loader import SkillLoader
from pleiad.tools.contracts import ToolResult
from pleiad.tools.interruption import ToolInterruption
from pleiad.tools.registry import ToolRegistry
from pleiad.tools.runtime import ToolRuntime
from pydantic import TypeAdapter

from pleiad_weave.agent.graph_controls import GraphControls
from pleiad_weave.agent.weave import CompletionDraft, RouteDraft, WeaveAgents, WorkerBinding
from pleiad_weave.application.recovery import observe_command_actions
from pleiad_weave.application.verification import verify_delivery
from pleiad_weave.infrastructure.metrics import ProductMetrics
from pleiad_weave.infrastructure.storage.model_inputs import MODEL_INPUT_QUEUE_BYTES, input_envelope
from pleiad_weave.infrastructure.storage.observations import DirectoryObserver
from pleiad_weave.infrastructure.storage.product import ProductStore, fingerprint, stable_id
from pleiad_weave.infrastructure.storage.product_content import ProductContent
from pleiad_weave.infrastructure.storage.product_details import NodeAttempt, ProductDetails, ToolResultDetail
from pleiad_weave.infrastructure.storage.product_models import (
    TERMINAL_STATUSES,
    ApprovalAction,
    Artifact,
    ArtifactMetadata,
    Attention,
    ChatMessageRequest,
    CleanupRecord,
    CleanupScope,
    DeletionPreview,
    Delivery,
    Execution,
    ExecutionDetailView,
    ExecutionInputView,
    ExecutionRun,
    ExecutionSpec,
    ExecutionStatus,
    Handback,
    HandbackResolution,
    InboxItem,
    InputConsumption,
    InputDisposition,
    MessageReceipt,
    ProductConflict,
    ProductEvent,
    Report,
    ResolutionReceipt,
    ResolutionRequest,
    RouteAttempt,
    RouteDecision,
    Space,
    Verification,
)

logger = logging.getLogger(__name__)
JSON_OBJECT = TypeAdapter[JsonObject](JsonObject)


class PendingGraphNode(ContractModel):
    """Validate persisted nested requests before translating user decisions."""

    node_id: str
    checkpoint: RunCheckpoint
    interruptions: tuple[ToolInterruption, ...]


PENDING_GRAPH_NODES = TypeAdapter[tuple[PendingGraphNode, ...]](tuple[PendingGraphNode, ...])


class ExecutionService:
    """Own Router scheduling, independent Workers, durable delivery, and user controls."""

    def __init__(
        self,
        agents: WeaveAgents,
        store: ProductStore,
        sessions: SessionStore,
        content: ProductContent,
        details: ProductDetails,
        *,
        max_workers: int = 3,
        observer: DirectoryObserver | None = None,
    ) -> None:
        """Bind product owners separately from SDK invocation mechanisms."""
        if max_workers < 1:
            raise ValueError("At least one Worker slot is required")
        self.agents = agents
        self.metrics = ProductMetrics(agents.observability.meter, store)
        self.store = store
        self.sessions = sessions
        self.content = content
        self.details = details
        self.observer = observer
        self.max_workers = max_workers
        self.admission_open = False
        self.closing = False
        self.owner_descriptor: int | None = None
        self.scheduler: asyncio.Task[None] | None = None
        self.scheduler_ready = asyncio.Event()
        self.group: asyncio.TaskGroup | None = None
        self.wakeup = asyncio.Event()
        self.worker_tasks: dict[str, asyncio.Task[None]] = {}
        self.handles: dict[str, RunHandle] = {}
        self.bindings: dict[str, WorkerBinding] = {}
        self.control_tasks: set[tuple[str, str]] = set()
        self.execution_locks: dict[str, asyncio.Lock] = {}
        self.delivery_lock = asyncio.Lock()
        self.router_task: asyncio.Task[None] | None = None
        self.router_handle: RunHandle | None = None
        self.cleanup_tasks: dict[str, asyncio.Task[JsonObject]] = {}

    @property
    def ready(self) -> bool:
        """Report admission only while the actual scheduler and exclusive owner remain live."""
        return (
            self.admission_open
            and not self.closing
            and self.owner_descriptor is not None
            and self.scheduler is not None
            and not self.scheduler.done()
        )

    def acquire_owner(self) -> int:
        """Acquire the process owner lock before recovering or accepting work."""
        descriptor = os.open(self.store.path.with_suffix(".owner.lock"), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            os.close(descriptor)
            raise SystemFault("weave.workspace_in_use", "Another service owns this workspace.") from error
        except OSError:
            os.close(descriptor)
            raise
        return descriptor

    async def start(self) -> None:
        """Take ownership and reconstruct persisted work before opening admission."""
        if self.ready:
            return
        self.owner_descriptor = await asyncio.to_thread(self.acquire_owner)
        try:
            await self.recover()
            self.closing = False
            self.scheduler_ready.clear()
            self.scheduler = asyncio.create_task(self.run(), name="weave-execution-owner")
            await self.scheduler_ready.wait()
            if self.scheduler.done():
                await self.scheduler
            self.admission_open = True
        except BaseException:
            os.close(self.owner_descriptor)
            self.owner_descriptor = None
            raise

    async def shutdown(self) -> None:
        """Pause safe work and release all process resources even after scheduler failure."""
        self.admission_open = False
        self.closing = True
        self.wakeup.set()
        try:
            async with asyncio.TaskGroup() as group:
                for handle in tuple(self.handles.values()):
                    group.create_task(handle.pause(reason="service_shutdown"))
                if self.router_handle is not None:
                    group.create_task(self.router_handle.result())
        finally:
            try:
                if self.scheduler is not None:
                    await self.scheduler
            finally:
                self.scheduler = None
                try:
                    await self.agents.aclose()
                finally:
                    if self.owner_descriptor is not None:
                        os.close(self.owner_descriptor)
                        self.owner_descriptor = None

    async def submit(self, request: ChatMessageRequest) -> MessageReceipt:
        """Commit one main-chat message and wake its actual input owners."""
        if not self.ready:
            raise SystemFault("weave.not_ready", "The execution service is not ready.")
        started = monotonic()
        receipt = await asyncio.to_thread(self.store.submit_message, request)
        self.metrics.input_count.add(1)
        self.metrics.input_duration.record(monotonic() - started)
        self.wakeup.set()
        return receipt

    async def control(
        self,
        execution_id: str,
        action: Literal["pause", "cancel", "resume"],
        *,
        request_id: str,
        expected_revision: int,
    ) -> Execution:
        """Persist an explicit user control before scheduling its runtime consequence."""
        started_at = monotonic()
        try:
            value = await asyncio.to_thread(
                self.store.control_execution,
                execution_id,
                action,
                request_id=request_id,
                expected_revision=expected_revision,
            )
            if action == "resume":
                if value.status is ExecutionStatus.BLOCKED:
                    await self.reconcile_execution(value)
                    value = await self.require_execution(execution_id)
                if (
                    value.status not in TERMINAL_STATUSES
                    and not value.unknown_effects
                    and value.control_intent != "cancel"
                ):
                    try:
                        await self.agents.validate_space(value.config_snapshot)
                    except (ValueError, LookupError, PleiadError):
                        await self.mark_blocked(value, "resource_unavailable", value.unknown_effects)
                value = await self.require_execution(execution_id)
            self.wakeup.set()
            return value
        finally:
            self.metrics.control_duration.record(monotonic() - started_at, {"action": action})

    async def resolve(self, attention_id: str, request: ResolutionRequest) -> ResolutionReceipt:
        """Commit one complete answer or exact approval and retain any explicit pause."""
        value = await asyncio.to_thread(self.store.resolve_attention, attention_id, request)
        self.wakeup.set()
        return value

    async def retry(
        self, execution_id: str, *, request_id: str, expected_revision: int, supplement: str = ""
    ) -> Execution:
        """Validate the current Space and create a revision-bound fresh root once."""
        digest = fingerprint(
            {"execution_id": execution_id, "expected_revision": expected_revision, "supplement": supplement}
        )

        def committed_retry() -> Execution | None:
            """Read the original command result before validating a new admission."""
            with self.store.transaction() as connection:
                return self.store.receipt(connection, f"retry/{request_id}", digest, Execution)

        committed = await asyncio.to_thread(committed_retry)
        if committed is not None:
            self.wakeup.set()
            return committed
        current = await self.require_execution(execution_id)
        space = await asyncio.to_thread(self.store.get_space, current.space_id)
        if space is None:
            raise LookupError("The retry Space does not exist")
        await self.agents.validate_space(space)
        value = await asyncio.to_thread(
            self.store.retry_execution,
            execution_id,
            request_id=request_id,
            expected_revision=expected_revision,
            expected_space_revision=space.revision,
            supplement=supplement,
        )
        self.wakeup.set()
        return value

    async def retry_delivery(self, delivery_id: str, *, request_id: str, expected_generation: int) -> Delivery:
        """Retry the original delivery without running the Worker again."""
        value = await asyncio.to_thread(
            self.store.retry_delivery, delivery_id, request_id=request_id, expected_generation=expected_generation
        )
        self.wakeup.set()
        return value

    async def delegate(self, spec: ExecutionSpec, request_id: str) -> Execution:
        """Accept work once, with child grants owned by a running SDK delegation call."""

        def committed_delegation() -> Execution | None:
            """Read the original accepted result before validating a new grant."""
            with self.store.transaction() as connection:
                return self.store.receipt(connection, f"execution/create/{request_id}", fingerprint(spec), Execution)

        committed = await asyncio.to_thread(committed_delegation)
        if committed is not None:
            self.wakeup.set()
            return committed
        if spec.parent_execution_id is not None:
            parent = await self.require_execution(spec.parent_execution_id)
            if parent.current_run_id is None:
                raise ProductConflict("Delegation requires an active owning SDK Run")
            sdk_run = await self.sessions.run(parent.session_id, parent.current_run_id)
            if any(attempt.completed_at is None for attempt in sdk_run.model_attempts):
                raise ProductConflict("Delegation requires the parent's admitted model request to finish")
            calls = await self.sessions.tool_calls(parent.session_id, sdk_run.run_id)
            if not any(
                call.tool_code == "weave.delegate_execution"
                and call.state == "running"
                and request_id == f"delegate:{sdk_run.run_id}:{call.call_id}"
                for call in calls
            ):
                raise ProductConflict("A child grant requires its active SDK delegation call")
            runs = await asyncio.to_thread(self.store.execution_runs, parent.execution_id)
            run = next(value for value in runs if value.run_id == sdk_run.run_id)
            await asyncio.to_thread(
                self.store.settle_run_usage,
                parent.execution_id,
                invocation_id=run.invocation_id,
                consumed=sdk_run.usage.budget_output_tokens,
            )
        space = await asyncio.to_thread(self.store.get_space, spec.space_id)
        if space is None:
            raise LookupError(spec.space_id)
        await self.agents.validate_space(space)
        value = await asyncio.to_thread(self.store.create_execution, spec, request_id=request_id)
        await asyncio.to_thread(self.store.seed_execution_input, value.execution_id)
        self.wakeup.set()
        return value

    async def progress(self, execution_id: str, run_id: str, summary: str) -> None:
        """Record details while leaving Router wake policy under the product owner."""
        inputs = await asyncio.to_thread(self.store.worker_inputs, execution_id, pending_only=False)
        report = Report(
            report_id=stable_id(f"progress/{execution_id}/{run_id}/{summary}"),
            execution_id=execution_id,
            run_id=run_id,
            input_watermark=max((item.target_sequence for item in inputs), default=0),
            kind="progress",
            summary=summary,
        )
        await asyncio.to_thread(self.store.accept_report, report)
        self.metrics.silent_reports.add(1)

    async def run(self) -> None:
        """Schedule bounded product work while user controls remain independently admissible."""
        async with asyncio.TaskGroup() as group:
            self.group = group
            self.scheduler_ready.set()
            try:
                while not self.closing:
                    self.wakeup.clear()
                    await self.deliver_inputs()
                    await asyncio.to_thread(self.store.expire_attentions)
                    await asyncio.to_thread(self.content.expire_attachments)
                    executions = await self.all_records("executions", Execution)
                    for execution in executions:
                        handle = self.handles.get(execution.execution_id)
                        action = execution.control_intent
                        key = (execution.execution_id, action)
                        if (
                            action in {"pause", "cancel"}
                            and key not in self.control_tasks
                            and (handle is not None or execution.status is ExecutionStatus.CANCELLING)
                        ):
                            self.control_tasks.add(key)
                            group.create_task(self.apply_control(execution, action))
                    if self.router_task is None or self.router_task.done():
                        attempt = await asyncio.to_thread(self.store.claim_router_batch)
                        if attempt is not None:
                            self.router_task = group.create_task(self.route(attempt))
                    running = sum(not task.done() for task in self.worker_tasks.values())
                    for execution in executions:
                        if running >= self.max_workers:
                            break
                        task = self.worker_tasks.get(execution.execution_id)
                        if execution.status is ExecutionStatus.QUEUED and (task is None or task.done()):
                            self.worker_tasks[execution.execution_id] = group.create_task(
                                self.work(execution.execution_id)
                            )
                            running += 1
                    try:
                        async with asyncio.timeout(0.05):
                            await self.wakeup.wait()
                    except TimeoutError:
                        continue
            finally:
                self.admission_open = False
                self.group = None

    async def deliver_inputs(self) -> None:
        """Admit bounded immutable SDK inputs while retaining durable product delivery intent."""
        async with self.delivery_lock:
            pending_bytes: dict[str, int] = {}
            accepted_ids: dict[str, set[str]] = {}
            deferred_targets: set[tuple[str, str]] = set()
            while True:
                deliveries = await asyncio.to_thread(
                    self.store.pending_deliveries, excluded_targets=frozenset(deferred_targets)
                )
                if not deliveries:
                    return
                deferred_before = len(deferred_targets)
                for delivery in deliveries:
                    item = await asyncio.to_thread(self.store.get_input, delivery.input_id)
                    if item is None:
                        raise RuntimeError("A delivery has no immutable product input")
                    if delivery.target_kind == "router":
                        session = Session("router", self.sessions)
                    else:
                        execution = await asyncio.to_thread(self.store.get_execution, delivery.target_id)
                        if execution is None:
                            raise RuntimeError("An accepted input has no owning Execution")
                        if execution.status in TERMINAL_STATUSES:
                            deferred_targets.add((delivery.target_kind, delivery.target_id))
                            continue
                        session = Session(execution.session_id, self.sessions)
                    if session.session_id not in pending_bytes:
                        journal = await self.sessions.snapshot(session.session_id)
                        pending_bytes[session.session_id] = sum(
                            len(record.envelope.message().model_dump_json().encode("utf-8"))
                            for record in journal.inputs
                            if record.step_id is None
                        )
                        accepted_ids[session.session_id] = {record.envelope.input_id for record in journal.inputs}
                    envelope = input_envelope(item)
                    size = len(envelope.message().model_dump_json().encode("utf-8"))
                    duplicate = item.input_id in accepted_ids[session.session_id]
                    if not duplicate and pending_bytes[session.session_id] + size > MODEL_INPUT_QUEUE_BYTES:
                        deferred_targets.add((delivery.target_kind, delivery.target_id))
                        continue
                    try:
                        receipt = await session.enqueue(envelope)
                    except (OSError, SystemFault, ValueError) as error:
                        self.metrics.delivery_failures.add(1)
                        await asyncio.to_thread(
                            self.store.fail_delivery,
                            delivery.delivery_id,
                            expected_generation=delivery.generation,
                            error=str(error),
                        )
                    else:
                        if not duplicate:
                            pending_bytes[session.session_id] += size
                            accepted_ids[session.session_id].add(item.input_id)
                        await asyncio.to_thread(
                            self.store.confirm_delivery,
                            delivery.delivery_id,
                            expected_generation=delivery.generation,
                            sdk_sequence=receipt.sequence,
                        )

                if len(deferred_targets) == deferred_before:
                    return

    async def route(self, attempt: RouteAttempt) -> None:
        """Run a fixed Router batch and atomically apply its candidate once."""
        binding = None
        started = monotonic()
        self.metrics.router_batches.add(1)
        self.metrics.router_batch_size.record(len(attempt.source_input_ids))
        try:
            if attempt.candidate is not None:
                await asyncio.to_thread(self.store.apply_route_decision, attempt.decision_id, attempt=attempt.attempt)
                return

            async def observe(event: Event) -> None:
                return None

            binding = await self.agents.router(attempt, observe)
            session = Session("router", self.sessions)
            for identity in attempt.sdk_input_ids:
                item = await asyncio.to_thread(self.store.get_input, identity)
                if item is None:
                    raise LookupError(identity)
                await session.enqueue(input_envelope(item))
            accepted = await session.run_for_invocation(attempt.invocation_id)
            if accepted is not None and accepted.receipt is not None:
                result = RunResult(accepted.receipt)
            else:
                handle = await binding.runner.start(
                    binding.agent,
                    session=session,
                    invocation_id=attempt.invocation_id,
                    input_ids=attempt.sdk_input_ids,
                    input_mode="batch",
                    limits=RunLimits(execution_timeout_seconds=120, max_model_steps=8, max_output_tokens=4096),
                    context=binding.context,
                )
                self.router_handle = handle
                await asyncio.to_thread(self.store.bind_route_run, attempt.decision_id, attempt.attempt, handle.run_id)
                binding.ready.set()
                outcome = await handle.result()
                if isinstance(outcome, RunSuspension):
                    raise RuntimeError("The read-only Router unexpectedly requested a suspension")
                result = outcome
            if result.state is not RunStatus.SUCCEEDED or result.structured_output is None:
                raise RuntimeError(f"Router failed: {result.reason}")
            draft = RouteDraft.model_validate(result.structured_output)
            decision = RouteDecision(
                decision_id=attempt.decision_id,
                source_input_ids=attempt.source_input_ids,
                reply=draft.reply,
                actions=draft.actions,
                read_versions=binding.versions,
            )
            for action in decision.actions:
                if action.kind == "create_space":
                    await self.agents.validate_configuration(action.configuration)
            await asyncio.to_thread(
                self.store.save_route_candidate, decision, attempt=attempt.attempt, sdk_run_id=result.run_id
            )
            await asyncio.to_thread(self.store.apply_route_decision, attempt.decision_id, attempt=attempt.attempt)
        except ProductConflict as error:
            logger.info(f"Router decision needs reassessment decision={attempt.decision_id} attempt={attempt.attempt}")
            await asyncio.to_thread(self.store.reassess_route, attempt.decision_id, reason=str(error))
        except (ValueError, RuntimeError, SystemFault, LookupError) as error:
            logger.error(
                f"Router attempt failed decision={attempt.decision_id} attempt={attempt.attempt}", exc_info=True
            )
            await asyncio.to_thread(self.store.reassess_route, attempt.decision_id, reason=str(error))
        finally:
            self.metrics.router_duration.record(monotonic() - started)
            if binding is not None:
                binding.ready.set()
            self.router_handle = None
            self.wakeup.set()

    async def work(self, execution_id: str) -> None:
        """Own one Worker's resources and project committed SDK facts into product state."""
        binding = None
        self.metrics.workers.add(1)
        try:
            execution = await asyncio.to_thread(self.store.get_execution, execution_id)
            if execution is None or execution.status is not ExecutionStatus.QUEUED:
                return
            try:
                await self.agents.validate_space(execution.config_snapshot)
            except (ValueError, LookupError, PleiadError):
                await self.mark_blocked(execution, "resource_unavailable", execution.unknown_effects)
                return
            execution = await asyncio.to_thread(
                self.store.transition_execution,
                execution_id,
                ExecutionStatus.RUNNING,
                expected_revision=execution.revision,
            )
            await asyncio.to_thread(self.store.seed_execution_input, execution_id)
            await self.deliver_inputs()
            session = Session(execution.session_id, self.sessions)
            runs = await asyncio.to_thread(self.store.execution_runs, execution_id)
            previous = max(runs, key=lambda value: value.attempt) if runs else None
            checkpoint = None
            if previous is not None and previous.state in {"pending", "running", "paused"}:
                accepted = await session.run_for_invocation(previous.invocation_id)
                if accepted is not None:
                    if accepted.receipt is not None or accepted.checkpoint is None:
                        await self.reconcile_execution(execution)
                        return
                    checkpoint = accepted.checkpoint
                    previous = previous.model_copy(
                        update={"run_id": accepted.run_id, "state": "paused", "checkpoint_ref": accepted.run_id}
                    )
                    await asyncio.to_thread(self.store.record_execution_run, previous)

            async def observe(event: Event) -> None:
                if isinstance(event, MessageCompletedEvent) and isinstance(event.message, ToolMessage):
                    await asyncio.to_thread(
                        self.details.save_tool_result,
                        ToolResultDetail(
                            tool_result_id=f"{event.run_id}/{event.message.message_id}",
                            execution_id=execution_id,
                            run_id=event.run_id,
                            step_id=event.step_id,
                            call_id=event.message.tool_call_id,
                            text=event.message.content,
                        ),
                    )
                elif isinstance(event, ModelStepCompletedEvent):
                    active = self.handles.get(execution_id)
                    if active is not None and event.run_id == active.run_id:
                        await self.deliver_inputs()

            binding = await self.agents.worker(execution, observe, self.delegate, self.progress)
            self.bindings[execution_id] = binding
            available = execution.budget_limit - execution.budget_consumed - execution.budget_reserved
            if available <= 0:
                raise RuntimeError("The task output budget is exhausted")
            if checkpoint is None:
                pending = await session.pending_inputs()
                if not pending:
                    raise RuntimeError("Queued work has no accepted unclaimed SDK input")
                run = (
                    previous
                    if previous is not None and previous.state == "pending"
                    else ExecutionRun(
                        execution_id=execution_id,
                        attempt=execution.run_attempt + 1,
                        invocation_id=f"{execution_id}/{execution.run_attempt + 1}",
                    )
                )
                await asyncio.to_thread(self.store.record_execution_run, run)
                with bind_log_context(execution_id=execution_id, session_id=execution.session_id):
                    handle = await binding.runner.start(
                        binding.agent,
                        session=session,
                        invocation_id=run.invocation_id,
                        input_ids=tuple(value.envelope.input_id for value in pending),
                        input_mode="live",
                        limits=RunLimits(
                            execution_timeout_seconds=1800, max_model_steps=64, max_output_tokens=available
                        ),
                        context=binding.context,
                    )
            else:
                if previous is None:
                    raise RuntimeError("The paused invocation mapping is missing")
                run = previous
                decisions = await self.resume_decisions(execution, checkpoint)
                with bind_log_context(execution_id=execution_id, session_id=execution.session_id):
                    handle = await binding.runner.resume(
                        binding.agent,
                        session=session,
                        checkpoint=checkpoint,
                        decisions=decisions,
                        limits=RunLimits(
                            execution_timeout_seconds=1800,
                            max_model_steps=64,
                            max_output_tokens=available + checkpoint.usage.budget_output_tokens,
                        ),
                    )
            self.handles[execution_id] = handle
            await asyncio.to_thread(
                self.store.record_execution_run, run.model_copy(update={"run_id": handle.run_id, "state": "running"})
            )
            binding.ready.set()
            outcome = await handle.result()
            await self.finish_work(execution_id, run, outcome)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error(f"Worker stopped execution={execution_id}", exc_info=True)
            await self.project_committed_outcome(execution_id)
            current = await asyncio.to_thread(self.store.get_execution, execution_id)
            if current is not None and current.status not in TERMINAL_STATUSES:
                await self.mark_blocked(current, "unhandled_failure")
        finally:
            self.metrics.workers.add(-1)
            if binding is not None:
                binding.ready.set()
            self.handles.pop(execution_id, None)
            self.bindings.pop(execution_id, None)
            self.wakeup.set()

    async def settle_returned_inputs(
        self, execution: Execution, run_id: str, *, reconciliation: ReconcileRecord | None = None
    ) -> bool:
        """Release historical unknown handbacks using exact persisted SDK reconciliation facts."""
        settled = {
            value.handback_id for value in await self.all_records("input_handback_resolutions", HandbackResolution)
        }
        pending = tuple(
            value
            for value in await self.all_records("input_handbacks", Handback)
            if value.target_execution_id == execution.execution_id
            and value.run_id == run_id
            and value.unknown_effects
            and value.handback_id not in settled
        )
        if not pending:
            return True
        session = Session(execution.session_id, self.sessions)
        if reconciliation is None:
            snapshot = await self.sessions.run(session.session_id, run_id)
            request_id = f"handback:{run_id}:{snapshot.revision}"
            result = await Runner(tools=ToolRegistry(), skills=SkillLoader(), tool_runtime=ToolRuntime()).reconcile(
                session=session,
                run_id=run_id,
                request_id=request_id,
                expected_epoch=snapshot.writer_epoch,
                expected_revision=snapshot.revision,
            )
            if result.unsettled_actions:
                await self.mark_blocked(
                    execution, "recovery_unknown", tuple(value.call_id for value in result.unsettled_actions)
                )
                return False
            journal = await self.sessions.snapshot(session.session_id)
            reconciliation = next(value for value in journal.reconciliations if value.request_id == request_id)
        else:
            journal = await self.sessions.snapshot(session.session_id)
        resolutions = await asyncio.to_thread(
            self.store.settle_handbacks,
            execution.execution_id,
            reconciliation=reconciliation,
            actions=tuple(value for value in journal.tool_calls if value.run_id == run_id),
        )
        confirmed = {value.handback_id for value in resolutions}
        unresolved = tuple(
            ref.call_id for value in pending if value.handback_id not in confirmed for ref in value.action_refs
        )
        if unresolved:
            await self.mark_blocked(execution, "recovery_unknown", unresolved)
            return False
        return True

    async def project_run_outcome(
        self, execution_id: str, run: ExecutionRun, outcome: RunResult | RunSuspension
    ) -> bool:
        """Project committed SDK usage and invocation state before applying product policy.

        Returns:
            Whether terminal charging must wait for independently owned child refunds.
        """
        execution = await self.require_execution(execution_id)
        waiting_for_children = (
            isinstance(outcome, RunResult)
            and (outcome.state in {RunStatus.FAILED, RunStatus.CANCELLED} or execution.control_intent == "cancel")
            and any(
                child.parent_execution_id == execution_id and child.status not in TERMINAL_STATUSES
                for child in await self.all_records("executions", Execution)
            )
        )
        if not waiting_for_children:
            await asyncio.to_thread(
                self.store.settle_run_usage,
                execution_id,
                invocation_id=run.invocation_id,
                consumed=outcome.usage.budget_output_tokens,
            )
        records = await asyncio.to_thread(self.store.execution_runs, execution_id)
        current_run = next(value for value in records if value.invocation_id == run.invocation_id)
        state = "paused" if isinstance(outcome, RunSuspension) else outcome.state.value
        await asyncio.to_thread(
            self.store.record_execution_run,
            current_run.model_copy(
                update={
                    "run_id": outcome.run_id,
                    "state": state,
                    "checkpoint_ref": outcome.run_id if isinstance(outcome, RunSuspension) else None,
                    "usage": outcome.usage.model_dump(mode="json"),
                }
            ),
        )
        return waiting_for_children

    async def project_committed_outcome(self, execution_id: str) -> None:
        """Consume an exact persisted receipt after the live SDK owner raised an exception."""
        async with self.execution_locks.setdefault(execution_id, asyncio.Lock()):
            execution = await self.require_execution(execution_id)
            runs = await asyncio.to_thread(self.store.execution_runs, execution_id)
            if not runs:
                return
            run = max(runs, key=lambda value: value.attempt)
            snapshot = await Session(execution.session_id, self.sessions).run_for_invocation(run.invocation_id)
            if snapshot is None or snapshot.receipt is None:
                return
            if (run.run_id is not None and run.run_id != snapshot.run_id) or (
                execution.current_run_id is not None and execution.current_run_id != snapshot.run_id
            ):
                raise ProductConflict("The failed SDK owner does not match the current Execution invocation")
            handle = self.handles.get(execution_id)
            if handle is not None and handle.run_id != snapshot.run_id:
                raise ProductConflict("The live SDK handle does not own the committed receipt")
            await self.project_run_outcome(execution_id, run, RunResult(snapshot.receipt))

    async def finish_work(self, execution_id: str, run: ExecutionRun, outcome: RunResult | RunSuspension) -> None:
        """Validate actual delivery facts, cumulative budgets, and pending control intent."""
        waiting_for_children = await self.project_run_outcome(execution_id, run, outcome)
        current = await asyncio.to_thread(self.store.get_execution, execution_id)
        if current is None:
            raise LookupError(execution_id)
        if waiting_for_children:
            if outcome.state is RunStatus.FAILED and current.control_intent != "cancel":
                await self.mark_blocked(current, "unhandled_failure", current.unknown_effects)
            return
        if isinstance(outcome, RunSuspension):
            if current.control_intent == "cancel":
                await self.cancel_stopped(current, outcome.checkpoint)
            elif current.control_intent == "pause" or self.closing:
                await asyncio.to_thread(
                    self.store.transition_execution,
                    execution_id,
                    ExecutionStatus.PAUSED,
                    expected_revision=current.revision,
                    checkpoint_ref=outcome.run_id,
                )
            else:
                await self.create_pending_attentions(current, outcome.checkpoint)
            return
        calls = await self.sessions.tool_calls(current.session_id, outcome.run_id)
        descendants = await self.reconcile_descendants(outcome.run_id, cancel=current.control_intent == "cancel")
        unsettled = tuple(
            dict.fromkeys(
                [value.call_id for value in calls if value.state in {"prepared", "running", "unknown"}]
                + [value.call_id for value in descendants]
            )
        )
        if unsettled:
            await self.mark_blocked(current, "recovery_unknown", unsettled)
            return
        if not await self.settle_returned_inputs(current, outcome.run_id):
            return
        if outcome.state is RunStatus.CANCELLED or current.control_intent == "cancel":
            await self.finish_cancellation(execution_id)
            return
        if outcome.state is RunStatus.FAILED and outcome.reason == "system_failure":
            await self.mark_blocked(current, "unhandled_failure", current.unknown_effects)
            return
        if outcome.state is not RunStatus.SUCCEEDED or outcome.structured_output is None:
            report = Report(
                report_id=stable_id(f"failure/{outcome.run_id}"),
                execution_id=execution_id,
                run_id=outcome.run_id,
                input_watermark=0,
                kind="failed",
                summary=outcome.reason or "Worker failed",
            )
            await asyncio.to_thread(self.store.accept_report, report)
            return
        draft = CompletionDraft.model_validate(outcome.structured_output)
        journal = await self.sessions.snapshot(current.session_id)
        consumed = {value.envelope.input_id for value in journal.inputs if value.step_id is not None}
        validation_errors = []
        if not set(draft.handled_input_ids) <= consumed:
            validation_errors.append("The final report claims inputs that never entered this Execution's SDK Session")
        inputs = await asyncio.to_thread(self.store.worker_inputs, execution_id, pending_only=False)
        pending = {
            value.input_id
            for value in await self.all_records("input_dispositions", InputDisposition)
            if value.target_execution_id == execution_id and value.state == "owned"
        }
        handled = tuple(
            identity for identity in draft.handled_input_ids if identity in pending and identity in consumed
        )
        successful_calls = {(value.run_id, value.call_id) for value in journal.tool_calls if value.state == "succeeded"}
        evidence_times = {
            value.tool_result_id: value.created_at
            for value in await self.all_records("tool_results", ToolResultDetail)
            if value.execution_id == execution_id and (value.run_id, value.call_id) in successful_calls
        }
        evidence_times.update(
            (value.node_attempt_id, value.finished_at)
            for value in await self.all_records("node_attempts", NodeAttempt)
            if value.execution_id == execution_id
            and value.status is NodeStatus.SUCCEEDED
            and value.finished_at is not None
        )
        owned_artifacts = {
            value.artifact_id: value
            for value in await self.all_records("artifacts", Artifact)
            if value.owner_execution_id == execution_id and value.deletion_state == "active"
        }
        evidence_times.update((identity, value.created_at) for identity, value in owned_artifacts.items())
        if not set(draft.artifact_ids) <= owned_artifacts.keys():
            validation_errors.append("Delivery artifacts must exist, remain available, and belong to this Execution")
        verification_results = []
        required_names = {value.name for value in current.delivery_contract.required_verifications}
        for verification in draft.verification:
            if verification.name in required_names:
                continue
            invalid_evidence = verification.status == "passed" and (
                not verification.evidence_refs or not set(verification.evidence_refs) <= evidence_times.keys()
            )
            if invalid_evidence:
                validation_errors.append(
                    f"Verification {verification.name} requires exact owned evidence IDs returned by host tools"
                )
            times = [evidence_times[identity] for identity in verification.evidence_refs if identity in evidence_times]
            verification_results.append(
                Verification(
                    name=verification.name,
                    status="unknown" if invalid_evidence else verification.status,
                    evidence_refs=verification.evidence_refs,
                    executed_at=max(times) if times else None,
                )
            )
        verification_results.extend(await verify_delivery(current, draft.artifact_ids, journal, self.content))
        report = Report(
            report_id=stable_id(f"{draft.kind}/{outcome.run_id}"),
            execution_id=execution_id,
            run_id=outcome.run_id,
            input_watermark=max((value.target_sequence for value in inputs if value.input_id in consumed), default=0),
            kind=draft.kind,
            summary=draft.summary,
            artifact_ids=tuple(identity for identity in draft.artifact_ids if identity in owned_artifacts),
            verification=tuple(verification_results),
            rejection_reasons=tuple(validation_errors),
        )
        accepted = await asyncio.to_thread(self.store.accept_report, report, handled_input_ids=handled)
        if accepted.rejection_reasons:
            corrected = await asyncio.to_thread(
                self.store.queue_after_rejected_report, accepted.report_id, evidence_refs=tuple(sorted(evidence_times))
            )
            if corrected.control_intent == "cancel":
                await self.finish_cancellation(execution_id)
            self.wakeup.set()

    def pending_requests(self, interruption: ToolInterruption) -> tuple[ToolInterruption, ...]:
        """Read leaf user requests without replacing their nested SDK identities."""
        kind = interruption.details.get("kind")
        if kind == "weave.wait_children":
            return ()
        if kind == "weave.graph":
            nodes = PENDING_GRAPH_NODES.validate_python(interruption.details.get("pending"))
            return tuple(
                request for node in nodes for item in node.interruptions for request in self.pending_requests(item)
            )
        if kind == "pleiad.subagent":
            checkpoint = RunCheckpoint.model_validate(interruption.details.get("checkpoint"))
            return tuple(request for item in checkpoint.interruptions for request in self.pending_requests(item))
        return (interruption,)

    async def create_pending_attentions(self, execution: Execution, checkpoint: RunCheckpoint) -> None:
        """Persist exact pending requests after the SDK has saved a safe checkpoint."""
        attentions: list[Attention] = []
        for interruption in checkpoint.interruptions:
            for item in self.pending_requests(interruption):
                kind: Literal["approval", "clarification"] = (
                    "clarification" if item.details.get("kind") == "weave.clarification" else "approval"
                )
                options = item.details.get("options", [])
                if not isinstance(options, list) or not all(isinstance(value, str) for value in options):
                    raise ValueError("Question options must be text")
                attention = Attention(
                    attention_id=item.interruption_id,
                    execution_id=execution.execution_id,
                    kind=kind,
                    question=item.reason,
                    options=tuple(options),
                    action_digest=fingerprint(item.arguments) if kind == "approval" else None,
                    action=(
                        ApprovalAction(tool_code=item.tool_code, tool_name=item.tool_name, arguments=item.arguments)
                        if kind == "approval"
                        else None
                    ),
                    checkpoint_ref=checkpoint.run_id,
                    expires_at=datetime.now(UTC) + timedelta(hours=1),
                )
                attentions.append(attention)
        await asyncio.to_thread(
            self.store.project_pending_attentions,
            execution.execution_id,
            checkpoint.run_id,
            attentions=tuple(attentions),
            has_interruptions=bool(checkpoint.interruptions),
        )

    async def resume_decisions(self, execution: Execution, checkpoint: RunCheckpoint) -> tuple[ResumeDecision, ...]:
        """Convert saved user decisions into server-authored exact tool outcomes."""
        decisions: list[ResumeDecision] = []
        for item in checkpoint.interruptions:
            choice: ResumeDecision | None
            if item.details.get("kind") == "weave.wait_children":
                identity = item.details.get("child_execution_id")
                if not isinstance(identity, str):
                    raise ValueError("The pending child Execution identity is missing")
                child = await self.require_execution(identity)
                if child.parent_execution_id != execution.execution_id:
                    raise ProductConflict("The pending child belongs to another Execution")
                if child.status not in TERMINAL_STATUSES:
                    continue
                now = datetime.now(UTC)
                choice = ResumeDecision(
                    interruption_id=item.interruption_id,
                    result=ToolResult(
                        call_id=item.call_id,
                        ok=True,
                        output={"children": [child.model_dump(mode="json")]},
                        started_at=now,
                        finished_at=now,
                    ),
                )
            else:
                choice = await self.decision_for(item)
            if choice is not None:
                decisions.append(choice)
        return tuple(decisions)

    async def decision_for(self, item: ToolInterruption) -> ResumeDecision | None:
        """Build one trusted resolution while retaining every nested checkpoint identity."""
        kind = item.details.get("kind")
        if kind == "pleiad.subagent":
            checkpoint = RunCheckpoint.model_validate(item.details.get("checkpoint"))
            choices: list[ResumeDecision] = []
            for pending in checkpoint.interruptions:
                choice = await self.decision_for(pending)
                if choice is not None:
                    choices.append(choice)
            if checkpoint.interruptions and not choices:
                return None
            now = datetime.now(UTC)
            return ResumeDecision(
                interruption_id=item.interruption_id,
                result=ToolResult(
                    call_id=item.call_id,
                    ok=True,
                    output={"subagent_decisions": [choice.model_dump(mode="json") for choice in choices]},
                    started_at=now,
                    finished_at=now,
                ),
            )
        if kind == "weave.graph":
            nodes = PENDING_GRAPH_NODES.validate_python(item.details.get("pending"))
            resumes = []
            for node in nodes:
                chosen: list[ResumeDecision] = []
                for pending in node.interruptions:
                    choice = await self.decision_for(pending)
                    if choice is not None:
                        chosen.append(choice)
                if chosen or not node.interruptions:
                    resumes.append(NodeResume(node_id=node.node_id, decisions=tuple(chosen)))
            if not resumes:
                return None
            now = datetime.now(UTC)
            return ResumeDecision(
                interruption_id=item.interruption_id,
                result=ToolResult(
                    call_id=item.call_id,
                    ok=True,
                    output={"node_resumes": [resume.model_dump(mode="json") for resume in resumes]},
                    started_at=now,
                    finished_at=now,
                ),
            )
        attention = await asyncio.to_thread(self.store.get_attention, item.interruption_id)
        if attention is None or attention.status not in {"resolved", "expired"}:
            return None
        now = datetime.now(UTC)
        if attention.status == "expired":
            return ResumeDecision(
                interruption_id=item.interruption_id,
                result=ToolResult(
                    call_id=item.call_id,
                    ok=False,
                    error_code="weave.decision_expired",
                    error_message="The pending user decision expired without authorizing this action.",
                    error_type=ErrorType.BUSINESS,
                    started_at=now,
                    finished_at=now,
                ),
            )
        if attention.kind == "approval":
            if attention.action_digest != fingerprint(item.arguments):
                raise ProductConflict("The approval is not bound to this exact action")
            approved = attention.resolution == "approve"
            result = ToolResult(
                call_id=item.call_id,
                ok=approved,
                output={"approved": True} if approved else None,
                error_code=None if approved else "weave.approval_rejected",
                error_message=None if approved else "The owner rejected this action.",
                error_type=None if approved else ErrorType.BUSINESS,
                started_at=now,
                finished_at=now,
            )
        else:
            result = ToolResult(
                call_id=item.call_id, ok=True, output={"answer": attention.resolution}, started_at=now, finished_at=now
            )
        return ResumeDecision(interruption_id=item.interruption_id, result=result)

    async def apply_control(self, execution: Execution, action: str) -> None:
        """Translate durable intent to its currently owned SDK scope."""
        try:
            lost_owner: Execution | None = None
            async with self.execution_locks.setdefault(execution.execution_id, asyncio.Lock()):
                execution = await self.require_execution(execution.execution_id)
                if execution.status in TERMINAL_STATUSES or execution.control_intent != action:
                    return
                handle = self.handles.get(execution.execution_id)
                if handle is not None:
                    if action == "cancel":
                        await handle.cancel(reason="user_cancelled")
                    else:
                        await handle.pause(reason="user_paused")
                elif action == "cancel":
                    session = Session(execution.session_id, self.sessions)
                    checkpoint = (
                        None if execution.current_run_id is None else await session.checkpoint(execution.current_run_id)
                    )
                    if execution.current_run_id is not None and (execution.unknown_effects or checkpoint is None):
                        lost_owner = execution
                    elif checkpoint is not None:
                        await self.cancel_stopped(execution, checkpoint)
                    elif not execution.unknown_effects:
                        await self.finish_cancellation(execution.execution_id)
            if lost_owner is not None:
                await self.reconcile_execution(lost_owner)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error(f"Control remains blocked execution={execution.execution_id} action={action}", exc_info=True)
            await self.project_committed_outcome(execution.execution_id)
            current = await self.require_execution(execution.execution_id)
            if current.status not in TERMINAL_STATUSES:
                await self.mark_blocked(current, "unhandled_failure", current.unknown_effects)
        finally:
            self.control_tasks.discard((execution.execution_id, action))
            self.wakeup.set()

    async def cancel_checkpoint(self, execution: Execution, checkpoint: RunCheckpoint, runner: Runner) -> RunResult:
        """Settle nested paused SDK scopes using persisted identities and no current model resources."""
        for item in checkpoint.interruptions:
            kind = item.details.get("kind")
            if kind == "pleiad.subagent":
                child = RunCheckpoint.model_validate(item.details.get("checkpoint"))
                if child.parent_run_id != checkpoint.run_id or item.details.get("child_run_id") != child.run_id:
                    raise ProductConflict("The paused Subagent does not belong to this parent Run")
                await self.cancel_checkpoint(execution, child, runner)
            elif kind == "weave.graph":
                nodes = PENDING_GRAPH_NODES.validate_python(item.details.get("pending"))
                identity = item.details.get("graph_run_id")
                if not isinstance(identity, str):
                    raise ValueError("The pending GraphRun identity is missing")
                record = await asyncio.to_thread(self.details.graph_run, execution.execution_id, identity)
                if record.parent_run_id != checkpoint.run_id or record.call_id != item.call_id:
                    raise ProductConflict("The paused GraphRun does not belong to this parent invocation")
                for node in nodes:
                    if node.checkpoint.parent_run_id != checkpoint.run_id:
                        raise ProductConflict("The paused Graph node does not belong to this parent Run")
                    await self.cancel_checkpoint(execution, node.checkpoint, runner)
                graph = GraphControls(
                    execution.execution_id,
                    self.details,
                    self.content,
                    self.agents.models,
                    runner,
                    self.sessions,
                )
                await graph.cancel_pending(identity, "user_cancelled")
        return await runner.cancel_paused(
            session=Session(checkpoint.session_id, self.sessions), checkpoint=checkpoint, reason="user_cancelled"
        )

    async def cancel_stopped(self, execution: Execution, checkpoint: RunCheckpoint) -> None:
        """Cancel safe SDK scopes after independently owned child cleanup settles."""
        if any(
            child.parent_execution_id == execution.execution_id and child.status not in TERMINAL_STATUSES
            for child in await self.all_records("executions", Execution)
        ):
            return
        runner = Runner(tools=ToolRegistry(), skills=SkillLoader(), tool_runtime=ToolRuntime())
        outcome = await self.cancel_checkpoint(execution, checkpoint, runner)
        runs = await asyncio.to_thread(self.store.execution_runs, execution.execution_id)
        run = next(value for value in runs if value.run_id == checkpoint.run_id)
        await asyncio.to_thread(
            self.store.settle_run_usage,
            execution.execution_id,
            invocation_id=run.invocation_id,
            consumed=outcome.usage.budget_output_tokens,
        )
        current = next(
            value
            for value in await asyncio.to_thread(self.store.execution_runs, execution.execution_id)
            if value.run_id == checkpoint.run_id
        )
        await asyncio.to_thread(
            self.store.record_execution_run,
            current.model_copy(
                update={"state": "cancelled", "checkpoint_ref": None, "usage": outcome.usage.model_dump(mode="json")}
            ),
        )
        await self.finish_cancellation(execution.execution_id)

    async def ignore_event(self, event: Event) -> None:
        """Accept an internal observer notification while reading persisted facts."""

    async def finish_cancellation(self, execution_id: str) -> None:
        """Confirm cancellation only after all independently owned child work settles."""
        current = await self.require_execution(execution_id)
        if any(
            child.parent_execution_id == execution_id and child.status not in TERMINAL_STATUSES
            for child in await self.all_records("executions", Execution)
        ):
            return
        if current.status in TERMINAL_STATUSES:
            return
        await asyncio.to_thread(self.store.withdraw_attentions, execution_id)
        if current.status is not ExecutionStatus.CANCELLING:
            await self.transition_latest(execution_id, ExecutionStatus.CANCELLING)
        await self.transition_latest(execution_id, ExecutionStatus.CANCELLED)
        elapsed = await asyncio.to_thread(self.store.cancellation_elapsed, execution_id)
        if elapsed is not None:
            self.metrics.cancellation_duration.record(
                elapsed, {"scope": "root" if current.parent_execution_id is None else "child"}
            )

    async def mark_blocked(self, execution: Execution, reason: str, unknown: tuple[str, ...] = ()) -> None:
        self.metrics.unknown_actions.record(len(unknown))
        await self.transition_latest(
            execution.execution_id, ExecutionStatus.BLOCKED, reason=reason, unknown_effects=unknown
        )
        if unknown:
            identity = stable_id(f"recovery/{execution.execution_id}/{fingerprint({'actions': list(unknown)})}")
            if await asyncio.to_thread(self.store.get_attention, identity) is None:
                await asyncio.to_thread(
                    self.store.create_attention,
                    Attention(
                        attention_id=identity,
                        execution_id=execution.execution_id,
                        kind="recovery",
                        question="External outcomes are unknown. Trusted backend evidence is required to continue.",
                        checkpoint_ref=execution.current_run_id,
                    ),
                )

    async def transition_latest(
        self,
        execution_id: str,
        status: ExecutionStatus,
        *,
        reason: str | None = None,
        checkpoint_ref: str | None = None,
        unknown_effects: tuple[str, ...] | None = None,
    ) -> Execution:
        """Apply an owner transition against the latest product revision."""
        current = await self.require_execution(execution_id)
        return await asyncio.to_thread(
            self.store.transition_execution,
            execution_id,
            status,
            expected_revision=current.revision,
            reason=reason,
            checkpoint_ref=checkpoint_ref,
            unknown_effects=unknown_effects,
        )

    async def require_execution(self, execution_id: str) -> Execution:
        value = await asyncio.to_thread(self.store.get_execution, execution_id)
        if value is None:
            raise LookupError(execution_id)
        return value

    async def recover(self) -> None:
        """Recover accepted work after exclusive ownership has been established."""
        router = Session("router", self.sessions)
        for attempt in await self.all_records("route_attempts", RouteAttempt):
            if attempt.state not in {"pending", "candidate"}:
                continue
            snapshot = await router.run_for_invocation(attempt.invocation_id)
            if snapshot is None or snapshot.receipt is not None:
                continue
            await Runner(tools=ToolRegistry(), skills=SkillLoader(), tool_runtime=ToolRuntime()).reconcile(
                session=router,
                run_id=snapshot.run_id,
                request_id=f"router-recovery:{snapshot.run_id}:{snapshot.revision}",
                expected_epoch=snapshot.writer_epoch,
                expected_revision=snapshot.revision,
            )
        for execution in await self.all_records("executions", Execution):
            if execution.status in TERMINAL_STATUSES:
                continue
            runs = await asyncio.to_thread(self.store.execution_runs, execution.execution_id)
            if runs or execution.status in {
                ExecutionStatus.RUNNING,
                ExecutionStatus.CANCELLING,
                ExecutionStatus.BLOCKED,
            }:
                await self.reconcile_execution(execution)
        for cleanup in await self.all_records("cleanup_records", CleanupRecord):
            if cleanup.state != "completed":
                await self.finish_cleanup(cleanup.request_id, expected_generation=cleanup.generation)

    async def reconcile_execution(self, execution: Execution, *, evidence: tuple[ActionEvidence, ...] = ()) -> None:
        """Apply trusted host evidence and honor saved control intent without replaying actions."""
        async with self.execution_locks.setdefault(execution.execution_id, asyncio.Lock()):
            execution = await self.require_execution(execution.execution_id)
            handle = self.handles.get(execution.execution_id)
            if handle is not None and not handle.finished:
                raise ProductConflict("A live Worker must settle before lost-owner reconciliation")
            session = Session(execution.session_id, self.sessions)
            runs = await asyncio.to_thread(self.store.execution_runs, execution.execution_id)
            run = max(runs, key=lambda value: value.attempt) if runs else None
            invocation_id = f"{execution.execution_id}/1" if run is None else run.invocation_id
            snapshot = await session.run_for_invocation(invocation_id)
            if snapshot is None:
                if run is not None and (run.run_id is not None or run.state != "pending"):
                    await self.mark_blocked(execution, "recovery_unknown")
                    return
                await asyncio.to_thread(self.store.withdraw_attentions, execution.execution_id, kind="recovery")
                value = await asyncio.to_thread(self.store.recover_unstarted_run, execution.execution_id, invocation_id)
                if value.control_intent == "cancel":
                    await self.finish_cancellation(value.execution_id)
                return
            if run is None:
                await self.mark_blocked(execution, "recovery_unknown")
                return
            if run.run_id is None:
                run = run.model_copy(update={"run_id": snapshot.run_id, "state": "running"})
                await asyncio.to_thread(self.store.record_execution_run, run)
            elif run.run_id != snapshot.run_id:
                raise ProductConflict("The recorded SDK invocation resolves to another Run")
            descendants = await self.reconcile_descendants(snapshot.run_id, cancel=execution.control_intent == "cancel")
            journal = await self.sessions.snapshot(session.session_id)
            evidence = await observe_command_actions(
                self.agents.sandbox.backend,
                journal,
                snapshot.run_id,
                cancel=execution.control_intent == "cancel",
                evidence=evidence,
            )
            runner = Runner(tools=ToolRegistry(), skills=SkillLoader(), tool_runtime=ToolRuntime())
            result = await runner.reconcile(
                session=session,
                run_id=snapshot.run_id,
                request_id=f"recovery:{snapshot.run_id}:{snapshot.revision}",
                expected_epoch=snapshot.writer_epoch,
                expected_revision=snapshot.revision,
                evidence=evidence,
            )
            unsettled = (*descendants, *result.unsettled_actions)
            if unsettled:
                await self.mark_blocked(
                    execution, "recovery_unknown", tuple(dict.fromkeys(value.call_id for value in unsettled))
                )
                return
            journal = await self.sessions.snapshot(session.session_id)
            reconciliation = next(
                value
                for value in journal.reconciliations
                if value.request_id == f"recovery:{snapshot.run_id}:{snapshot.revision}"
            )
            if not await self.settle_returned_inputs(execution, snapshot.run_id, reconciliation=reconciliation):
                return
            await asyncio.to_thread(self.store.withdraw_attentions, execution.execution_id, kind="recovery")
            if execution.unknown_effects:
                execution = await self.transition_latest(
                    execution.execution_id, ExecutionStatus.BLOCKED, reason="recovery_unknown", unknown_effects=()
                )
            if result.snapshot.receipt is not None:
                await asyncio.to_thread(self.store.recover_finished_run, execution.execution_id, snapshot.run_id)
                await self.finish_work(execution.execution_id, run, RunResult(result.snapshot.receipt))
            elif result.snapshot.checkpoint is not None:
                await self.finish_work(execution.execution_id, run, RunSuspension(result.snapshot.checkpoint))
                current = await self.require_execution(execution.execution_id)
                if current.status not in TERMINAL_STATUSES and current.control_intent != "cancel":
                    try:
                        await self.agents.validate_space(current.config_snapshot)
                    except (ValueError, LookupError, PleiadError):
                        await self.mark_blocked(current, "resource_unavailable", current.unknown_effects)

    async def reconcile_descendants(self, run_id: str, *, cancel: bool) -> tuple[ActionRef, ...]:
        """Recover only unsettled descendant effects through their original SDK Sessions."""
        session_ids = await self.sessions.descendant_sessions((run_id,))
        runner = Runner(tools=ToolRegistry(), skills=SkillLoader(), tool_runtime=ToolRuntime())
        unsettled: list[ActionRef] = []
        journals = [await self.sessions.snapshot(session_id) for session_id in session_ids]
        depths = {run_id: 0}
        pending = [snapshot for journal in journals for snapshot in journal.runs]
        while pending:
            children = [snapshot for snapshot in pending if snapshot.parent_run_id in depths]
            if not children:
                raise ProductConflict("Descendant Run ownership cannot be resolved to the original root")
            for snapshot in children:
                parent = snapshot.parent_run_id
                if parent is None:
                    raise ProductConflict("A descendant Run requires its persisted parent identity")
                depths[snapshot.run_id] = depths[parent] + 1
            pending = [snapshot for snapshot in pending if snapshot.run_id not in depths]
        for journal in sorted(journals, key=lambda value: max(depths[run.run_id] for run in value.runs), reverse=True):
            affected = {call.run_id for call in journal.tool_calls if call.state in {"running", "unknown"}}
            affected.update(
                value.run_id
                for value in journal.operations
                if value.state in {"prepared", "running", "cancel_requested", "unknown"}
            )
            for snapshot in sorted(journal.runs, key=lambda value: depths[value.run_id], reverse=True):
                if snapshot.run_id not in affected:
                    continue
                evidence = await observe_command_actions(
                    self.agents.sandbox.backend, journal, snapshot.run_id, cancel=cancel
                )
                result = await runner.reconcile(
                    session=Session(journal.session_id, self.sessions),
                    run_id=snapshot.run_id,
                    request_id=f"descendant-recovery:{snapshot.run_id}:{snapshot.revision}",
                    expected_epoch=snapshot.writer_epoch,
                    expected_revision=snapshot.revision,
                    evidence=evidence,
                )
                unsettled.extend(result.unsettled_actions)
        return tuple(unsettled)

    async def all_records[T: ContractModel](self, table: str, model: type[T]) -> tuple[T, ...]:
        """Read one bounded owner-managed collection outside the event loop."""

        def read() -> tuple[T, ...]:
            with self.store.transaction() as connection:
                return self.store.records(connection, table, model)

        return await asyncio.to_thread(read)

    async def wait(self, execution_id: str) -> Execution:
        """Wait for a terminal job without taking cancellation ownership."""
        while True:
            value = await self.require_execution(execution_id)
            if value.status in TERMINAL_STATUSES or value.status in {
                ExecutionStatus.PAUSED,
                ExecutionStatus.BLOCKED,
                ExecutionStatus.WAITING_INPUT,
                ExecutionStatus.WAITING_APPROVAL,
            }:
                return value
            await asyncio.sleep(0.02)

    async def snapshot(self, *, limit: int = 50) -> JsonObject:
        """Read all first-screen entities and their action eligibility at one watermark."""
        snapshot = await asyncio.to_thread(self.store.snapshot, limit=limit)
        return JSON_OBJECT.validate_python(snapshot.model_dump(mode="json"))

    async def execution_detail(self, execution_id: str) -> JsonObject:
        """Read atomic product facts and separately watermarked SDK consumption evidence."""

        def read() -> ExecutionDetailView:
            with self.store.transaction() as connection:
                execution = self.store.require(connection, "executions", execution_id, Execution)
                space = self.store.require(connection, "spaces", execution.space_id, Space)
                if space.deleted:
                    raise LookupError("The Execution's Space was deleted")
                reports = tuple(
                    record
                    for record in self.store.records(connection, "reports", Report)
                    if record.execution_id == execution_id
                )
                report_ids = {record.report_id for record in reports}
                inputs = tuple(
                    record
                    for record in self.store.records(connection, "inbox_items", InboxItem)
                    if record.target_kind == "execution" and record.target_id == execution_id
                )
                delivery_inputs = {record.input_id for record in inputs}
                delivery_inputs.update(
                    record.input_id
                    for record in self.store.records(connection, "inbox_items", InboxItem)
                    if record.source == "weave.report" and record.causation_id in report_ids
                )
                handbacks = tuple(
                    value
                    for value in self.store.records(connection, "input_handbacks", Handback)
                    if value.target_execution_id == execution_id
                )
                handback_ids = {value.handback_id for value in handbacks}
                return ExecutionDetailView(
                    **self.store.execution_view(connection, execution).model_dump(),
                    read_sequence=self.store.sequence(connection),
                    session_read_sequence=0,
                    reports=reports,
                    artifacts=tuple(
                        ArtifactMetadata.model_validate(record.model_dump(exclude={"relative_path"}))
                        for record in self.store.records(connection, "artifacts", Artifact)
                        if record.owner_execution_id == execution_id
                    ),
                    children=tuple(
                        self.store.execution_view(connection, record)
                        for record in self.store.records(connection, "executions", Execution)
                        if record.parent_execution_id == execution_id
                    ),
                    attentions=tuple(
                        self.store.attention_view(connection, record)
                        for record in self.store.records(connection, "attentions", Attention)
                        if record.execution_id == execution_id
                    ),
                    runs=tuple(
                        record
                        for record in self.store.records(connection, "execution_runs", ExecutionRun)
                        if record.execution_id == execution_id
                    ),
                    inputs=tuple(ExecutionInputView(**record.model_dump()) for record in inputs),
                    deliveries=tuple(
                        self.store.delivery_view(connection, record)
                        for record in self.store.records(connection, "deliveries", Delivery)
                        if record.input_id in delivery_inputs
                    ),
                    dispositions=tuple(
                        record
                        for record in self.store.records(connection, "input_dispositions", InputDisposition)
                        if record.target_execution_id == execution_id
                    ),
                    handbacks=handbacks,
                    handback_resolutions=tuple(
                        record
                        for record in self.store.records(connection, "input_handback_resolutions", HandbackResolution)
                        if record.handback_id in handback_ids
                    ),
                )

        product = await asyncio.to_thread(read)
        journal = await self.sessions.snapshot(product.session_id)
        sdk_inputs = {record.envelope.input_id: record for record in journal.inputs}
        inputs: list[ExecutionInputView] = []
        for item in product.inputs:
            record = sdk_inputs.get(item.input_id)
            consumption = None
            if record is not None:
                consumption = InputConsumption(
                    sequence=record.sequence,
                    accepted_at=record.accepted_at,
                    state="consumed"
                    if record.step_id is not None
                    else "claimed"
                    if record.run_id is not None
                    else "accepted",
                    run_id=record.run_id,
                    step_id=record.step_id,
                )
            inputs.append(item.model_copy(update={"sdk_consumption": consumption}))
        result = product.model_copy(update={"inputs": tuple(inputs), "session_read_sequence": journal.revision})
        return JSON_OBJECT.validate_python(result.model_dump(mode="json"))

    async def space_detail(self, space_id: str) -> JsonObject:
        """Read a Space and all related summaries within the same product transaction."""

        def read() -> JsonObject:
            with self.store.transaction() as connection:
                space = self.store.require(connection, "spaces", space_id, Space)
                if space.deleted:
                    raise LookupError("The Space was deleted")
                jobs = tuple(
                    value
                    for value in self.store.records(connection, "executions", Execution)
                    if value.space_id == space_id
                )
                identities = {value.execution_id for value in jobs}
                sequence = self.store.sequence(connection)
                result = self.store.space_view(connection, space).model_dump(mode="json")
                result["read_sequence"] = sequence
                result["executions"] = self.store.page(
                    tuple(self.store.execution_view(connection, job) for job in jobs),
                    limit=100,
                    cursor=0,
                    sequence=sequence,
                ).model_dump(mode="json")
                result["artifacts"] = [
                    ArtifactMetadata.model_validate(record.model_dump(exclude={"relative_path"})).model_dump(
                        mode="json"
                    )
                    for record in self.store.records(connection, "artifacts", Artifact)
                    if record.owner_execution_id in identities
                ]
                result["attentions"] = [
                    self.store.attention_view(connection, record).model_dump(mode="json")
                    for record in self.store.records(connection, "attentions", Attention)
                    if record.execution_id in identities
                ]
                return JSON_OBJECT.validate_python(result)

        return await asyncio.to_thread(read)

    async def cleanup_scope(self, space_id: str) -> CleanupScope:
        """Read the controlled resource impact before issuing or accepting a deletion preview."""
        executions = tuple(
            value for value in await self.all_records("executions", Execution) if value.space_id == space_id
        )
        sessions = {value.session_id for value in executions}
        run_ids: list[str] = []
        for session_id in tuple(sessions):
            run_ids.extend(value.run_id for value in (await self.sessions.snapshot(session_id)).runs)
        sessions.update(await self.sessions.descendant_sessions(tuple(run_ids)))
        attempts = await self.all_records("node_attempts", NodeAttempt)
        owners = {value.execution_id for value in executions}
        sessions.update(
            value.session_id for value in attempts if value.execution_id in owners and value.session_id is not None
        )
        sdk: list[JsonObject] = []
        active = False
        for session_id in sorted(sessions):
            state = await self.sessions.snapshot(session_id)
            active = active or state.current_run_id is not None
            sdk.append({"session_id": session_id, "revision": state.revision})
        files = await asyncio.to_thread(self.content.task_file_scope, self.agents.workspace, space_id)
        facts = (
            ()
            if self.agents.facts is None
            else await asyncio.to_thread(
                self.agents.facts.list_facts, "space", space_id=space_id, include_inactive=True
            )
        )
        digest = fingerprint(
            JSON_OBJECT.validate_python(
                {"sdk": sdk, "files": files, "facts": [value.model_dump(mode="json") for value in facts]}
            )
        )
        return CleanupScope(
            sdk_session_count=len(sessions),
            task_file_count=len(files),
            space_fact_count=len(facts),
            digest=digest,
            blockers=("Active or paused SDK scopes still belong to this Space",) if active else (),
        )

    async def deletion_preview(self, space_id: str) -> DeletionPreview:
        """Bind product references and controlled resource identities to one reviewable preview."""
        scopes = await self.cleanup_scope(space_id)
        return await asyncio.to_thread(self.content.deletion_preview, space_id, scopes=scopes)

    async def finish_cleanup(self, request_id: str, *, expected_generation: str) -> CleanupRecord:
        """Reclaim every owned scope before publishing a Space deletion tombstone.

        Args:
            request_id: Stable accepted cleanup request whose progress is persisted.
            expected_generation: Generation that still owns this exact deletion plan.

        Returns:
            Committed completion or a visible failed record retaining retry ownership.

        Raises:
            ProductConflict: If the supplied generation is obsolete.
        """
        cleanup = await asyncio.to_thread(
            self.content.cleanup_record, request_id, expected_generation=expected_generation
        )
        if cleanup.state == "completed":
            return cleanup
        self.metrics.cleanup_attempts.add(1)
        try:
            if "sessions_planned" not in cleanup.completed_stages:
                session_ids: set[str] = set()
                run_ids: list[str] = []
                for execution_id in cleanup.plan.execution_ids:
                    execution = await self.require_execution(execution_id)
                    session_ids.add(execution.session_id)
                    journal = await self.sessions.snapshot(execution.session_id)
                    run_ids.extend(value.run_id for value in journal.runs)
                attempts = await self.all_records("node_attempts", NodeAttempt)
                session_ids.update(
                    value.session_id
                    for value in attempts
                    if value.execution_id in cleanup.plan.execution_ids and value.session_id is not None
                )
                session_ids.update(await self.sessions.descendant_sessions(tuple(run_ids)))
                cleanup = await asyncio.to_thread(
                    self.content.save_cleanup_sessions,
                    request_id,
                    expected_generation=expected_generation,
                    session_ids=tuple(sorted(session_ids)),
                )
            if "sessions" not in cleanup.completed_stages:
                for session_id in cleanup.session_ids:
                    await Session(session_id, self.sessions).clear()
                cleanup = await asyncio.to_thread(
                    self.content.complete_cleanup_stage,
                    request_id,
                    expected_generation=expected_generation,
                    stage="sessions",
                )
            if "facts" not in cleanup.completed_stages:
                if self.agents.facts is not None:
                    await asyncio.to_thread(
                        self.agents.facts.delete_space, cleanup.space_id, generation=expected_generation
                    )
                cleanup = await asyncio.to_thread(
                    self.content.complete_cleanup_stage,
                    request_id,
                    expected_generation=expected_generation,
                    stage="facts",
                )
            await asyncio.to_thread(
                self.content.reclaim_work_directory,
                request_id,
                expected_generation=expected_generation,
                workspace=self.agents.workspace,
            )
            await asyncio.to_thread(
                self.content.reclaim_execution_details, request_id, expected_generation=expected_generation
            )
            await asyncio.to_thread(self.content.reclaim_content, request_id, expected_generation=expected_generation)
            return await asyncio.to_thread(
                self.content.finish_deletion, request_id, expected_generation=expected_generation
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.error(f"Space cleanup remains pending request={request_id}", exc_info=True)
            return await asyncio.to_thread(
                self.content.fail_cleanup,
                request_id,
                expected_generation=expected_generation,
                error_name=type(error).__name__,
            )

    async def delete_space(
        self, space_id: str, *, request_id: str, expected_revision: int, preview_token: str
    ) -> JsonObject:
        """Own accepted generation-fenced cleanup after its HTTP subscriber disconnects."""
        space = await asyncio.to_thread(self.store.get_space, space_id)
        if space is None:
            raise LookupError(space_id)
        scopes = CleanupScope() if space.deletion_pending or space.deleted else await self.cleanup_scope(space_id)
        accepted = await asyncio.to_thread(
            self.content.accept_deletion,
            space_id,
            request_id=request_id,
            expected_revision=expected_revision,
            preview_token=preview_token,
            scopes=scopes,
        )
        task = self.cleanup_tasks.get(request_id)
        if task is None or (task.done() and not task.result().get("deleted")):

            async def finish() -> JsonObject:
                value = await self.finish_cleanup(request_id, expected_generation=accepted.generation)
                return {"deleted": value.state == "completed"}

            if self.group is None:
                raise RuntimeError("Deletion requires the running execution owner")
            task = self.group.create_task(finish())
            self.cleanup_tasks[request_id] = task
        result = await asyncio.shield(task)
        if not result.get("deleted"):
            raise ProductConflict("The accepted Space cleanup failed and can be retried")
        return result

    async def events(self, *, after_sequence: int = 0) -> AsyncIterator[ProductEvent]:
        """Observe product facts without owning or cancelling background work."""
        cursor = after_sequence
        while self.ready:
            page = await asyncio.to_thread(self.store.events, after_sequence=cursor, limit=100)
            for event in page.items:
                cursor = event.sequence
                yield event
            if not page.items:
                await asyncio.sleep(0.05)
