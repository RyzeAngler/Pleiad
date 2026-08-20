"""HTTP entrypoint tests independent of model credentials."""

from collections.abc import Callable
from datetime import UTC, datetime

from fastapi.testclient import TestClient

from pleiad.application.models import ExecuteRequest
from pleiad.entrypoints.api import create_app
from pleiad.observability import TraceRecord
from pleiad.runtime.events import RuntimeEvent
from pleiad.storage.models import ExecutionRecord, ExecutionStatus


class FakeService:
    """Minimal application-service double for HTTP boundary tests."""

    listener: Callable[[RuntimeEvent], None] | None = None

    def subscribe_events(self, listener: Callable[[RuntimeEvent], None]) -> None:
        """Accept a Runtime Event listener for protocol completeness."""
        self.listener = listener

    def unsubscribe_events(self, listener: Callable[[RuntimeEvent], None]) -> None:
        """Remove the active Runtime Event listener."""
        if self.listener is listener:
            self.listener = None

    async def execute(self, request: ExecuteRequest) -> ExecutionRecord:
        """Return one deterministic execution record."""
        now = datetime.now(UTC)
        return ExecutionRecord(
            execution_id="e1",
            conversation_id=request.conversation_id,
            objective=request.objective,
            status=ExecutionStatus.SUCCEEDED,
            output={"ok": True},
            created_at=now,
            updated_at=now,
        )

    def get(self, execution_id: str) -> ExecutionRecord | None:
        """Return no record for the not-found test."""
        return None

    def events(self, execution_id: str) -> tuple[RuntimeEvent, ...]:
        """Return an empty event stream for the HTTP boundary test."""
        return ()

    def traces(self, execution_id: str) -> tuple[TraceRecord, ...]:
        """Return an empty independent trace stream for the HTTP boundary test."""
        return ()


def test_api_exposes_health_and_execution_contract() -> None:
    """Health and execution endpoints keep HTTP outside Runtime."""
    client = TestClient(create_app(FakeService()))
    assert client.get("/healthz").json() == {"status": "ok"}
    response = client.post("/v1/executions", json={"objective": "测试"})
    assert response.status_code == 200
    assert response.json()["execution"]["execution_id"] == "e1"
    assert client.get("/v1/executions/missing").status_code == 404
