import asyncio

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import StreamingResponse
from pleiad.errors.base import SystemFault

from pleiad_weave.api.dependencies import get_service
from pleiad_weave.application.service import ExecutionService
from pleiad_weave.application.web import (
    ConversationMessage,
    ExecutionDetails,
    conversation_messages,
    execution_details,
    stream_execution_events,
)
from pleiad_weave.infrastructure.storage.models import ExecutionRecord

router = APIRouter(prefix="/v1")
service_dependency = Depends(get_service)


@router.get("/executions", response_model=list[ExecutionRecord])
async def list_executions(
    conversation_id: str | None = Query(default=None, min_length=1),
    service: ExecutionService = service_dependency,
) -> list[ExecutionRecord]:
    """Read durable job history, optionally restricted to one conversation."""
    return list(await asyncio.to_thread(service.repository.list_records, conversation_id))


@router.get("/executions/{execution_id}/details", response_model=ExecutionDetails)
async def get_execution_details(
    execution_id: str,
    service: ExecutionService = service_dependency,
) -> ExecutionDetails:
    """Read execution-owned graph definitions, node attempts, and JSON outputs."""
    try:
        return await asyncio.to_thread(execution_details, service, execution_id)
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except SystemFault as error:
        raise HTTPException(status_code=500, detail=error.message) from error


@router.get("/conversations/{conversation_id}/messages", response_model=list[ConversationMessage])
async def get_conversation_messages(
    conversation_id: str,
    service: ExecutionService = service_dependency,
) -> list[ConversationMessage]:
    """Read ordered main-conversation messages through public SDK memory APIs."""
    try:
        return list(await conversation_messages(service, conversation_id))
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.get("/executions/{execution_id}/stream")
async def execution_stream(
    execution_id: str,
    after_sequence: int = Query(default=0, ge=0),
    last_event_id: str | None = Header(default=None),
    service: ExecutionService = service_dependency,
) -> StreamingResponse:
    """Stream live SDK progress with durable replay across browser reconnects."""
    if await asyncio.to_thread(service.get, execution_id) is None:
        raise HTTPException(status_code=404, detail="execution not found")
    if last_event_id is not None:
        try:
            last_sequence = int(last_event_id)
        except ValueError as error:
            raise HTTPException(status_code=400, detail="Last-Event-ID must be a nonnegative integer") from error
        if last_sequence < 0:
            raise HTTPException(status_code=400, detail="Last-Event-ID must be a nonnegative integer")
        after_sequence = max(after_sequence, last_sequence)
    return StreamingResponse(
        stream_execution_events(service, execution_id, after_sequence),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
