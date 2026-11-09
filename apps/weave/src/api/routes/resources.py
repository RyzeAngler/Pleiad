from fastapi import APIRouter, Depends, Query
from pleiad.contracts import JsonObject

from pleiad_weave.api.dependencies import get_service
from pleiad_weave.application.service import ExecutionService
from pleiad_weave.capabilities.resources import ResourceDetail, ResourceKind, ResourceSummary
from pleiad_weave.infrastructure.storage.product import JSON_OBJECT

router = APIRouter(prefix="/v1")
service_dependency = Depends(get_service)


async def resource_values(service: ExecutionService, kind: ResourceKind) -> list[JsonObject]:
    """Project only host-approved resource fields from the current catalog."""
    catalog = await service.agents.resources()
    values = catalog.get(kind)
    if not isinstance(values, list):
        raise RuntimeError("The host resource catalog has no requested kind")
    projected: list[JsonObject] = []
    for value in values:
        if not isinstance(value, dict):
            raise RuntimeError("The host resource catalog contains an invalid descriptor")
        code = value.get("code")
        name = value.get("name")
        if not isinstance(code, str) or not isinstance(name, str):
            raise RuntimeError("Resource descriptors require a code and name")
        fields: JsonObject = {
            "kind": kind,
            "code": code,
            "name": name,
        }
        for key in ("description", "available", "disabled_reason", "configuration_location"):
            if key in value:
                fields[key] = value[key]
        projected.append(ResourceSummary.model_validate(fields).model_dump(mode="json"))
    return projected


@router.get("/resources")
async def resources(
    kind: ResourceKind = "tools",
    query: str = "",
    cursor: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=100),
    service: ExecutionService = service_dependency,
) -> JsonObject:
    """Read the current resource catalog without starting a model or external action."""
    values = [
        value
        for value in await resource_values(service, kind)
        if query.casefold() in f"{value['name']} {value['description']} {value['code']}".casefold()
    ]
    return JSON_OBJECT.validate_python(
        {
            "items": values[cursor : cursor + limit],
            "total": len(values),
            "next_cursor": cursor + limit if cursor + limit < len(values) else None,
        }
    )


@router.get("/resources/{kind}/{code:path}")
async def resource(kind: ResourceKind, code: str, service: ExecutionService = service_dependency) -> ResourceDetail:
    """Read safe resource details through an explicit catalog identity."""
    return await service.agents.resource_detail(kind, code)


@router.get("/settings")
async def settings(service: ExecutionService = service_dependency) -> JsonObject:
    """Return a read-only host summary without credentials or absolute storage paths."""
    return JSON_OBJECT.validate_python(
        {
            "configuration_locations": [".env", "capabilities.yaml", "mcp.json"],
            "models": [
                {
                    "role": role,
                    "model": model.model,
                    "provider": model.provider,
                    "credentials_configured": bool(model.api_key.get_secret_value()),
                }
                for role, model in service.agents.models.items()
            ],
            "approval_tool_codes": sorted(service.agents.approval_tool_codes),
            "resource_bindings": ["workspace"],
        }
    )
