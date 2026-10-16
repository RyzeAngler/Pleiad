import asyncio
import logging
from collections.abc import Mapping, Sequence

from fastmcp.client.client import CallToolResult
from fastmcp.client.transports import ClientTransport

from pleiad.contracts import JsonObject, JsonSchema, JsonValue
from pleiad.mcp.contracts import McpServer, McpToolInfo
from pleiad.mcp.errors import McpToolCallFault, McpToolResultFault
from pleiad.observability.attributes import CALL_ID, MCP_CALL_NAME, TOOL_CODE
from pleiad.observability.tracing import Observability
from pleiad.tools.base import BaseTool, ToolFailure, ToolOutput
from pleiad.tools.contracts import ToolContext, ToolDescriptor, ToolPolicy
from pleiad.tools.errors import RemoteToolError

logger = logging.getLogger(__name__)


class McpTool[TransportT: ClientTransport](BaseTool):
    """Execute one discovered MCP Tool through a fresh FastMCP connection."""

    def __init__(
        self,
        server: McpServer[TransportT],
        info: McpToolInfo,
        policy: ToolPolicy | None = None,
        observability: Observability | None = None,
    ) -> None:
        """Bind immutable discovery data to a disconnected server client.

        Args:
            server: Server identity and disconnected client prototype.
            info: Validated remote Tool metadata.
            policy: Optional local execution policy.
        """
        self.server = server
        self.info = info
        self.policy_value = ToolPolicy() if policy is None else policy
        self.observability = Observability() if observability is None else observability

    @property
    def descriptor(self) -> ToolDescriptor:
        """Return the namespaced Pleiad Tool descriptor."""
        return self.info.descriptor

    @property
    def input_schema(self) -> JsonSchema:
        """Return the remote Tool input schema validated at discovery."""
        return self.info.input_schema

    @property
    def policy(self) -> ToolPolicy:
        """Return the local policy applied around the remote call."""
        return self.policy_value

    async def execute(self, context: ToolContext, arguments: JsonObject) -> ToolOutput:
        """Call the remote Tool once and preserve its complete observation."""
        logger.debug(
            f"MCP Tool call started server={self.server.code} tool={self.info.remote_name} call_id={context.call_id}"
        )
        with self.observability.span(
            MCP_CALL_NAME,
            attributes={CALL_ID: context.call_id, TOOL_CODE: self.descriptor.code},
            observation_type="tool",
        ) as span:
            try:
                async with self.server.client.new() as client:
                    result = await client.call_tool(
                        self.info.remote_name,
                        arguments,
                        timeout=self.policy.timeout_seconds,
                        raise_on_error=False,
                    )
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.error(
                    f"MCP Tool call failed server={self.server.code} tool={self.info.remote_name} "
                    f"call_id={context.call_id}",
                    exc_info=True,
                )
                raise McpToolCallFault() from error
            self.observability.finish(span, "failed" if result.is_error else "succeeded")

        try:
            output = project_result(result)
        except (TypeError, ValueError) as error:
            logger.error(
                f"MCP Tool result rejected server={self.server.code} tool={self.info.remote_name} "
                f"call_id={context.call_id}",
                exc_info=True,
            )
            raise McpToolResultFault() from error

        if result.is_error:
            logger.warning(
                f"MCP Tool returned a business failure server={self.server.code} tool={self.info.remote_name} "
                f"call_id={context.call_id} parts={len(result.content)}"
            )
            return ToolFailure(RemoteToolError(), output)

        logger.info(
            f"MCP Tool call completed server={self.server.code} tool={self.info.remote_name} "
            f"call_id={context.call_id} parts={len(result.content)}"
        )
        return output


def project_result(result: CallToolResult) -> JsonObject:
    """Preserve all serializable MCP content and structured content."""
    content = [json_value(part.model_dump(mode="json", by_alias=True, exclude_none=True)) for part in result.content]
    structured = None if result.structured_content is None else json_value(result.structured_content)
    return {"content": content, "structured_content": structured}


def json_value(value: object) -> JsonValue:
    """Validate arbitrary remote content against Pleiad's JSON value contract."""
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, Mapping):
        return {str(key): json_value(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [json_value(item) for item in value]
    raise ValueError("MCP content must be JSON serializable")
