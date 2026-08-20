"""Application orchestration above Runtime and below external entrypoints."""

import asyncio
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import uuid4

from loguru import logger

from pleiad.agent.contracts import AgentResult
from pleiad.application.models import ExecuteRequest
from pleiad.contracts import JsonObject
from pleiad.errors import ErrorCode, ErrorInfo, PleiadError, make_error
from pleiad.logging import logging_context
from pleiad.observability import (
    ObservabilityPort,
    TraceRecord,
    TraceScope,
    TraceService,
    TraceStatus,
)
from pleiad.runtime.events import EventBus, EventListener, RuntimeEvent
from pleiad.storage.models import ExecutionRecord, ExecutionStatus
from pleiad.storage.repository import ExecutionRepository


class PleiadRuntime(Protocol):
    """Runtime contract required by the Application Service."""

    events: EventBus | None

    async def run(
        self,
        objective: str,
        business_input: JsonObject,
        *,
        conversation_id: str,
        execution_id: str,
    ) -> AgentResult:
        """Run one objective through the Pleiad supervisor."""

    def graph_for_execution(self, execution_id: str) -> tuple[str, int] | None:
        """Return the graph revision selected during one execution."""


class ExecutionService(Protocol):
    """Application boundary consumed by HTTP and future UI entrypoints."""

    async def execute(self, request: ExecuteRequest) -> ExecutionRecord:
        """Execute one user objective."""

    def get(self, execution_id: str) -> ExecutionRecord | None:
        """Read one execution summary."""

    def events(self, execution_id: str) -> tuple[RuntimeEvent, ...]:
        """Replay one execution's event stream."""

    def traces(self, execution_id: str) -> tuple[TraceRecord, ...]:
        """Replay independent execution Trace records."""

    def subscribe_events(self, listener: EventListener) -> None:
        """Subscribe to live Runtime Events without exposing EventBus internals."""

    def unsubscribe_events(self, listener: EventListener) -> None:
        """Remove a live Runtime Event subscription."""


@dataclass(slots=True)
class PleiadApplicationService:
    """Submit objectives and persist their lifecycle summaries."""

    pleiad: PleiadRuntime
    repository: ExecutionRepository
    observability: ObservabilityPort | None = None
    trace_service: TraceService | None = None

    async def execute(self, request: ExecuteRequest) -> ExecutionRecord:
        """Run PleiadAgent and persist pending, running, and terminal states."""
        execution_id = uuid4().hex
        scope = (
            self.observability.trace(
                execution_id,
                conversation_id=request.conversation_id,
                name="execution",
                attributes={"objective_length": len(request.objective)},
            )
            if self.observability
            else nullcontext()
        )
        with logging_context(execution_id=execution_id), scope as trace:
            record = await self._execute(request, execution_id)
            if isinstance(trace, TraceScope):
                trace.set_status(
                    _trace_status(record.status),
                    _trace_error(record.error_code) if record.error_code else None,
                )
            return record

    async def _execute(self, request: ExecuteRequest, execution_id: str) -> ExecutionRecord:
        """Execute one request inside its already-created observability scope."""
        logger.info("开始执行目标: objective_length={}", len(request.objective))
        now = datetime.now(UTC)
        record = ExecutionRecord(
            execution_id=execution_id,
            conversation_id=request.conversation_id,
            objective=request.objective,
            status=ExecutionStatus.PENDING,
            created_at=now,
            updated_at=now,
        )
        self._emit(execution_id, "execution.started", {"objective": request.objective})
        self.repository.save(record)
        running = record.model_copy(
            update={"status": ExecutionStatus.RUNNING, "updated_at": datetime.now(UTC)}
        )
        self.repository.save(running)
        try:
            result = await self.pleiad.run(
                request.objective,
                request.input,
                conversation_id=request.conversation_id,
                execution_id=execution_id,
            )
            output = (
                result.structured_output if isinstance(result.structured_output, dict) else None
            )
            if output is None and result.text:
                output = {"text": result.text}
            status = _execution_status(result.status.value)
            error = result.error
        except asyncio.CancelledError:
            status = ExecutionStatus.CANCELLED
            output = None
            error = make_error(ErrorCode.AGENT_CANCELLED)
        except PleiadError as exc:
            status = ExecutionStatus.FAILED
            output = None
            error = exc.info
        except Exception as exc:
            logger.exception("执行目标时发生未预期异常")
            status = ExecutionStatus.FAILED
            output = None
            error = make_error(
                ErrorCode.SYSTEM_UNEXPECTED,
                details={"exception": exc.__class__.__name__},
            )
        graph_ref = self.pleiad.graph_for_execution(execution_id)
        terminal = running.model_copy(
            update={
                "status": status,
                "graph_id": graph_ref[0] if graph_ref else None,
                "graph_revision": graph_ref[1] if graph_ref else None,
                "output": output,
                "error_code": error.code if error else None,
                "error": error.message if error else None,
                "updated_at": datetime.now(UTC),
            }
        )
        saved = self.repository.save(terminal)
        event_type = {
            ExecutionStatus.SUCCEEDED: "execution.completed",
            ExecutionStatus.CANCELLED: "execution.cancelled",
        }.get(status, "execution.failed")
        self._emit(
            execution_id,
            event_type,
            {
                "status": status.value,
                "error": error.as_dict() if error else None,
            },
        )
        logger.info(
            "执行结束: status={} error_code={}",
            status.value,
            error.code if error else "-",
        )
        return saved

    def get(self, execution_id: str) -> ExecutionRecord | None:
        """Read one persisted execution summary."""
        return self.repository.get(execution_id)

    def events(self, execution_id: str) -> tuple[RuntimeEvent, ...]:
        """Replay events emitted for one execution."""
        if self.pleiad.events is None:
            return ()
        return self.pleiad.events.list(execution_id)

    def traces(self, execution_id: str) -> tuple[TraceRecord, ...]:
        """Replay independent Trace records for one execution."""
        if self.trace_service is None:
            return ()
        return self.trace_service.list(execution_id)

    def subscribe_events(self, listener: EventListener) -> None:
        """Subscribe to live Runtime Events through the application boundary."""
        if self.pleiad.events is not None:
            self.pleiad.events.subscribe(listener)

    def unsubscribe_events(self, listener: EventListener) -> None:
        """Remove a live Runtime Event subscription through the application boundary."""
        if self.pleiad.events is not None:
            self.pleiad.events.unsubscribe(listener)

    def _emit(self, execution_id: str, event_type: str, data: dict[str, object]) -> None:
        if self.pleiad.events:
            self.pleiad.events.emit(execution_id, event_type, data)


def _execution_status(status: str) -> ExecutionStatus:
    """Map Agent status to the durable Application status vocabulary."""
    if status == "succeeded":
        return ExecutionStatus.SUCCEEDED
    if status == "cancelled":
        return ExecutionStatus.CANCELLED
    return ExecutionStatus.FAILED


def _trace_status(status: ExecutionStatus) -> TraceStatus:
    """Map durable execution status to the tracing vocabulary."""
    if status is ExecutionStatus.SUCCEEDED:
        return TraceStatus.SUCCEEDED
    if status is ExecutionStatus.CANCELLED:
        return TraceStatus.CANCELLED
    if status is ExecutionStatus.RUNNING:
        return TraceStatus.RUNNING
    return TraceStatus.FAILED


def _trace_error(error_code: str | None) -> ErrorInfo | None:
    """Rebuild a catalogued ErrorInfo for the terminal Trace record."""
    if error_code is None:
        return None
    try:
        return make_error(ErrorCode(error_code))
    except ValueError:
        return make_error(
            ErrorCode.SYSTEM_UNEXPECTED,
            details={"error_code": error_code},
        )
