"""FastAPI entrypoint backed by the same Application Service as CLI."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from pleiad_weave.api.routes.executions import router as execution_router
from pleiad_weave.api.routes.health import router as health_router
from pleiad_weave.api.routes.workbench import router as workbench_router
from pleiad_weave.api.routes.workspaces import router as workspace_router
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
    app.include_router(workbench_router)
    app.include_router(execution_router)
    app.include_router(workspace_router)

    web_root = Path(__file__).resolve().parents[1] / "web" / "assets"
    if (web_root / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=web_root / "assets"), name="web-assets")

    @app.get("/", response_class=FileResponse, include_in_schema=False)
    async def web_index() -> FileResponse:
        """Serve the bundled workspace UI from the same origin as its API.

        Returns:
            The frontend entry document with a fresh asset manifest on reload.

        Raises:
            HTTPException: If the checkout has not built its frontend assets.
        """
        index = web_root / "index.html"
        if not index.is_file():
            raise HTTPException(
                status_code=503,
                detail="Web assets are missing. Run npm ci and npm run build in apps/weave/web, then restart serve.",
            )
        return FileResponse(
            index,
            headers={
                "Cache-Control": "no-cache",
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "same-origin",
                "Content-Security-Policy": (
                    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                    "connect-src 'self'; img-src 'self' data:; font-src 'self'; "
                    "object-src 'none'; base-uri 'self'; frame-ancestors 'none'"
                ),
            },
        )

    return app
