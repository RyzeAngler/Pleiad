import hashlib
import os
from pathlib import Path
from typing import Annotated, Literal, Self

from dotenv import dotenv_values
from fastmcp import Client
from fastmcp.client.auth import OAuth
from fastmcp.client.transports import ClientTransport, StreamableHttpTransport
from key_value.aio.stores.filetree import FileTreeStore
from pleiad.contracts import ContractModel
from pleiad.mcp.contracts import McpServer
from pleiad.mcp.stdio import IsolatedStdioTransport
from pydantic import AnyHttpUrl, Field, TypeAdapter, model_validator

HTTP_URL = TypeAdapter(AnyHttpUrl)
EnvironmentName = Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9_]*$")]
HeaderName = Annotated[str, Field(pattern=r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")]


class McpConnection(ContractModel):
    """Configure one host-owned HTTP or stdio connection with host credentials."""

    code: str = Field(pattern=r"^[a-z][a-z0-9_-]*$")
    enabled: bool = False
    url: str | None = None
    url_env: EnvironmentName | None = None
    command: str | None = Field(default=None, min_length=1, pattern=r"^\S(?:.*\S)?$")
    args: tuple[str, ...] = ()
    env: dict[EnvironmentName, str] = Field(default_factory=dict)
    env_from: dict[EnvironmentName, EnvironmentName] = Field(default_factory=dict)
    auth: Literal["none", "bearer", "oauth"] = "none"
    token_env: EnvironmentName | None = None
    headers: dict[HeaderName, str] = Field(default_factory=dict)
    header_env: dict[HeaderName, EnvironmentName] = Field(default_factory=dict)
    allowed_tools: tuple[str, ...] | None = None
    timeout_seconds: float = Field(default=60, gt=0, le=3600)

    @model_validator(mode="after")
    def validate_binding(self) -> Self:
        """Require one transport and reject settings for a different transport."""
        if sum(value is not None for value in (self.url, self.url_env, self.command)) != 1:
            raise ValueError("MCP connection requires exactly one of url, url_env, and command")
        if self.url is not None:
            HTTP_URL.validate_python(self.url)
        if (self.auth == "bearer") != (self.token_env is not None):
            raise ValueError("MCP bearer authentication requires token_env; other modes must omit it")
        if self.command is not None:
            if self.auth != "none" or self.headers or self.header_env:
                raise ValueError("MCP stdio connections must omit HTTP authentication and headers")
        elif self.args or self.env or self.env_from:
            raise ValueError("MCP HTTP connections must omit stdio args, env, and env_from")
        if self.env.keys() & self.env_from.keys():
            raise ValueError("MCP env and env_from must not configure the same subprocess variable")
        return self

    @model_validator(mode="after")
    def validate_headers(self) -> Self:
        """Reject duplicate or embedded authorization headers."""
        names = [name.lower() for name in (*self.headers, *self.header_env)]
        if "authorization" in names:
            raise ValueError("Use token_env or OAuth instead of storing Authorization in mcp.json")
        if len(names) != len(set(names)):
            raise ValueError("MCP header names must be unique, ignoring case")
        return self

    @model_validator(mode="after")
    def validate_allowed_tools(self) -> Self:
        """Reject ambiguous or empty tool names in an explicit allowlist."""
        if self.allowed_tools is not None:
            if any(not name.strip() or name != name.strip() for name in self.allowed_tools):
                raise ValueError("MCP allowed_tools must contain non-empty remote tool names without padding")
            if len(self.allowed_tools) != len(set(self.allowed_tools)):
                raise ValueError("MCP allowed_tools must not contain duplicate remote tool names")
        return self


class McpConfiguration(ContractModel):
    """Validate the optional host-owned connection file."""

    servers: tuple[McpConnection, ...] = ()

    @model_validator(mode="after")
    def validate_codes(self) -> Self:
        """Reject ambiguous connection identities, including disabled entries."""
        codes = [server.code for server in self.servers]
        if len(codes) != len(set(codes)):
            raise ValueError("MCP server codes must be unique")
        return self


class WorkspaceOAuth(OAuth):
    """Keep workspace-specific OAuth state on the host and consent in the CLI."""

    def __init__(self, url: str, root: Path, *, interactive: bool) -> None:
        """Bind a private persistent token directory and an explicit login policy."""
        self.interactive = interactive
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        super().__init__(url, client_name="Pleiad Weave", token_storage=FileTreeStore(data_directory=root))

    async def redirect_handler(self, authorization_url: str) -> None:
        """Reject background consent requests while allowing token refresh.

        Raises:
            ValueError: If interactive consent is needed outside the login CLI.
        """
        if not self.interactive:
            raise ValueError("MCP authorization is required; run pleiad mcp-login <server-code> in a terminal")
        await super().redirect_handler(authorization_url)


def load_mcp_configuration(workspace: Path) -> McpConfiguration:
    """Read host MCP configuration outside the model-writable workspace."""
    values = host_environment()
    configured = values.get("PLEIAD_MCP_CONFIG_FILE", "mcp.json")
    if configured is None or not configured.strip():
        raise ValueError("PLEIAD_MCP_CONFIG_FILE must name a host configuration file")
    path = Path(configured).expanduser().resolve()
    if path.is_relative_to(workspace.resolve()):
        raise ValueError("MCP configuration must be outside the tool-visible workspace")
    return McpConfiguration.model_validate_json(path.read_bytes()) if path.exists() else McpConfiguration()


def host_environment() -> dict[str, str | None]:
    """Read host .env values with process overrides without modifying os.environ."""
    values = dict(dotenv_values(".env", interpolate=False))
    values.update(os.environ)
    return values


def required_environment(name: str, values: dict[str, str | None]) -> str:
    """Resolve a required host variable without including its value in errors."""
    value = values.get(name)
    if value is None or not value.strip():
        raise ValueError(f"MCP configuration requires host environment variable {name}")
    return value


def create_mcp_server(
    connection: McpConnection, workspace: Path, *, interactive: bool = False
) -> McpServer[ClientTransport]:
    """Build a disconnected SDK binding using .env with process overrides."""
    values = host_environment()
    transport: ClientTransport
    if connection.command is not None:
        environment = dict(connection.env)
        environment.update({name: required_environment(source, values) for name, source in connection.env_from.items()})
        transport = IsolatedStdioTransport(connection.command, connection.args, env=environment)
    else:
        transport = create_http_transport(connection, workspace, values, interactive=interactive)
    return McpServer(
        connection.code,
        Client(transport, timeout=connection.timeout_seconds, init_timeout=connection.timeout_seconds),
    )


def create_http_transport(
    connection: McpConnection,
    workspace: Path,
    values: dict[str, str | None],
    *,
    interactive: bool,
) -> StreamableHttpTransport:
    """Resolve HTTP headers and authentication without placing secrets in descriptors."""
    endpoint = connection.url
    if connection.url_env is not None:
        endpoint = required_environment(connection.url_env, values)
    url = str(HTTP_URL.validate_python(endpoint))
    auth: str | WorkspaceOAuth | None = None
    if connection.token_env is not None:
        auth = required_environment(connection.token_env, values)
    elif connection.auth == "oauth":
        directory = values.get("PLEIAD_MCP_AUTH_DIRECTORY", ".pleiad-workspace/mcp")
        if directory is None or not directory.strip():
            raise ValueError("PLEIAD_MCP_AUTH_DIRECTORY must name a host directory")
        root = Path(directory).expanduser().resolve()
        if root.is_relative_to(workspace.resolve()):
            raise ValueError("MCP OAuth credentials must be outside the tool-visible workspace")
        workspace_key = hashlib.sha256(str(workspace.resolve()).encode()).hexdigest()
        auth = WorkspaceOAuth(url, root / workspace_key / connection.code, interactive=interactive)
    headers = dict(connection.headers)
    headers.update({name: required_environment(source, values) for name, source in connection.header_env.items()})
    return StreamableHttpTransport(url, headers=headers, auth=auth)
