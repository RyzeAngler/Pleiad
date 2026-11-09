import asyncio
import json
from collections.abc import AsyncIterator
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pleiad.contracts import JsonObject

from pleiad_weave.api.dependencies import csrf_token, get_service, get_writable_service
from pleiad_weave.application.service import ExecutionService
from pleiad_weave.infrastructure.storage.product_models import (
    AttentionView,
    ChatMessageRequest,
    MessageReceipt,
    MessageView,
    Page,
    ResolutionReceipt,
    ResolutionRequest,
)

router = APIRouter(prefix="/v1")
service_dependency = Depends(get_service)
write_dependency = Depends(get_writable_service)


@router.get("/snapshot")
async def snapshot(
    request: Request, limit: int = Query(default=50, ge=1, le=100), service: ExecutionService = service_dependency
) -> JsonObject:
    """Read one consistent product snapshot and issue its browser-only credential."""
    value = await service.snapshot(limit=limit)
    capabilities = value.get("capabilities")
    if not isinstance(capabilities, dict):
        raise RuntimeError("The product snapshot has no capability contract")
    return {**value, "capabilities": {**capabilities, "csrf_token": csrf_token(request)}}


@router.get("/chat/messages")
async def messages(
    before_sequence: int | None = Query(default=None, ge=1),
    limit: int = Query(default=50, ge=1, le=100),
    service: ExecutionService = service_dependency,
) -> Page[MessageView]:
    """Read only formally committed permanent-chat messages."""
    return await asyncio.to_thread(service.store.messages, before_sequence=before_sequence, limit=limit)


@router.post("/chat/messages", status_code=202)
async def submit(request: ChatMessageRequest, service: ExecutionService = write_dependency) -> MessageReceipt:
    """Accept a complete chat submission atomically using its stable client key."""
    if not service.ready:
        raise HTTPException(status_code=503, detail="The workspace owner is not ready to accept work")
    return await service.submit(request)


@router.get("/attentions")
async def attentions(
    status: Literal["pending", "resolved", "expired", "withdrawn"] = "pending",
    cursor: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=100),
    service: ExecutionService = service_dependency,
) -> Page[AttentionView]:
    """Read bounded user-decision facts independently of running models."""
    return await asyncio.to_thread(service.store.attentions, status=status, cursor=cursor, limit=limit)


@router.get("/attentions/{attention_id}")
async def attention(attention_id: str, service: ExecutionService = service_dependency) -> JsonObject:
    """Read a current or already resolved Attention with truthful action availability."""
    value = await asyncio.to_thread(service.store.read_attention, attention_id)
    return value.model_dump(mode="json")


@router.post("/attentions/{attention_id}/resolve")
async def resolve(
    attention_id: str, request: ResolutionRequest, service: ExecutionService = write_dependency
) -> ResolutionReceipt:
    """Save one complete answer or explicit decision through the shared service."""
    return await service.resolve(attention_id, request)


async def product_events(service: ExecutionService, after_sequence: int, request: Request) -> AsyncIterator[str]:
    """Replay the committed journal while keeping browser lifetime independent."""
    cursor = after_sequence
    while not await request.is_disconnected():
        page = await asyncio.to_thread(service.store.events, after_sequence=cursor, limit=100)
        if cursor > page.read_sequence or page.total > 1000 or (page.items and page.items[0].sequence != cursor + 1):
            service.metrics.snapshot_resyncs.add(1)
            value = json.dumps({"snapshot_url": "/v1/snapshot", "snapshot_sequence": page.read_sequence})
            yield f"event: resync\ndata: {value}\n\n"
            return
        for event in page.items:
            yield f"id: {event.sequence}\nevent: product\ndata: {event.model_dump_json()}\n\n"
            cursor = event.sequence
        if not page.items:
            yield ": keep-alive\n\n"
            await asyncio.sleep(0.25)


@router.get("/events")
async def events(
    request: Request,
    after_sequence: int = Query(default=0, ge=0),
    last_event_id: str | None = Header(default=None),
    service: ExecutionService = service_dependency,
) -> StreamingResponse:
    """Subscribe to product facts using a global resumable event cursor."""
    if last_event_id is not None:
        try:
            cursor = int(last_event_id)
        except ValueError as error:
            raise HTTPException(status_code=400, detail="Last-Event-ID must be a nonnegative integer") from error
        if cursor < 0:
            raise HTTPException(status_code=400, detail="Last-Event-ID must be a nonnegative integer")
        after_sequence = max(after_sequence, cursor)
    return StreamingResponse(
        product_events(service, after_sequence, request),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )
