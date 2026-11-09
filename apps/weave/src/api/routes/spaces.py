import asyncio
from typing import Literal

from fastapi import APIRouter, Depends, Query
from pleiad.contracts import JsonObject

from pleiad_weave.api.dependencies import get_service, get_writable_service
from pleiad_weave.api.models import RevisionRequest, SpaceDelete, SpaceUpdate
from pleiad_weave.application.service import ExecutionService
from pleiad_weave.infrastructure.storage.product_models import DeletionPreview, Page, SpaceConfiguration, SpaceView

router = APIRouter(prefix="/v1/spaces")
service_dependency = Depends(get_service)
write_dependency = Depends(get_writable_service)


@router.get("")
async def spaces(
    query: str = "",
    status: Literal["active", "archived"] | None = None,
    cursor: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=100),
    service: ExecutionService = service_dependency,
) -> Page[SpaceView]:
    """Read paginated reusable Worker identities."""
    return await asyncio.to_thread(service.store.list_spaces, query=query, status=status, cursor=cursor, limit=limit)


@router.get("/{space_id}")
async def space(space_id: str, service: ExecutionService = service_dependency) -> JsonObject:
    """Read a Space and its related durable work from the shared application."""
    return await service.space_detail(space_id)


@router.patch("/{space_id}")
async def update(space_id: str, request: SpaceUpdate, service: ExecutionService = write_dependency) -> JsonObject:
    """Validate the entire host-authorized configuration before updating its revision."""
    configuration = SpaceConfiguration.model_validate(request.model_dump(exclude={"request_id", "expected_revision"}))
    await service.agents.validate_configuration(configuration)
    await asyncio.to_thread(
        service.store.update_space,
        space_id,
        configuration,
        request_id=request.request_id,
        expected_revision=request.expected_revision,
    )
    return await service.space_detail(space_id)


@router.post("/{space_id}/archive")
async def archive(space_id: str, request: RevisionRequest, service: ExecutionService = write_dependency) -> JsonObject:
    """Stop new admission while existing Space work retains its execution owner."""
    await asyncio.to_thread(
        service.store.archive_space,
        space_id,
        request_id=request.request_id,
        expected_revision=request.expected_revision,
    )
    return await service.space_detail(space_id)


@router.post("/{space_id}/restore")
async def restore(space_id: str, request: RevisionRequest, service: ExecutionService = write_dependency) -> JsonObject:
    """Revalidate current host resources before restoring Space admission."""
    current = await asyncio.to_thread(service.store.get_space, space_id)
    if current is None:
        raise LookupError("The Space does not exist")
    await service.agents.validate_configuration(
        SpaceConfiguration.model_validate(current.model_dump(include=set(SpaceConfiguration.model_fields)))
    )
    await asyncio.to_thread(
        service.store.archive_space,
        space_id,
        request_id=request.request_id,
        expected_revision=request.expected_revision,
        restore=True,
    )
    return await service.space_detail(space_id)


@router.get("/{space_id}/deletion-preview")
async def preview(space_id: str, service: ExecutionService = service_dependency) -> DeletionPreview:
    """Calculate an exact deletion impact without accepting cleanup."""
    return await service.deletion_preview(space_id)


@router.delete("/{space_id}")
async def delete(space_id: str, request: SpaceDelete, service: ExecutionService = write_dependency) -> JsonObject:
    """Await the service-owned cleanup associated with this accepted request."""
    return await service.delete_space(
        space_id,
        request_id=request.request_id,
        expected_revision=request.expected_revision,
        preview_token=request.preview_token,
    )
