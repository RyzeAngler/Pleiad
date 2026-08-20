"""Application lifecycle and durable runtime record tests."""

import asyncio
from pathlib import Path

from pleiad.agent.contracts import AgentResult, AgentStatus
from pleiad.application.models import ExecuteRequest
from pleiad.application.service import PleiadApplicationService
from pleiad.observability import TraceRecordType, TraceService, create_tracer
from pleiad.observability.stores.jsonl import JsonlTraceStore
from pleiad.runtime.events import EventBus
from pleiad.storage.repository import ExecutionRepository


class FakePleiad:
    """Small Pleiad double exposing the same lifecycle dependencies as Runtime."""

    def __init__(self) -> None:
        self.events: EventBus | None = EventBus()

    async def run(
        self,
        objective: str,
        business_input: dict[str, object],
        *,
        conversation_id: str,
        execution_id: str,
    ) -> AgentResult:
        del objective, business_input, conversation_id, execution_id
        return AgentResult(
            run_id="agent-run",
            status=AgentStatus.SUCCEEDED,
            structured_output={"answer": "完成"},
        )

    def graph_for_execution(self, execution_id: str) -> tuple[str, int] | None:
        return ("graph-1", 2)


def test_application_persists_terminal_lifecycle_and_graph_reference(tmp_path: Path) -> None:
    pleiad = FakePleiad()
    service = PleiadApplicationService(pleiad, ExecutionRepository(tmp_path / "executions"))

    record = asyncio.run(service.execute(ExecuteRequest(objective="完成任务")))

    assert record.status.value == "succeeded"
    assert record.graph_id == "graph-1"
    assert record.graph_revision == 2
    assert service.get(record.execution_id) == record
    assert [event.event_type for event in service.events(record.execution_id)] == [
        "execution.started",
        "execution.completed",
    ]


def test_application_creates_one_root_trace_for_execution(tmp_path: Path) -> None:
    """Execution owns a Trace while Runtime Events remain a separate stream."""
    pleiad = FakePleiad()
    trace_service = TraceService(JsonlTraceStore(tmp_path / "traces"))
    tracer = create_tracer(trace_service)
    service = PleiadApplicationService(
        pleiad,
        ExecutionRepository(tmp_path / "executions"),
        tracer,
        trace_service,
    )

    record = asyncio.run(service.execute(ExecuteRequest(objective="创建追踪")))
    traces = service.traces(record.execution_id)
    trace_service.close()

    assert [item.record_type for item in traces] == [
        TraceRecordType.TRACE_STARTED,
        TraceRecordType.TRACE_ENDED,
    ]
    assert traces[0].trace_id == traces[1].trace_id
    assert traces[1].status.value == "succeeded"
