"""Execution submission and replay endpoints."""

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Query

from pleiad_weave.api.dependencies import get_service
from pleiad_weave.application.dto import (
    ApprovalDecisionRequest,
    ApprovalRequest,
    EventResponse,
    ExecuteRequest,
    ExecutionResponse,
)
from pleiad_weave.application.errors import ApprovalConflictError
from pleiad_weave.application.service import ExecutionService

router = APIRouter(prefix="/v1/executions")
service_dependency = Depends(get_service)


@router.post("", response_model=ExecutionResponse, status_code=202)
async def submit(
    request: ExecuteRequest,
    service: ExecutionService = service_dependency,
) -> ExecutionResponse:
    """Queue one objective and return its durable identity immediately."""
    try:
        return ExecutionResponse(execution=await service.submit(request))
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.post("/{execution_id}/cancel", response_model=ExecutionResponse)
async def cancel_execution(
    execution_id: str,
    service: ExecutionService = service_dependency,
) -> ExecutionResponse:
    """Cancel one in-process Execution and return its durable terminal state."""
    record = await service.cancel(execution_id)
    if record is None:
        raise HTTPException(status_code=404, detail="execution not found")
    return ExecutionResponse(execution=record)


@router.get("/approvals", response_model=list[ApprovalRequest])
async def pending_approvals(service: ExecutionService = service_dependency) -> list[ApprovalRequest]:
    """List actions awaiting this workspace owner's explicit decision."""
    return list(await service.approvals())


@router.get("/{execution_id}/approvals", response_model=list[ApprovalRequest])
async def execution_approvals(
    execution_id: str, service: ExecutionService = service_dependency
) -> list[ApprovalRequest]:
    """Read the immutable action details saved with a paused Execution."""
    try:
        return list(await service.approvals(execution_id))
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.post("/{execution_id}/approvals/{interruption_id}", response_model=ExecutionResponse, status_code=202)
async def decide_approval(
    execution_id: str,
    interruption_id: str,
    request: ApprovalDecisionRequest,
    service: ExecutionService = service_dependency,
) -> ExecutionResponse:
    """Commit an approval or denial without accepting a replacement Tool result."""
    try:
        return ExecutionResponse(execution=await service.decide(execution_id, interruption_id, request.decision))
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ApprovalConflictError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.get("/{execution_id}", response_model=ExecutionResponse)
async def get_execution(
    execution_id: str,
    service: ExecutionService = service_dependency,
) -> ExecutionResponse:
    """Return one durable execution summary."""
    record = await asyncio.to_thread(service.get, execution_id)
    if record is None:
        raise HTTPException(status_code=404, detail="execution not found")
    return ExecutionResponse(execution=record)


@router.get("/{execution_id}/events", response_model=list[EventResponse])
async def events(
    execution_id: str,
    after_sequence: int = Query(default=0, ge=0),
    service: ExecutionService = service_dependency,
) -> list[EventResponse]:
    """Replay journal events strictly after the optional sequence cursor."""
    if await asyncio.to_thread(service.get, execution_id) is None:
        raise HTTPException(status_code=404, detail="execution not found")
    replay = await service.events(execution_id, after_sequence=after_sequence)
    return [EventResponse.from_event(event) for event in replay]
