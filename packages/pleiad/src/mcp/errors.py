from enum import StrEnum

from pleiad.errors.base import SystemFault


class McpErrorCode(StrEnum):
    """Stable system-failure codes owned by the MCP component."""

    DISCOVERY_FAILED = "mcp.discovery.failed"
    TOOL_SCHEMA_INVALID = "mcp.tool.schema_invalid"
    TOOL_CALL_FAILED = "mcp.tool.call_failed"
    TOOL_RESULT_INVALID = "mcp.tool.result_invalid"


class McpDiscoveryFault(SystemFault):
    """Report an MCP connection or Tool discovery failure."""

    def __init__(self, message: str = "MCP Tool discovery failed.") -> None:
        """Create an MCP discovery fault.

        Args:
            message: Diagnostic description of the discovery failure.
        """
        super().__init__(McpErrorCode.DISCOVERY_FAILED, message)


class McpToolSchemaFault(SystemFault):
    """Report an MCP Tool contract that cannot be exposed safely."""

    def __init__(self, message: str = "An MCP Tool schema is invalid.") -> None:
        """Create an MCP Tool schema fault.

        Args:
            message: Diagnostic description of the invalid contract.
        """
        super().__init__(McpErrorCode.TOOL_SCHEMA_INVALID, message)


class McpToolCallFault(SystemFault):
    """Report an MCP transport or protocol failure during Tool execution."""

    def __init__(self, message: str = "The MCP Tool call failed.") -> None:
        """Create an MCP Tool call fault.

        Args:
            message: Diagnostic description of the call failure.
        """
        super().__init__(McpErrorCode.TOOL_CALL_FAILED, message)


class McpToolResultFault(SystemFault):
    """Report an MCP Tool result that cannot enter the Agent loop."""

    def __init__(self, message: str = "The MCP Tool result is invalid.") -> None:
        """Create an MCP Tool result fault.

        Args:
            message: Diagnostic description of the invalid result.
        """
        super().__init__(McpErrorCode.TOOL_RESULT_INVALID, message)
