from collections.abc import Awaitable, Callable
from secrets import compare_digest
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024
MAX_UPLOAD_REQUEST_BYTES = MAX_ATTACHMENT_BYTES + 64 * 1024


class UploadTooLargeError(ValueError):
    """Stop reading an upload as soon as its bounded request budget is exhausted."""


def error_response(status: int, code: str, message: str, request_id: str | None = None) -> JSONResponse:
    """Return the common safe error envelope for an HTTP boundary rejection."""
    if request_id is None:
        request_id = uuid4().hex
    return JSONResponse(
        status_code=status,
        content={"code": code, "message": message, "request_id": request_id, "retryable": status in {429, 503}},
    )


async def guard_request(request: Request, call_next: Callable[[Request], Awaitable[Response]], token: str) -> Response:
    """Check local ownership and same-origin browser mutations before routing."""
    try:
        authority = urlsplit(f"//{request.headers.get('host', '')}")
        target_port = authority.port if authority.port is not None else 443 if request.url.scheme == "https" else 80
        if authority.hostname not in {"localhost", "127.0.0.1", "::1"} or authority.username is not None:
            return error_response(403, "http.host_rejected", "The service accepts local Host identities only")
        origin = request.headers.get("origin")
        if origin is not None:
            source = urlsplit(origin)
            source_port = source.port if source.port is not None else 443 if source.scheme == "https" else 80
            if (
                source.scheme != request.url.scheme
                or source.hostname != authority.hostname
                or source_port != target_port
                or source.path not in {"", "/"}
                or source.query
                or source.fragment
                or source.username is not None
            ):
                return error_response(403, "http.origin_rejected", "The request Origin does not match this service")
    except ValueError:
        return error_response(400, "http.invalid_origin", "The request Host or Origin is invalid")
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        credential = request.headers.get("x-weave-csrf")
        if credential is None or not compare_digest(credential, token):
            return error_response(
                403, "http.csrf_rejected", "The current browser connection must be verified before submitting changes"
            )
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    return response


class UploadLimitMiddleware:
    """Bound raw multipart bytes before the parser can spool an oversized file."""

    def __init__(self, app: ASGIApp) -> None:
        """Wrap the application's incoming upload stream."""
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Count actual multipart bytes independently of Content-Length claims."""
        if scope["type"] != "http" or scope["path"] != "/v1/attachments" or scope["method"] != "POST":
            await self.app(scope, receive, send)
            return
        total = 0

        async def limited_receive() -> Message:
            nonlocal total
            message = await receive()
            if message["type"] == "http.request":
                total += len(message.get("body", b""))
                if total > MAX_UPLOAD_REQUEST_BYTES:
                    raise UploadTooLargeError("The attachment exceeds its upload limit")
            return message

        await self.app(scope, limited_receive, send)
