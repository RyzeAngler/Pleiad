import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from types import TracebackType
from uuid import uuid4

from pleiad.contracts import JsonObject
from pleiad.events.supervisor import (
    DelegationStatus,
    WorkerCompletedEvent,
    WorkerConsumedEvent,
    WorkerLifecycleEvent,
    WorkerStartedEvent,
    WorkerSubmittedEvent,
)
from pleiad.memory.short_memory import ShortMemory
from pleiad.orchestration.errors import (
    DelegationNotFoundError,
    OrchestrationConfigurationError,
    OrchestrationStateError,
    WorkerBudgetError,
    WorkerNotFoundError,
)
from pleiad.orchestration.supervisor.state import (
    DelegationRecord,
    SupervisorSnapshot,
)
from pleiad.orchestration.supervisor.worker import Worker
from pleiad.runtime.lifecycle.result import RunResult
from pleiad.runtime.lifecycle.state import RunStatus
from pleiad.tools.contracts import ParentRunLimits, ToolContext


@dataclass(slots=True)
class ParentBudget:
    """Mutable reservation ledger for concurrent children of one parent Run."""

    remaining_turns: int
    remaining_output_tokens: int


@dataclass(frozen=True, slots=True)
class BudgetReservation:
    """Budget held for one accepted Worker until its result is settled."""

    parent_run_id: str
    turns: int
    output_tokens: int


class Supervisor:
    """Own synchronous and explicitly scoped asynchronous Worker Runs."""

    def __init__(self, workers: Mapping[str, Worker] | tuple[Worker, ...]) -> None:
        """Bind the host allowlist of Worker Runs."""
        selected = dict(workers) if isinstance(workers, Mapping) else {worker.worker_id: worker for worker in workers}
        if not isinstance(workers, Mapping) and len(selected) != len(workers):
            raise OrchestrationConfigurationError("Worker IDs must be unique.")
        if any(key != worker.worker_id for key, worker in selected.items()):
            raise OrchestrationConfigurationError("Worker mapping keys must match Worker IDs.")
        self.workers = selected
        self.records: dict[str, DelegationRecord] = {}
        self.events: list[WorkerLifecycleEvent] = []
        self.delivered_event_ids: set[str] = set()
        self.tasks: dict[str, asyncio.Task[RunResult]] = {}
        self.signals: dict[str, asyncio.Event] = {}
        self.memories: dict[str, ShortMemory] = {}
        self.task_group: asyncio.TaskGroup | None = None
        self.parent_budgets: dict[str, ParentBudget] = {}
        self.reservations: dict[str, BudgetReservation] = {}
        self.sequence = 0

    async def __aenter__(self) -> "Supervisor":
        """Open a structured scope that owns submitted Worker tasks."""
        if self.task_group is not None:
            raise RuntimeError("Supervisor scope is already open")
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

    def worker_for(self, worker: str | Worker) -> Worker:
        """Resolve one explicitly bound Worker or Agent-facing Worker ID."""
        if isinstance(worker, Worker):
            bound = self.workers.get(worker.worker_id)
            if bound is worker:
                return worker
            raise OrchestrationConfigurationError(f"Worker is not bound to this Supervisor: {worker.worker_id}.")
        try:
            return self.workers[worker]
        except KeyError as error:
            raise WorkerNotFoundError(worker) from error

    def worker_for_agent(self, agent_name: str) -> Worker:
        """Resolve the Worker whose Agent has the requested stable name."""
        matches = tuple(worker for worker in self.workers.values() if worker.agent.name == agent_name)
        if len(matches) != 1:
            raise OrchestrationConfigurationError(f"Worker Agent binding is ambiguous or missing: {agent_name}.")
        return matches[0]

    async def run_worker(self, worker: str | Worker, task: str, context: ToolContext) -> RunResult:
        """Run one Worker synchronously inside the caller's cancellation scope."""
        selected = self.worker_for(worker)
        if not task.strip():
            raise OrchestrationConfigurationError("Worker task must not be blank.")
        selected.child_config(context)
        selected.parent_policy(context)
        delegation_id = uuid4().hex
        child_context = self.reserve_budget(delegation_id, selected, context)
        child_config = selected.child_config(child_context)
        parent_policy = selected.parent_policy(child_context)
        self.records[delegation_id] = DelegationRecord(
            delegation_id=delegation_id,
            worker_id=selected.worker_id,
            task=task,
            status=DelegationStatus.RUNNING,
            parent_run_id=context.run_id,
        )
        self.signals[delegation_id] = asyncio.Event()
        self.record_lifecycle(
            WorkerSubmittedEvent,
            delegation_id,
            parent_run_id=context.run_id,
            worker_id=selected.worker_id,
        )
        self.record_lifecycle(
            WorkerStartedEvent,
            delegation_id,
            parent_run_id=context.run_id,
            worker_id=selected.worker_id,
        )
        memory = selected.memory(delegation_id)
        self.memories[delegation_id] = memory
        try:
            result = await selected.runner.run(
                selected.agent,
                task,
                memory=memory,
                context_blocks=selected.context_blocks(task),
                config=child_config,
                handoffs=selected.handoffs,
                parent_run_id=context.run_id,
                parent_policy=parent_policy,
            )
        except asyncio.CancelledError:
            self.records[delegation_id] = self.records[delegation_id].model_copy(
                update={"status": DelegationStatus.CANCELLED}
            )
            raise
        except Exception as error:
            self.records[delegation_id] = self.records[delegation_id].model_copy(
                update={
                    "status": DelegationStatus.FAILED,
                    "error_code": "orchestration.worker.system_failure",
                    "error_message": str(error),
                }
            )
            raise
        self.record_result(delegation_id, result)
        self.settle_budget(delegation_id, result)
        return result

    async def submit_worker(self, worker: str, task: str, context: ToolContext) -> JsonObject:
        """Accept one asynchronous Worker and return only its delegation identity."""
        selected = self.worker_for(worker)
        if not task.strip():
            raise OrchestrationConfigurationError("Worker task must not be blank.")
        if self.task_group is None:
            raise OrchestrationConfigurationError("Asynchronous Worker submission requires an open Supervisor scope.")
        selected.child_config(context)
        selected.parent_policy(context)
        delegation_id = uuid4().hex
        child_context = self.reserve_budget(delegation_id, selected, context)
        selected.child_config(child_context)
        selected.parent_policy(child_context)
        self.records[delegation_id] = DelegationRecord(
            delegation_id=delegation_id,
            worker_id=selected.worker_id,
            task=task,
            status=DelegationStatus.ACCEPTED,
            parent_run_id=context.run_id,
        )
        self.signals[delegation_id] = asyncio.Event()
        self.record_lifecycle(
            WorkerSubmittedEvent,
            delegation_id,
            parent_run_id=context.run_id,
            worker_id=selected.worker_id,
        )
        coroutine = self.run_submitted(delegation_id, selected, task, child_context)
        task_handle = self.task_group.create_task(coroutine)
        self.tasks[delegation_id] = task_handle
        return {"delegation_id": delegation_id, "status": DelegationStatus.ACCEPTED.value}

    async def run_submitted(
        self,
        delegation_id: str,
        worker: Worker,
        task: str,
        context: ToolContext,
    ) -> RunResult:
        """Execute accepted work and publish exactly one completion event."""
        record = self.records[delegation_id]
        self.records[delegation_id] = record.model_copy(update={"status": DelegationStatus.RUNNING})
        self.record_lifecycle(
            WorkerStartedEvent,
            delegation_id,
            parent_run_id=record.parent_run_id,
            worker_id=worker.worker_id,
        )
        try:
            result = await worker.runner.run(
                worker.agent,
                task,
                memory=self.store_memory(delegation_id, worker),
                context_blocks=worker.context_blocks(task),
                config=worker.child_config(context),
                handoffs=worker.handoffs,
                parent_run_id=context.run_id,
                parent_policy=worker.parent_policy(context),
            )
        except asyncio.CancelledError:
            self.record_terminal(delegation_id, DelegationStatus.CANCELLED)
            raise
        except Exception as error:
            self.record_terminal(
                delegation_id,
                DelegationStatus.FAILED,
                error_code="orchestration.worker.system_failure",
                error_message=str(error),
            )
            raise
        self.record_result(delegation_id, result)
        self.settle_budget(delegation_id, result)
        return result

    def reserve_budget(self, delegation_id: str, worker: Worker, context: ToolContext) -> ToolContext:
        """Atomically reserve a bounded share of one parent Run budget."""
        limits = context.parent_limits
        parent_run_id = context.run_id
        if limits is None or parent_run_id is None:
            return context
        pool = self.parent_budgets.get(parent_run_id)
        if pool is None:
            pool = ParentBudget(limits.remaining_turns, limits.remaining_output_tokens)
            self.parent_budgets[parent_run_id] = pool
        turns = min(worker.config.max_turns, pool.remaining_turns)
        output_tokens = min(worker.config.max_output_tokens, pool.remaining_output_tokens)
        if turns < 1 or output_tokens < 1:
            raise WorkerBudgetError()
        pool.remaining_turns -= turns
        pool.remaining_output_tokens -= output_tokens
        self.reservations[delegation_id] = BudgetReservation(parent_run_id, turns, output_tokens)
        reserved_limits = ParentRunLimits(
            deadline=limits.deadline,
            remaining_turns=turns,
            remaining_output_tokens=output_tokens,
            tool_concurrency=limits.tool_concurrency,
        )
        return ToolContext.model_validate({**context.model_dump(), "parent_limits": reserved_limits})

    def settle_budget(self, delegation_id: str, result: RunResult) -> None:
        """Return only confirmed unused budget after a child reaches a terminal result."""
        if result.status is RunStatus.INTERRUPTED:
            return
        reservation = self.reservations.pop(delegation_id, None)
        if reservation is None:
            return
        pool = self.parent_budgets[reservation.parent_run_id]
        pool.remaining_turns += max(0, reservation.turns - result.usage.model_calls)
        pool.remaining_output_tokens += max(0, reservation.output_tokens - result.usage.budget_output_tokens)

    def resume_context(self, delegation_id: str, context: ToolContext) -> ToolContext:
        """Reuse the original child reservation under the current parent deadline."""
        record = self.records[delegation_id]
        if record.parent_run_id != context.run_id:
            raise OrchestrationStateError("A Worker cannot resume under a different parent Run.")
        reservation = self.reservations.get(delegation_id)
        if reservation is None:
            if context.parent_limits is not None:
                raise OrchestrationStateError("A paused Worker budget reservation is missing.")
            return context
        limits = context.parent_limits
        if limits is None:
            raise OrchestrationStateError("A paused Worker requires its parent Run limits.")
        reserved_limits = ParentRunLimits(
            deadline=limits.deadline,
            remaining_turns=reservation.turns,
            remaining_output_tokens=reservation.output_tokens,
            tool_concurrency=limits.tool_concurrency,
        )
        return ToolContext.model_validate({**context.model_dump(), "parent_limits": reserved_limits})

    async def wait_worker(self, delegation_id: str) -> WorkerCompletedEvent:
        """Wait for one accepted Worker completion without polling or sleeping."""
        while True:
            event = next(
                (
                    candidate
                    for candidate in reversed(self.events)
                    if isinstance(candidate, WorkerCompletedEvent) and candidate.delegation_id == delegation_id
                ),
                None,
            )
            if event is not None:
                if event.event_id not in self.delivered_event_ids:
                    self.delivered_event_ids.add(event.event_id)
                    record = self.records[delegation_id]
                    self.record_lifecycle(
                        WorkerConsumedEvent,
                        delegation_id,
                        parent_run_id=record.parent_run_id,
                        child_run_id=record.child_run_id,
                        worker_id=record.worker_id,
                        completion_event_id=event.event_id,
                    )
                return event
            if delegation_id not in self.records:
                raise DelegationNotFoundError(delegation_id)
            await self.signals[delegation_id].wait()

    def events_since(self, sequence: int = 0) -> tuple[WorkerLifecycleEvent, ...]:
        """Return Worker lifecycle events in stable sequence order."""
        if sequence < 0:
            raise ValueError("sequence must not be negative")
        return tuple(
            sorted((event for event in self.events if event.sequence > sequence), key=lambda event: event.sequence)
        )

    def snapshot(self) -> SupervisorSnapshot:
        """Return JSON-only coordination state for host-controlled recovery."""
        return SupervisorSnapshot(
            delegations=tuple(sorted(self.records.values(), key=lambda item: item.delegation_id)),
            events=tuple(sorted(self.events, key=lambda item: item.sequence)),
            delivered_event_ids=tuple(sorted(self.delivered_event_ids)),
        )

    def restore(self, snapshot: SupervisorSnapshot) -> None:
        """Restore completed coordination facts after bindings are re-established."""
        if any(
            record.status in {DelegationStatus.ACCEPTED, DelegationStatus.RUNNING} for record in snapshot.delegations
        ):
            raise OrchestrationConfigurationError("Pending Worker delegations require host-controlled reattachment.")
        self.records = {record.delegation_id: record for record in snapshot.delegations}
        self.events = list(snapshot.events)
        self.delivered_event_ids = set(snapshot.delivered_event_ids)
        self.sequence = max((event.sequence for event in self.events), default=0)
        self.signals = {record.delegation_id: asyncio.Event() for record in snapshot.delegations}
        for event in self.events:
            if isinstance(event, WorkerCompletedEvent):
                self.signals[event.delegation_id].set()

    def record_result(self, delegation_id: str, result: RunResult) -> None:
        """Project one terminal RunResult into one controlled completion event."""
        status = {
            RunStatus.SUCCEEDED: DelegationStatus.SUCCEEDED,
            RunStatus.FAILED: DelegationStatus.FAILED,
            RunStatus.INTERRUPTED: DelegationStatus.INTERRUPTED,
            RunStatus.TIMED_OUT: DelegationStatus.FAILED,
            RunStatus.LIMIT_REACHED: DelegationStatus.FAILED,
        }[result.status]
        output = result.structured_output
        if output is None:
            output = {"text": result.final_output}
        if result.checkpoint is not None:
            output = {"checkpoint": result.checkpoint.model_dump(mode="json")}
        self.record_terminal(
            delegation_id,
            status,
            child_run_id=result.run_id,
            result=output,
            error_code=result.error.code if result.error is not None else None,
            error_message=result.error.message if result.error is not None else None,
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
            WorkerCompletedEvent(
                event_id=uuid4().hex,
                run_id=record.parent_run_id,
                delegation_id=delegation_id,
                sequence=self.sequence,
                parent_run_id=record.parent_run_id,
                child_run_id=child_run_id,
                worker_id=record.worker_id,
                status=status,
                result=result,
                error_code=error_code,
                error_message=error_message,
            )
        )
        self.signals[delegation_id].set()

    def record_lifecycle(
        self,
        event_type: type[WorkerSubmittedEvent] | type[WorkerStartedEvent] | type[WorkerConsumedEvent],
        delegation_id: str,
        *,
        parent_run_id: str | None,
        worker_id: str,
        child_run_id: str | None = None,
        completion_event_id: str | None = None,
    ) -> None:
        """Append one non-terminal Worker lifecycle fact."""
        self.sequence += 1
        common: dict[str, object] = {
            "run_id": parent_run_id,
            "delegation_id": delegation_id,
            "sequence": self.sequence,
            "parent_run_id": parent_run_id,
            "child_run_id": child_run_id,
            "worker_id": worker_id,
        }
        if event_type is WorkerConsumedEvent:
            if completion_event_id is None:
                raise ValueError("A consumed Worker event requires its completion event ID.")
            self.events.append(WorkerConsumedEvent(**common, completion_event_id=completion_event_id))
            return
        self.events.append(event_type(**common))

    def store_memory(self, delegation_id: str, worker: Worker) -> ShortMemory:
        """Return the one child memory handle retained for pause/resume."""
        memory = self.memories.get(delegation_id)
        if memory is None:
            memory = worker.memory(delegation_id)
            self.memories[delegation_id] = memory
        return memory

    def memory_for_interruption(self, interruption: object, worker: Worker) -> ShortMemory:
        """Resolve the paused child memory from explicit delegation details."""
        details = getattr(interruption, "details", {})
        delegation_id = details.get("delegation_id") if isinstance(details, dict) else None
        if not isinstance(delegation_id, str):
            raise OrchestrationStateError("A paused Worker interruption is missing its delegation ID.")
        record = self.records.get(delegation_id)
        if record is None or record.worker_id != worker.worker_id:
            raise OrchestrationStateError("A paused Worker delegation mapping is missing or incompatible.")
        if record.status is not DelegationStatus.INTERRUPTED:
            raise OrchestrationStateError("A Worker delegation can only resume from the interrupted state.")
        memory = self.memories.get(delegation_id)
        if memory is None:
            raise OrchestrationStateError("A paused Worker memory mapping is missing.")
        return memory
