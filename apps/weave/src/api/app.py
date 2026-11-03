"""FastAPI entrypoint backed by the same Application Service as CLI."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from pleiad_weave.api.routes.executions import router as execution_router
from pleiad_weave.api.routes.health import router as health_router
from pleiad_weave.application.service import ExecutionService


def create_app(service: ExecutionService) -> FastAPI:
    """Create a small HTTP surface without putting HTTP logic in Runtime."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        try:
            await service.start()
            yield
        finally:
            await service.shutdown()

    app = FastAPI(title="Pleiad Weave", version="0.1.0", lifespan=lifespan)
    app.state.service = service
    app.include_router(health_router)
    app.include_router(execution_router)

    return app
