"""Health endpoint for process and container probes."""

from fastapi import APIRouter

from pleiad_weave.application.dto import HealthResponse

router = APIRouter()


@router.get("/healthz", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Report that the HTTP process is serving requests."""
    return HealthResponse()
