import re
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pleiad.contracts import ContractModel, JsonObject, JsonValue
from pleiad.mcp.tool import McpTool
from pleiad.models.anthropic.client import AnthropicModelClient
from pleiad.models.binding import credential_field, credential_values
from pleiad.models.openai.client import OpenAIModelClient
from pleiad.sandbox.policy import SandboxPolicy
from pleiad.sandbox.sandbox import Sandbox
from pleiad.skills.loader import SkillLoader
from pleiad.skills.manifest import load_skill_descriptor
from pleiad.tools.base import BaseTool
from pleiad.tools.builtins.command import CommandTool
from pleiad.tools.builtins.file_support import FileToolBinding
from pleiad.tools.builtins.read_file import ReadFileTool
from pleiad.tools.builtins.write_file import WriteFileTool
from pydantic import Field

from pleiad_weave.capabilities.mcp import McpConnection
from pleiad_weave.capabilities.tools import ScriptTool

MASK = "[REDACTED]"
URL_PATTERN = re.compile(r"[a-z][a-z0-9+.-]*://[^\s<>\"'`]+", re.IGNORECASE)
ASSIGNMENT_PATTERN = re.compile(
    r"(?i)([\w.-]*(?:api[_-]?key|token|password|passwd|secret|authorization|credential|cookie)|key|ak)"
    r"([\"']?\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|(?:bearer|basic)\s+[^\s,;&]+|[^\s,;&]+)"
)
BEARER_PATTERN = re.compile(r"(?i)\b(bearer|basic)\s+[^\s,;\"']+")
PRIVATE_KEY_PATTERN = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL)
type ResourceKind = Literal["models", "tools", "skills", "environments"]


class ResourceSummary(ContractModel):
    """Describe one currently bound resource in the user-facing catalog."""

    kind: ResourceKind
    code: str
    name: str
    description: str | None = None
    available: bool = True
    disabled_reason: str | None = None
    configuration_location: str | None = None


class ResourceDetail(ResourceSummary):
    """Expose complete selected resource data after credential redaction."""

    metadata: JsonObject = Field(default_factory=dict)
    configuration: JsonObject = Field(default_factory=dict)
    input_schema: JsonObject | None = None
    output_schema: JsonObject | None = None
    content: str | None = None
    files: tuple[str, ...] = ()


def redact_url(value: str) -> str:
    """Retain URL routing information while masking credentials and signed query values."""
    parsed = urlsplit(value)
    authority = parsed.netloc
    if "@" in authority:
        authority = f"{MASK}@{authority.rsplit('@', 1)[1]}"
    query = urlencode(
        [
            (key, MASK if credential_field(key) or key.casefold() in {"sig", "signature", "x-amz-signature"} else item)
            for key, item in parse_qsl(parsed.query, keep_blank_values=True)
        ],
        safe="[]",
    )
    fragment = ASSIGNMENT_PATTERN.sub(lambda match: f"{match.group(1)}{match.group(2)}{MASK}", parsed.fragment)
    return urlunsplit((parsed.scheme, authority, parsed.path, query, fragment))


def redact_text(value: str, *, secrets: tuple[str, ...] = ()) -> str:
    """Mask credential assignments and authenticated URLs in free-form resource text."""
    for secret in secrets:
        value = value.replace(secret, MASK)
    value = PRIVATE_KEY_PATTERN.sub(MASK, value)
    value = URL_PATTERN.sub(lambda match: redact_url(match.group()), value)
    value = ASSIGNMENT_PATTERN.sub(lambda match: f"{match.group(1)}{match.group(2)}{MASK}", value)
    return BEARER_PATTERN.sub(lambda match: f"{match.group(1)} {MASK}", value)


def redact_value(value: JsonValue, *, secrets: tuple[str, ...] = ()) -> JsonValue:
    """Recursively mask actual configuration values while retaining their field names."""
    if isinstance(value, dict):
        return {
            key: MASK if credential_field(key) and item is not None else redact_value(item, secrets=secrets)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_value(item, secrets=secrets) for item in value]
    if isinstance(value, str):
        return redact_text(value, secrets=secrets)
    return value


def redact_schema(schema: JsonObject, *, sensitive_property: bool = False, secrets: tuple[str, ...] = ()) -> JsonObject:
    """Preserve schema definitions, masking only credential literals and examples."""
    result: JsonObject = {}
    for key, value in schema.items():
        if key in {"default", "examples", "example", "const", "enum"}:
            result[key] = MASK if sensitive_property and value is not None else redact_value(value, secrets=secrets)
        elif key in {"properties", "$defs", "definitions", "patternProperties"} and isinstance(value, dict):
            result[key] = {
                name: redact_schema(item, sensitive_property=credential_field(name), secrets=secrets)
                if isinstance(item, dict)
                else item
                for name, item in value.items()
            }
        elif isinstance(value, dict):
            result[key] = redact_schema(value, sensitive_property=sensitive_property, secrets=secrets)
        elif isinstance(value, list):
            result[key] = [
                redact_schema(item, sensitive_property=sensitive_property, secrets=secrets)
                if isinstance(item, dict)
                else redact_text(item, secrets=secrets)
                if isinstance(item, str)
                else item
                for item in value
            ]
        else:
            result[key] = redact_text(value, secrets=secrets) if isinstance(value, str) else value
    return result


def safe_detail(detail: ResourceDetail, *, secrets: tuple[str, ...] = ()) -> ResourceDetail:
    """Sanitize all browser-visible values before returning the detail contract."""
    values = detail.model_dump(mode="json")
    schemas = {key: values.pop(key) for key in ("input_schema", "output_schema")}
    credentials = (*secrets, *credential_values(values))
    redacted = redact_value(values, secrets=credentials)
    if not isinstance(redacted, dict):
        raise TypeError("Resource details must serialize to an object")
    return ResourceDetail.model_validate(
        {
            **redacted,
            **{
                key: None if schema is None else redact_schema(schema, secrets=credentials)
                for key, schema in schemas.items()
            },
        }
    )


def model_credentials(model: OpenAIModelClient | AnthropicModelClient) -> tuple[str, ...]:
    """Collect configured credentials for masking aliases in descriptive resource text."""
    key = model.api_key.get_secret_value()
    return (
        *((key,) if key else ()),
        *credential_values({"headers": dict(model.headers), "provider_options": model.provider_options}),
    )


def model_detail(
    role: str, model: OpenAIModelClient | AnthropicModelClient, *, secrets: tuple[str, ...] = ()
) -> ResourceDetail:
    """Read the configured public model client without contacting its provider."""
    return safe_detail(
        ResourceDetail(
            kind="models",
            code=role,
            name=model.model,
            configuration_location=".env",
            metadata={"role": role},
            configuration={
                "role": role,
                "protocol": "openai" if isinstance(model, OpenAIModelClient) else "anthropic",
                "provider": model.provider,
                "model": model.model,
                "base_url": model.base_url,
                "timeout_seconds": model.timeout_seconds,
                "max_retries": model.max_retries,
                "provider_options": model.provider_options,
                "headers": dict(model.headers),
                "api_key": MASK if model.api_key.get_secret_value() else None,
            },
        ),
        secrets=(*secrets, *model_credentials(model)),
    )


def file_binding(binding: FileToolBinding) -> JsonObject:
    """Describe the actual file scopes and limits supplied to a bound SDK Tool."""
    return {
        "workspace": str(binding.workspace),
        "scratch": str(binding.scratch),
        "skill_roots": {code: str(path) for code, path in binding.skill_roots.items()},
        "max_read_bytes": binding.max_read_bytes,
        "max_write_bytes": binding.max_write_bytes,
        "max_lines": binding.max_lines,
        "excluded_workspace_paths": list(binding.excluded_workspace_paths),
    }


def tool_detail(
    tool: BaseTool,
    approval_tool_codes: frozenset[str],
    connection: McpConnection | None,
    *,
    secrets: tuple[str, ...] = (),
) -> ResourceDetail:
    """Project real Tool contracts and host bindings without executing the Tool."""
    configuration: JsonObject = {
        "policy": tool.policy.model_dump(mode="json"),
        "required_permissions": [*sorted(tool.required_permissions)],
        "approval_required": tool.descriptor.code in approval_tool_codes,
        "supports_background": tool.supports_background,
    }
    output_schema: JsonObject | None = None
    location: str | None = None
    if isinstance(tool, ScriptTool):
        configuration["binding"] = {
            "type": "workspace_script",
            "script": tool.binding.script,
            "source_root": str(tool.source_root),
            "files": file_binding(tool.files),
            "sandbox_policy": tool.sandbox_policy.model_dump(mode="json"),
        }
        output_schema = tool.binding.output_schema
        location = "capabilities.yaml"
    elif isinstance(tool, McpTool):
        if connection is None:
            raise ValueError("MCP Tool details require the current host connection")
        configuration["binding"] = {
            "type": "mcp",
            "server_code": tool.server.code,
            "remote_name": tool.info.remote_name,
            "connection": connection.model_dump(mode="json"),
        }
        output_schema = tool.info.output_schema
        location = "mcp.json"
    elif isinstance(tool, ReadFileTool | WriteFileTool | CommandTool):
        configuration["binding"] = {"type": "builtin", "files": file_binding(tool.binding)}
        if isinstance(tool, CommandTool):
            configuration["sandbox_policy"] = tool.sandbox_policy.model_dump(mode="json")
    return safe_detail(
        ResourceDetail(
            kind="tools",
            code=tool.descriptor.code,
            name=tool.descriptor.name,
            description=tool.descriptor.description,
            configuration_location=location,
            metadata=tool.descriptor.model_dump(mode="json"),
            configuration=configuration,
            input_schema=tool.input_schema,
            output_schema=output_schema,
        ),
        secrets=secrets,
    )


def skill_detail(loader: SkillLoader, code: str, *, secrets: tuple[str, ...] = ()) -> ResourceDetail:
    """Load a selected Skill's full source and inventory through its validated package."""
    skill = loader.load(code)
    package = loader.package_root(code)
    return safe_detail(
        ResourceDetail(
            kind="skills",
            code=code,
            name=skill.descriptor.name,
            description=skill.descriptor.description,
            configuration_location=f"skills/{code}/SKILL.md",
            metadata={
                **skill.descriptor.model_dump(mode="json"),
                "package": load_skill_descriptor(package / "SKILL.md").model_dump(mode="json"),
            },
            configuration={
                "root": str(package),
                "content_digest": skill.content_digest,
                "references": list(skill.references),
            },
            content=loader.read_file(code, "SKILL.md").decode("utf-8"),
            files=tuple(sorted(path.relative_to(package).as_posix() for path in package.rglob("*") if path.is_file())),
        ),
        secrets=secrets,
    )


def environment_detail(
    workspace: Path, sandbox: Sandbox, policy: SandboxPolicy, *, secrets: tuple[str, ...] = ()
) -> ResourceDetail:
    """Describe the actual workspace binding and configured execution limits."""
    return safe_detail(
        ResourceDetail(
            kind="environments",
            code="workspace",
            name="Local workspace",
            configuration_location=".env",
            metadata={"host_resource_id": "workspace"},
            configuration={
                "workspace": str(workspace),
                "backend": sandbox.backend.runtime,
                "sandbox_policy": policy.model_dump(mode="json"),
            },
        ),
        secrets=secrets,
    )
