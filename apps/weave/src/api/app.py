import logging
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pleiad.errors.base import ErrorType, PleiadError
from starlette.exceptions import HTTPException as StarletteHTTPException

from pleiad_weave.api.routes.chat import router as chat_router
from pleiad_weave.api.routes.content import router as content_router
from pleiad_weave.api.routes.executions import router as execution_router
from pleiad_weave.api.routes.health import router as health_router
from pleiad_weave.api.routes.resources import router as resource_router
from pleiad_weave.api.routes.sources import router as source_router
from pleiad_weave.api.routes.spaces import router as space_router
from pleiad_weave.api.security import UploadLimitMiddleware, error_response, guard_request
from pleiad_weave.application.service import ExecutionService
from pleiad_weave.infrastructure.storage.product_content import ContentCorruptError
from pleiad_weave.infrastructure.storage.product_models import ProductCapacityError, ProductConflict

logger = logging.getLogger(__name__)


def create_app(service: ExecutionService) -> FastAPI:
    """Compose the only public HTTP contract around the shared application owner."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await service.start()
        try:
            yield
        finally:
            await service.shutdown()

    app = FastAPI(title="Pleiad Weave", version="0.1.0", lifespan=lifespan)
    token = secrets.token_urlsafe(32)
    app.state.service = service
    app.state.csrf_token = token
    app.add_middleware(UploadLimitMiddleware)

    @app.middleware("http")
    async def security(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        """Verify local browser ownership before any product mutation."""
        request.state.request_id = uuid4().hex
        return await guard_request(request, call_next, token)

    @app.exception_handler(ProductConflict)
    async def conflict(request: Request, error: ProductConflict) -> JSONResponse:
        """Preserve explicit revision and idempotency conflicts for client review."""
        response = error_response(409, "product.conflict", str(error), request.state.request_id)
        if error.current_revision is not None:
            return JSONResponse(
                status_code=409,
                content={
                    "code": "product.conflict",
                    "message": str(error),
                    "request_id": request.state.request_id,
                    "retryable": False,
                    "current_revision": error.current_revision,
                },
            )
        return response

    @app.exception_handler(ProductCapacityError)
    async def capacity(request: Request, error: ProductCapacityError) -> JSONResponse:
        """Reject admission limits without accepting a partial product change."""
        return error_response(429, "product.capacity", str(error), request.state.request_id)

    @app.exception_handler(ContentCorruptError)
    async def corrupt(request: Request, error: ContentCorruptError) -> JSONResponse:
        """Expose a checked content failure without reporting empty success."""
        return error_response(409, "artifact_corrupt", str(error), request.state.request_id)

    @app.exception_handler(LookupError)
    async def missing(request: Request, error: LookupError) -> JSONResponse:
        """Report an unavailable owned record without exposing host filesystem data."""
        return error_response(
            404, "product.not_found", "The requested owned record is unavailable", request.state.request_id
        )

    @app.exception_handler(ValueError)
    async def invalid(request: Request, error: ValueError) -> JSONResponse:
        """Reject a caller contract violation through the uniform public envelope."""
        return error_response(422, "product.invalid_request", str(error), request.state.request_id)

    @app.exception_handler(RequestValidationError)
    async def validation(request: Request, error: RequestValidationError) -> JSONResponse:
        """Report invalid public fields while leaving original payloads out of errors."""
        return JSONResponse(
            status_code=422,
            content={
                "code": "http.validation",
                "message": "The request does not match the public contract",
                "request_id": request.state.request_id,
                "retryable": False,
                "details": [{"field": list(value["loc"]), "message": value["msg"]} for value in error.errors()],
            },
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, error: StarletteHTTPException) -> JSONResponse:
        """Keep HTTP boundary rejections consistent with product errors."""
        message = error.detail
        return error_response(error.status_code, f"http.{error.status_code}", message, request.state.request_id)

    @app.exception_handler(PleiadError)
    async def sdk_error(request: Request, error: PleiadError) -> JSONResponse:
        """Preserve stable SDK codes while keeping diagnostic causes in local logs."""
        if error.code == "weave.not_ready":
            return error_response(503, error.code, "The workspace owner is not ready", request.state.request_id)
        if error.code == "session.conflict":
            return error_response(409, error.code, error.message, request.state.request_id)
        if error.type is ErrorType.BUSINESS:
            return error_response(422, error.code, error.message, request.state.request_id)
        logger.error(f"HTTP source fault request={request.state.request_id} code={error.code}", exc_info=error)
        return error_response(500, error.code, "The service could not complete this request", request.state.request_id)

    app.include_router(health_router)
    app.include_router(chat_router)
    app.include_router(space_router)
    app.include_router(execution_router)
    app.include_router(content_router)
    app.include_router(resource_router)
    app.include_router(source_router)
    web_root = Path(__file__).resolve().parents[1] / "web" / "assets"
    if (web_root / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=web_root / "assets"), name="web-assets")

    @app.get("/", response_class=FileResponse, include_in_schema=False)
    async def web_index() -> FileResponse:
        """Serve the bundled single-window product UI from its API origin."""
        index = web_root / "index.html"
        if not index.is_file():
            raise HTTPException(status_code=503, detail="Web assets are missing. Build the Weave frontend and restart.")
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
