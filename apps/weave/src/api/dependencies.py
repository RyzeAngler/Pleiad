from fastapi import HTTPException, Request

from pleiad_weave.application.service import ExecutionService


def get_service(request: Request) -> ExecutionService:
    """Read the shared application owner installed at composition time."""
    value: object = getattr(request.app.state, "service", None)
    if not isinstance(value, ExecutionService):
        raise RuntimeError("The application service has not been configured")
    return value


def csrf_token(request: Request) -> str:
    """Read the HTTP-only credential kept outside all model-accessible roots."""
    value: object = getattr(request.app.state, "csrf_token", None)
    if not isinstance(value, str):
        raise RuntimeError("The browser request credential has not been configured")
    return value


def get_writable_service(request: Request) -> ExecutionService:
    """Require the live workspace owner before accepting a browser mutation."""
    service = get_service(request)
    if not service.ready:
        raise HTTPException(status_code=503, detail="The workspace owner is not ready to accept changes")
    return service
