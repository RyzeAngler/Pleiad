import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from fastmcp import Client
from fastmcp.client.transports import ClientTransport

from pleiad.contracts import JsonSchema
from pleiad.tools.contracts import ToolDescriptor


@dataclass(frozen=True, slots=True)
class McpServer[TransportT: ClientTransport]:
    """Describe one MCP server using a disconnected FastMCP client prototype."""

    code: str
    client: Client[TransportT]
    tool_permissions: Mapping[str, frozenset[str]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Validate stable server identity and ownership of connection state."""
        if re.fullmatch(r"[a-z][a-z0-9_-]*", self.code) is None:
            raise ValueError("MCP server code must be a lowercase identifier")
        if self.client.is_connected():
            raise ValueError("MCP server client must be disconnected")
        permissions: dict[str, frozenset[str]] = {}
        for name, required in self.tool_permissions.items():
            if not name.strip() or any(not permission.strip() for permission in required):
                raise ValueError("MCP Tool names and permission requirements must be non-empty")
            permissions[name] = frozenset(required)
        object.__setattr__(self, "tool_permissions", MappingProxyType(permissions))


@dataclass(frozen=True, slots=True)
class McpToolInfo:
    """Keep the remote identity and validated Pleiad contract of one MCP Tool."""

    remote_name: str
    descriptor: ToolDescriptor
    input_schema: JsonSchema
    output_schema: JsonSchema | None
