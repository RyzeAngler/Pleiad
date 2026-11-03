import asyncio

import pytest
from fastmcp import Client, FastMCP
from fastmcp.client.transports import FastMCPTransport

from pleiad.errors.base import SystemFault
from pleiad.mcp.contracts import McpServer
from pleiad.mcp.discovery import discover_tools
from pleiad.mcp.errors import McpErrorCode


def test_discovery_rejects_host_permissions_for_a_missing_remote_tool() -> None:
    app = FastMCP("permissions")

    @app.tool
    def read() -> str:
        return "value"

    async def scenario() -> None:
        server = McpServer(
            "permissions", Client(FastMCPTransport(app)), tool_permissions={"misspelled": frozenset({"resource.read"})}
        )
        with pytest.raises(SystemFault) as caught:
            await discover_tools(server)
        assert caught.value.code == McpErrorCode.TOOL_SCHEMA_INVALID
        assert isinstance(caught.value.__cause__, ValueError)

    asyncio.run(scenario())


def test_discovery_namespaces_tools_without_executing_them() -> None:
    calls = 0
    app = FastMCP("test-server")

    @app.tool(description="Echo remote text")
    def echo(value: str) -> dict[str, str]:
        nonlocal calls
        calls += 1
        return {"value": value}

    async def scenario() -> None:
        client = Client(FastMCPTransport(app))
        server = McpServer("test_server", client)

        tools = await discover_tools(server)

        assert len(tools) == 1
        assert tools[0].descriptor.name == "test_server__echo"
        assert tools[0].descriptor.code == "mcp.test_server.echo"
        assert tools[0].input_schema["required"] == ["value"]
        assert calls == 0
        assert not client.is_connected()

    asyncio.run(scenario())


def test_discovery_classifies_transport_failure_as_a_system_fault(monkeypatch: pytest.MonkeyPatch) -> None:
    cause = ConnectionError("private transport detail")
    app = FastMCP("test-server")

    async def fail_list(*args: object, **kwargs: object) -> list[object]:
        raise cause

    monkeypatch.setattr(Client, "list_tools", fail_list)

    async def scenario() -> None:
        server = McpServer("test_server", Client(FastMCPTransport(app)))

        with pytest.raises(SystemFault) as caught:
            await discover_tools(server)

        assert caught.value.code == McpErrorCode.DISCOVERY_FAILED
        assert caught.value.__cause__ is cause

    asyncio.run(scenario())


def test_discovery_propagates_cancellation(monkeypatch: pytest.MonkeyPatch) -> None:
    app = FastMCP("test-server")

    async def cancel_list(*args: object, **kwargs: object) -> list[object]:
        raise asyncio.CancelledError

    monkeypatch.setattr(Client, "list_tools", cancel_list)

    async def scenario() -> None:
        server = McpServer("test_server", Client(FastMCPTransport(app)))

        with pytest.raises(asyncio.CancelledError):
            await discover_tools(server)

    asyncio.run(scenario())


def test_discovery_uses_a_fresh_connection_each_time() -> None:
    app = FastMCP("test-server")

    @app.tool
    def ping() -> str:
        return "pong"

    async def scenario() -> None:
        client = Client(FastMCPTransport(app))
        server = McpServer("test_server", client)

        first = await discover_tools(server)
        assert not client.is_connected()
        second = await discover_tools(server)

        assert first[0].descriptor == second[0].descriptor
        assert not client.is_connected()

    asyncio.run(scenario())
