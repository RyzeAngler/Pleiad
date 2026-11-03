import asyncio

import pytest
from fastmcp import Client, FastMCP
from fastmcp.client.transports import FastMCPTransport
from fastmcp.exceptions import ToolError
from mcp_types import CallToolResult, TextContent

from pleiad.contracts import CapabilityGrants
from pleiad.mcp.contracts import McpServer
from pleiad.mcp.discovery import discover_tools
from pleiad.mcp.errors import McpErrorCode, McpToolCallFault
from pleiad.tools.contracts import ToolCall, ToolContext, ToolResult
from pleiad.tools.errors import ToolErrorCode
from pleiad.tools.runtime import ToolRuntime


def test_host_bound_mcp_permissions_block_side_effects_until_the_current_run_is_granted() -> None:
    app = FastMCP("counter")
    state = {"counter": 0}

    @app.tool
    def increment() -> int:
        state["counter"] += 1
        return state["counter"]

    async def scenario() -> None:
        permissions = {"increment": frozenset({"counter.write"})}
        server = McpServer("counter", Client(FastMCPTransport(app)), tool_permissions=permissions)
        permissions.clear()
        tool = (await discover_tools(server))[0]
        call = ToolCall(call_id="increment", tool_code=tool.descriptor.code)
        runtime = ToolRuntime()
        denied = await runtime.execute(tool, call, ToolContext(call_id=call.call_id))
        assert isinstance(denied, ToolResult) and not denied.ok
        assert denied.error_code == ToolErrorCode.PERMISSION_DENIED
        assert state["counter"] == 0
        granted = await runtime.execute(
            tool,
            call,
            ToolContext(call_id=call.call_id, grants=CapabilityGrants(capabilities=frozenset({"counter.write"}))),
        )
        assert isinstance(granted, ToolResult) and granted.ok
        assert state["counter"] == 1

    asyncio.run(scenario())


def test_cancelling_a_normal_mcp_request_propagates_and_finishes_remote_cleanup() -> None:
    app = FastMCP("cancellation")
    started = asyncio.Event()
    stopped = asyncio.Event()

    @app.tool
    async def wait_for_cancel() -> str:
        started.set()
        try:
            await asyncio.Event().wait()
            return "unreachable"
        finally:
            stopped.set()

    async def scenario() -> None:
        tool = (await discover_tools(McpServer("cancel", Client(FastMCPTransport(app)))))[0]
        task = asyncio.create_task(tool.execute(ToolContext(call_id="cancel"), {}))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        async with asyncio.timeout(5):
            await stopped.wait()

    asyncio.run(scenario())


def test_mcp_tool_preserves_all_remote_content_and_structured_content() -> None:
    app = FastMCP("test-server")

    @app.tool
    def inspect() -> CallToolResult:
        return CallToolResult(
            content=[TextContent(text="first"), TextContent(text="second")],
            structuredContent={"answer": "ready"},
        )

    async def scenario() -> None:
        client = Client(FastMCPTransport(app))
        tool = (await discover_tools(McpServer("test_server", client)))[0]

        output = await tool.execute(ToolContext(call_id="call-1"), {})

        assert output == {
            "content": [
                {"type": "text", "text": "first"},
                {"type": "text", "text": "second"},
            ],
            "structured_content": {"answer": "ready"},
        }
        assert not client.is_connected()

    asyncio.run(scenario())


def test_mcp_tool_classifies_transport_failure_as_a_system_fault(monkeypatch: pytest.MonkeyPatch) -> None:
    cause = ConnectionError("private transport detail")
    app = FastMCP("test-server")

    @app.tool
    def ping() -> str:
        return "pong"

    async def scenario() -> None:
        client = Client(FastMCPTransport(app))
        tool = (await discover_tools(McpServer("test_server", client)))[0]

        async def fail_call(*args: object, **kwargs: object) -> CallToolResult:
            raise cause

        monkeypatch.setattr(Client, "call_tool", fail_call)

        with pytest.raises(McpToolCallFault) as caught:
            await tool.execute(ToolContext(call_id="call-1"), {})

        assert caught.value.code == McpErrorCode.TOOL_CALL_FAILED
        assert caught.value.__cause__ is cause

    asyncio.run(scenario())


def test_remote_tool_failure_returns_its_observation_to_the_agent_loop() -> None:
    app = FastMCP("test-server")

    @app.tool
    def fail() -> str:
        raise ToolError("remote failure")

    async def scenario() -> None:
        client = Client(FastMCPTransport(app))
        tool = (await discover_tools(McpServer("test_server", client)))[0]

        outcome = await ToolRuntime().execute(
            tool,
            ToolCall(call_id="call-1", tool_code=tool.descriptor.code, arguments={}),
            ToolContext(call_id="call-1"),
        )

        assert isinstance(outcome, ToolResult)
        assert not outcome.ok
        assert outcome.error_code == ToolErrorCode.REMOTE_FAILED
        assert outcome.output == {
            "content": [{"type": "text", "text": "remote failure"}],
            "structured_content": None,
        }
        assert not client.is_connected()

    asyncio.run(scenario())
