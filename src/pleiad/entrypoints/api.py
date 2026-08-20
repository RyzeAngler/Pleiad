"""FastAPI entrypoint backed by the same Application Service as CLI."""

from fastapi import FastAPI, HTTPException

from pleiad.application.models import (
    EventResponse,
    ExecuteRequest,
    ExecutionResponse,
    HealthResponse,
)
from pleiad.application.service import ExecutionService


def create_app(service: ExecutionService) -> FastAPI:
    """Create a small HTTP surface without putting HTTP logic in Runtime."""
    app = FastAPI(title="Pleiad", version="0.1.0")

    @app.get("/healthz", response_model=HealthResponse)
    async def health() -> HealthResponse:
        return HealthResponse()

    @app.post("/v1/executions", response_model=ExecutionResponse)
    async def execute(request: ExecuteRequest) -> ExecutionResponse:
        return ExecutionResponse(execution=await service.execute(request))

    @app.get("/v1/executions/{execution_id}", response_model=ExecutionResponse)
    async def get_execution(execution_id: str) -> ExecutionResponse:
        record = service.get(execution_id)
        if record is None:
            raise HTTPException(status_code=404, detail="execution not found")
        return ExecutionResponse(execution=record)

    @app.get("/v1/executions/{execution_id}/events", response_model=list[EventResponse])
    async def events(execution_id: str) -> list[EventResponse]:
        """Replay the common Runtime event stream for API and future TUI clients."""
        if service.get(execution_id) is None:
            raise HTTPException(status_code=404, detail="execution not found")
        return [EventResponse.from_event(event) for event in service.events(execution_id)]

    return app
