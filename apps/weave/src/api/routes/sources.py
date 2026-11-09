import asyncio

from fastapi import APIRouter, Depends, Query
from pleiad.contracts import JsonObject, JsonValue

from pleiad_weave.api.dependencies import get_service, get_writable_service
from pleiad_weave.api.models import RequestIdentity
from pleiad_weave.application.service import ExecutionService
from pleiad_weave.infrastructure.storage.observations import (
    DirectoryObserver,
    ObservationReceipt,
    SourceHealth,
    SourceObservation,
)
from pleiad_weave.infrastructure.storage.product_models import Page

router = APIRouter(prefix="/v1/sources")
service_dependency = Depends(get_service)
write_dependency = Depends(get_writable_service)


def require_observer(service: ExecutionService, source_id: str) -> DirectoryObserver:
    """Restrict source access to the directory currently authorized by the host."""
    observer = service.observer
    if observer is None or observer.source_id != source_id:
        raise LookupError("The requested host source is not configured")
    return observer


@router.get("")
async def sources(service: ExecutionService = service_dependency) -> JsonObject:
    """Read the host's observation-only source configuration without probing files."""
    items: list[JsonValue] = []
    if service.observer is not None:
        items.append(service.observer.configuration())
    return {"items": items, "total": len(items), "enabled": service.observer is not None}


@router.get("/{source_id}/health")
async def health(source_id: str, service: ExecutionService = service_dependency) -> SourceHealth:
    """Run an explicit directory accessibility check without observing or dispatching."""
    return await asyncio.to_thread(require_observer(service, source_id).health)


@router.get("/{source_id}/observations")
async def observations(
    source_id: str,
    after_sequence: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=100),
    service: ExecutionService = service_dependency,
) -> Page[SourceObservation]:
    """Read committed source facts without rescanning external files."""
    return await asyncio.to_thread(
        require_observer(service, source_id).observations, after_sequence=after_sequence, limit=limit
    )


@router.post("/{source_id}/observe")
async def observe(
    source_id: str, request: RequestIdentity, service: ExecutionService = write_dependency
) -> ObservationReceipt:
    """Commit only the metadata changes found by this explicitly requested read-only scan."""
    return await asyncio.to_thread(require_observer(service, source_id).observe, request_id=request.request_id)
