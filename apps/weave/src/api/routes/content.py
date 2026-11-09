import asyncio
from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from pleiad.contracts import JsonObject
from starlette.datastructures import UploadFile

from pleiad_weave.api.dependencies import get_service, get_writable_service
from pleiad_weave.api.models import RequestIdentity
from pleiad_weave.api.security import MAX_ATTACHMENT_BYTES, UploadTooLargeError
from pleiad_weave.application.service import ExecutionService
from pleiad_weave.infrastructure.storage.product_details import TextPage
from pleiad_weave.infrastructure.storage.product_models import Artifact, Attachment

router = APIRouter(prefix="/v1")
service_dependency = Depends(get_service)
write_dependency = Depends(get_writable_service)


def metadata(value: Artifact | Attachment) -> JsonObject:
    """Project approved file metadata without revealing owned storage paths."""
    return value.model_dump(mode="json", exclude={"relative_path"})


def content_response(
    payload: bytes, name: str, mime: str, digest: str, disposition: Literal["inline", "attachment"]
) -> Response:
    """Serve checked bytes with a bounded content policy and immutable digest."""
    return Response(
        payload,
        media_type=mime,
        headers={
            "Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(name, safe='')}",
            "ETag": f'"{digest}"',
            "Cache-Control": "private, no-cache",
            "Content-Security-Policy": "sandbox; default-src 'none'",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post("/attachments", status_code=201)
async def upload(request: Request, service: ExecutionService = write_dependency) -> JsonObject:
    """Validate streamed multipart bytes before creating a ready attachment reference."""
    try:
        async with request.form(max_files=1, max_fields=1, max_part_size=MAX_ATTACHMENT_BYTES) as form:
            identity = form.get("client_attachment_id")
            file = form.get("file")
            if not isinstance(identity, str) or not identity.strip() or not isinstance(file, UploadFile):
                raise HTTPException(status_code=422, detail="One client_attachment_id and one file are required")
            if file.filename is None or file.content_type is None:
                raise HTTPException(status_code=422, detail="The attachment name and MIME type are required")
            payload = bytearray()
            while chunk := await file.read(64 * 1024):
                payload.extend(chunk)
                if len(payload) > MAX_ATTACHMENT_BYTES:
                    raise UploadTooLargeError("The attachment exceeds the 25 MiB file limit")
            value = await asyncio.to_thread(
                service.content.upload_attachment,
                client_attachment_id=identity,
                name=file.filename,
                mime=file.content_type,
                payload=bytes(payload),
            )
    except UploadTooLargeError as error:
        raise HTTPException(status_code=413, detail=str(error)) from error
    service.metrics.content_bytes.record(len(payload), {"kind": "attachment", "direction": "upload"})
    return metadata(value)


@router.get("/attachments/{attachment_id}")
async def attachment(attachment_id: str, service: ExecutionService = service_dependency) -> JsonObject:
    """Read current attachment metadata without exposing its storage address."""
    value = await asyncio.to_thread(service.store.get_attachment, attachment_id)
    if value is None:
        raise LookupError("The attachment does not exist")
    return metadata(value)


@router.get("/attachments/{attachment_id}/content")
async def attachment_content(
    attachment_id: str,
    disposition: Literal["inline", "attachment"] = "attachment",
    service: ExecutionService = service_dependency,
) -> Response:
    """Return original attachment bytes after content and lifecycle validation."""
    value = await asyncio.to_thread(service.store.get_attachment, attachment_id)
    if value is None:
        raise LookupError("The attachment does not exist")
    payload = await asyncio.to_thread(service.content.read_attachment, attachment_id)
    service.metrics.content_bytes.record(len(payload), {"kind": "attachment", "direction": "download"})
    return content_response(payload, value.name, value.mime, value.digest, disposition)


@router.delete("/attachments/{attachment_id}")
async def delete_attachment(
    attachment_id: str, request: RequestIdentity, service: ExecutionService = write_dependency
) -> JsonObject:
    """Revoke only a temporary attachment that no committed message references."""
    return metadata(
        await asyncio.to_thread(service.content.delete_attachment, attachment_id, request_id=request.request_id)
    )


@router.get("/attachments/{attachment_id}/text")
async def attachment_text(
    attachment_id: str,
    offset: int = Query(default=0, ge=0),
    max_bytes: int = Query(default=8192, ge=1, le=32768),
    service: ExecutionService = service_dependency,
) -> TextPage:
    """Read an original attachment through bounded, integrity-checked UTF-8 pages."""
    page = await asyncio.to_thread(
        service.content.content_text_page, "attachment", attachment_id, offset=offset, max_bytes=max_bytes
    )
    service.metrics.content_bytes.record(len(page.text.encode("utf-8")), {"kind": "attachment", "direction": "preview"})
    return page


@router.get("/artifacts/{artifact_id}")
async def artifact(artifact_id: str, service: ExecutionService = service_dependency) -> JsonObject:
    """Read artifact ownership and actual preview and download capabilities."""
    value = await asyncio.to_thread(service.store.get_artifact, artifact_id)
    if value is None:
        raise LookupError("The artifact does not exist")
    return metadata(value)


@router.get("/artifacts/{artifact_id}/content")
async def artifact_content(
    artifact_id: str,
    disposition: Literal["inline", "attachment"] = "attachment",
    service: ExecutionService = service_dependency,
) -> Response:
    """Read an owned artifact only after its original digest and size are checked."""
    value = await asyncio.to_thread(service.store.get_artifact, artifact_id)
    if value is None:
        raise LookupError("The artifact does not exist")
    if disposition == "inline" and not value.previewable:
        raise HTTPException(status_code=422, detail="This artifact supports download only")
    if disposition == "attachment" and not value.downloadable:
        raise HTTPException(status_code=422, detail="This artifact is unavailable for download")
    payload = await asyncio.to_thread(service.content.read_artifact, artifact_id)
    service.metrics.content_bytes.record(len(payload), {"kind": "artifact", "direction": "download"})
    return content_response(payload, value.name, value.mime, value.digest, disposition)


@router.get("/artifacts/{artifact_id}/text")
async def artifact_text(
    artifact_id: str,
    offset: int = Query(default=0, ge=0),
    max_bytes: int = Query(default=8192, ge=1, le=32768),
    service: ExecutionService = service_dependency,
) -> TextPage:
    """Read original artifact text through the same bounded preview contract."""
    page = await asyncio.to_thread(
        service.content.content_text_page, "artifact", artifact_id, offset=offset, max_bytes=max_bytes
    )
    service.metrics.content_bytes.record(len(page.text.encode("utf-8")), {"kind": "artifact", "direction": "preview"})
    return page
