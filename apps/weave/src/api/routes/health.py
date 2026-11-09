import asyncio

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pleiad.errors.base import SystemFault

from pleiad_weave.api.dependencies import get_service
from pleiad_weave.application.service import ExecutionService

router = APIRouter()
service_dependency = Depends(get_service)


@router.get("/healthz")
async def health() -> dict[str, str]:
    """Report that the HTTP process is serving requests."""
    return {"status": "ok"}


@router.get("/readyz")
async def ready(service: ExecutionService = service_dependency) -> JSONResponse:
    """Report workspace ownership separately from process liveness."""
    if not service.ready:
        return JSONResponse(status_code=503, content={"ready": False})
    try:
        await asyncio.to_thread(service.store.probe_writable)
    except (SystemFault, OSError):
        return JSONResponse(status_code=503, content={"ready": False, "reason": "storage_unavailable"})
    return JSONResponse(content={"ready": True})
