import asyncio
import logging
import re
from collections.abc import Mapping

from fastmcp.client.transports import ClientTransport
from jsonschema import SchemaError
from jsonschema.validators import validator_for
from mcp_types import Tool as RemoteTool

from pleiad.contracts import JsonSchema
from pleiad.mcp.contracts import McpServer, McpToolInfo
from pleiad.mcp.errors import McpDiscoveryFault, McpToolSchemaFault
from pleiad.mcp.tool import McpTool
from pleiad.tools.contracts import ToolDescriptor

logger = logging.getLogger(__name__)


async def discover_tools[TransportT: ClientTransport](
    server: McpServer[TransportT],
) -> tuple[McpTool[TransportT], ...]:
    """Discover all Tools through one short-lived FastMCP connection.

    Args:
        server: Server identity and disconnected client prototype.

    Returns:
        Executable Pleiad Tools bound to fresh per-call connections.

    Raises:
        McpDiscoveryFault: If connection or Tool listing fails.
        McpToolSchemaFault: If a remote Tool contract is invalid or ambiguous.
    """
    logger.info(f"MCP Tool discovery started server={server.code}")
    try:
        async with server.client.new() as client:
            remote_tools = await client.list_tools()
    except asyncio.CancelledError:
        raise
    except Exception as error:
        logger.error(f"MCP Tool discovery failed server={server.code}", exc_info=True)
        raise McpDiscoveryFault() from error

    try:
        tools = tuple(McpTool(server, tool_info(server.code, remote)) for remote in remote_tools)
        names = [tool.descriptor.name for tool in tools]
        if len(names) != len(set(names)):
            raise ValueError("MCP Tool names must be unique after namespacing")
    except (SchemaError, TypeError, ValueError) as error:
        logger.error(f"MCP Tool contract rejected server={server.code}", exc_info=True)
        raise McpToolSchemaFault() from error

    logger.info(f"MCP Tool discovery completed server={server.code} tools={len(tools)}")
    return tools


def tool_info(server_code: str, remote: RemoteTool) -> McpToolInfo:
    """Project one remote MCP Tool into a validated Pleiad contract."""
    remote_name = remote.name.strip()
    if not remote_name:
        raise ValueError("MCP Tool name must not be empty")
    if remote.execution is not None and remote.execution.task_support == "required":
        raise ValueError("MCP task-only Tools are not supported")
    model_name = f"{server_code}__{remote_name}"
    if re.fullmatch(r"[A-Za-z0-9_-]+", model_name) is None:
        raise ValueError("MCP Tool name is not compatible with model Tool naming")
    input_schema = schema(remote.input_schema, "input")
    output_schema = None if remote.output_schema is None else schema(remote.output_schema, "output")
    description = remote.description.strip() if remote.description and remote.description.strip() else None
    if description is None and remote.title is not None and remote.title.strip():
        description = remote.title.strip()
    return McpToolInfo(
        remote_name=remote_name,
        descriptor=ToolDescriptor(
            name=model_name,
            code=f"mcp.{server_code}.{remote_name}",
            description=description if description is not None else remote_name,
        ),
        input_schema=input_schema,
        output_schema=output_schema,
    )


def schema(value: object, label: str) -> JsonSchema:
    """Validate one remote JSON Schema while preserving its declared dialect."""
    if not isinstance(value, Mapping):
        raise TypeError(f"MCP Tool {label} schema must be an object")
    result: JsonSchema = {str(key): item for key, item in value.items()}
    validator_for(result).check_schema(result)
    return result
