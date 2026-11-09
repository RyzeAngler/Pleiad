import asyncio
from typing import Annotated, Literal
from uuid import uuid4

import httpx
import typer
import uvicorn
from pydantic import ValidationError

from pleiad_weave.api.app import create_app
from pleiad_weave.application.client import WeaveClient, display_json, execution_record
from pleiad_weave.capabilities.mcp import create_mcp_server, load_mcp_configuration
from pleiad_weave.infrastructure.bootstrap import create_application
from pleiad_weave.infrastructure.config import MemorySettings
from pleiad_weave.infrastructure.storage.product_models import ChatMessageRequest, ResolutionRequest
from pleiad_weave.memory_cli import memory_app
from pleiad_weave.tui.app import PleiadApp

app = typer.Typer(add_completion=False, help="Weave permanent chat and workspace controls")
attention_app = typer.Typer(help="Read and answer the running owner's persistent requests")
execution_app = typer.Typer(help="Inspect and control work owned by the running service")
app.add_typer(attention_app, name="attention")
app.add_typer(execution_app, name="execution")
app.add_typer(memory_app, name="memory")
DEFAULT_SERVER = "http://127.0.0.1:8000"


@app.command("mcp-login")
def mcp_login(server_code: str) -> None:
    """Authorize one explicitly selected MCP connection."""
    settings = MemorySettings()
    configuration = load_mcp_configuration(settings.workspace)
    connection = next((item for item in configuration.servers if item.code == server_code), None)
    if connection is None:
        raise typer.BadParameter(f"MCP server is not configured: {server_code}")
    server = create_mcp_server(connection, settings.workspace, interactive=True)

    async def login() -> None:
        async with server.client as client:
            tools = await client.list_tools()
        typer.echo(f"MCP connection ready: {server_code} ({len(tools)} tools)")

    asyncio.run(login())


@app.command()
def run(
    text: Annotated[str, typer.Argument(help="Message for the permanent main chat")],
    server: Annotated[str, typer.Option("--server", envvar="PLEIAD_SERVER_URL")] = DEFAULT_SERVER,
    message_id: Annotated[str | None, typer.Option("--message-id")] = None,
    executions: Annotated[list[str] | None, typer.Option("--execution")] = None,
    wait: Annotated[bool, typer.Option("--wait/--no-wait")] = True,
    timeout: Annotated[float, typer.Option("--timeout", min=1)] = 300.0,
) -> None:
    """Submit one chat message and observe its committed Router reply."""
    try:
        request = ChatMessageRequest(
            client_message_id=str(uuid4()) if message_id is None else message_id,
            text=text,
            target_execution_ids=() if executions is None else tuple(executions),
        )
    except ValidationError as error:
        raise typer.BadParameter(str(error)) from error

    async def send() -> None:
        async with WeaveClient(server) as client:
            initial = await client.snapshot()
            receipt = await client.submit(request)
            if not wait:
                typer.echo(receipt.model_dump_json(indent=2))
                return
            async with asyncio.timeout(timeout):
                reply = await client.wait_for_reply(receipt, after_sequence=initial.snapshot_sequence)
            typer.echo(reply.body)
            for execution_id in reply.target_execution_ids:
                record = execution_record(await client.execution(execution_id))
                typer.echo(f"{record.execution_id}  {record.status.value}  {record.objective}")

    try:
        asyncio.run(send())
    except (httpx.HTTPError, TimeoutError, ValueError) as error:
        typer.echo(f"Message request {request.client_message_id} failed: {error}", err=True)
        raise typer.Exit(code=1) from error


@app.command()
def serve(
    host: Annotated[str, typer.Option("--host")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", min=1, max=65535)] = 8000,
) -> None:
    """Start the workspace's single execution owner and browser interface."""
    uvicorn.run(create_app(create_application()), host=host, port=port)


@app.command()
def tui(
    server: Annotated[str, typer.Option("--server", envvar="PLEIAD_SERVER_URL")] = DEFAULT_SERVER,
) -> None:
    """Connect a terminal interface to the existing workspace owner."""
    PleiadApp(WeaveClient(server)).run()


@attention_app.command("list")
def list_attentions(
    server: Annotated[str, typer.Option("--server", envvar="PLEIAD_SERVER_URL")] = DEFAULT_SERVER,
) -> None:
    """List current user requests from the shared product journal."""

    async def read() -> None:
        async with WeaveClient(server) as client:
            typer.echo(display_json(await client.get("/v1/attentions", params={"status": "pending"})))

    asyncio.run(read())


@attention_app.command("answer")
def answer_attention(
    attention_id: str,
    answer: str,
    server: Annotated[str, typer.Option("--server", envvar="PLEIAD_SERVER_URL")] = DEFAULT_SERVER,
) -> None:
    """Answer the inspected question and commit its main-chat message once."""
    asyncio.run(resolve_attention(server, attention_id, answer=answer))


@attention_app.command("approve")
def approve_attention(
    attention_id: str,
    server: Annotated[str, typer.Option("--server", envvar="PLEIAD_SERVER_URL")] = DEFAULT_SERVER,
) -> None:
    """Approve the current exact host action bound to this request."""
    asyncio.run(resolve_attention(server, attention_id, decision="approve"))


@attention_app.command("reject")
def reject_attention(
    attention_id: str,
    server: Annotated[str, typer.Option("--server", envvar="PLEIAD_SERVER_URL")] = DEFAULT_SERVER,
) -> None:
    """Reject the current exact host action bound to this request."""
    asyncio.run(resolve_attention(server, attention_id, decision="reject"))


async def resolve_attention(
    server: str,
    attention_id: str,
    *,
    answer: str | None = None,
    decision: Literal["approve", "reject"] | None = None,
) -> None:
    """Read the latest request and submit its revision and action digest."""
    async with WeaveClient(server) as client:
        current = await client.attention(attention_id)
        request = ResolutionRequest(
            request_id=str(uuid4()),
            expected_revision=current.revision,
            answer=answer,
            decision=decision,
            action_digest=current.action_digest,
        )
        result = await client.resolve(attention_id, request)
        typer.echo(result.model_dump_json(indent=2))


@execution_app.command("list")
def list_executions(
    server: Annotated[str, typer.Option("--server", envvar="PLEIAD_SERVER_URL")] = DEFAULT_SERVER,
) -> None:
    """List the running owner's durable work records."""

    async def read() -> None:
        async with WeaveClient(server) as client:
            typer.echo(display_json(await client.get("/v1/executions")))

    asyncio.run(read())


@execution_app.command("show")
def show_execution(
    execution_id: str,
    server: Annotated[str, typer.Option("--server", envvar="PLEIAD_SERVER_URL")] = DEFAULT_SERVER,
) -> None:
    """Read current work details, reports, children, and artifacts."""

    async def read() -> None:
        async with WeaveClient(server) as client:
            typer.echo(display_json(await client.execution(execution_id)))

    asyncio.run(read())


@execution_app.command("control")
def control_execution(
    execution_id: str,
    action: Literal["pause", "resume", "cancel"],
    server: Annotated[str, typer.Option("--server", envvar="PLEIAD_SERVER_URL")] = DEFAULT_SERVER,
) -> None:
    """Submit a revision-checked pause, resume, or cancellation request."""

    async def control() -> None:
        async with WeaveClient(server) as client:
            record = execution_record(await client.execution(execution_id))
            result = await client.control(record, action)
            typer.echo(result.model_dump_json(indent=2))

    asyncio.run(control())
