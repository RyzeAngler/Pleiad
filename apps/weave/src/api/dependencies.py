"""Dependency ports for HTTP adapters."""

from fastapi import Request

from pleiad_weave.application.service import ExecutionService


def require_service(service: ExecutionService | None) -> ExecutionService:
    """Return a configured service or fail at composition time."""
    if service is None:
        raise RuntimeError("Pleiad Weave application service is not configured")
    return service


def get_service(request: Request) -> ExecutionService:
    """Resolve the application service installed by ``create_app``."""
    return require_service(getattr(request.app.state, "service", None))
