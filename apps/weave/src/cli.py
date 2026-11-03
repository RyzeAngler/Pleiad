"""Typer CLI entrypoint for the Application Service."""

import asyncio
from pathlib import Path
from typing import Annotated, Literal

import typer
import uvicorn
from pleiad.contracts import JsonObject
from pleiad.errors.base import SystemFault
from pydantic import ConfigDict, TypeAdapter, ValidationError

from pleiad_weave.api.app import create_app
from pleiad_weave.application.dto import ApprovalRequest, ExecuteRequest
from pleiad_weave.application.errors import ApprovalConflictError
from pleiad_weave.infrastructure.bootstrap import create_application
from pleiad_weave.infrastructure.config import MemorySettings, Settings
from pleiad_weave.infrastructure.storage.models import ExecutionStatus
from pleiad_weave.infrastructure.storage.repository import ExecutionRepository
from pleiad_weave.memory_cli import memory_app
from pleiad_weave.tui.app import PleiadApp

app = typer.Typer(add_completion=False, help="Pleiad Weave dynamic Agent runtime")
approval_app = typer.Typer(help="Inspect and decide persisted Tool approval requests")
app.add_typer(approval_app, name="approval")
app.add_typer(memory_app, name="memory")
JSON_OBJECT_ADAPTER = TypeAdapter[JsonObject](JsonObject, config=ConfigDict(strict=True, allow_inf_nan=False))


@app.command()
def run(
    objective: str = typer.Argument(..., help="要完成的目标"),
    input_json: str = typer.Option("{}", "--input", help="JSON 格式的业务输入"),
) -> None:
    """Submit one objective to the Weave Agent."""
    try:
        request = ExecuteRequest(objective=objective, input=JSON_OBJECT_ADAPTER.validate_json(input_json))
    except ValidationError as error:
        raise typer.BadParameter("Input must be a valid JSON object and the objective must not be empty") from error
    service = create_application()

    async def execute() -> None:
        try:
            await service.start()
            queued = await service.submit(request)
            record = await service.wait(queued.execution_id)
            if record is None:
                raise RuntimeError("Execution disappeared before completion")
            typer.echo(record.model_dump_json(indent=2))
        finally:
            await service.shutdown()

    try:
        asyncio.run(execute())
    except SystemFault as error:
        if error.code != "weave.workspace_in_use":
            raise
        typer.echo(error.message, err=True)
        raise typer.Exit(code=1) from error


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8000, "--port"),
) -> None:
    """Start the HTTP API."""
    service = create_application()
    uvicorn.run(create_app(service), host=host, port=port)


@app.command()
def tui() -> None:
    """Start the interactive terminal workbench."""
    PleiadApp(create_application(Settings(log_console=False))).run()


@approval_app.command("list")
def list_approvals(
    execution_id: Annotated[str | None, typer.Option("--execution-id")] = None,
    workspace: Annotated[Path | None, typer.Option("--workspace")] = None,
) -> None:
    """List the exact pending actions, including requests retained after restart."""
    settings = MemorySettings() if workspace is None else MemorySettings(workspace=workspace)
    root = settings.runtime_root / "executions"
    if not (root / "execution-journal.sqlite3").is_file():
        if execution_id is not None:
            raise typer.BadParameter(f"Execution not found: {execution_id}")
        typer.echo("[]")
        return
    repository = ExecutionRepository(root)
    if execution_id is None:
        records = repository.list_nonterminal()
    else:
        record = repository.get(execution_id)
        if record is None:
            raise typer.BadParameter(f"Execution not found: {execution_id}")
        records = (record,)
    pending: list[ApprovalRequest] = []
    for record in records:
        if record.status is not ExecutionStatus.WAITING_APPROVAL:
            continue
        state = repository.resume_state(record.execution_id)
        if state is None:
            raise ApprovalConflictError("The waiting Execution recovery state is missing.")
        decided = {item.interruption_id for item in state.decisions}
        pending.extend(
            ApprovalRequest(execution_id=record.execution_id, interruption=item, deadline=state.checkpoint.deadline)
            for item in state.checkpoint.interruptions
            if item.interruption_id not in decided
        )
    typer.echo("[" + ",".join(item.model_dump_json() for item in pending) + "]")


@approval_app.command("approve")
def approve(execution_id: str, interruption_id: str) -> None:
    """Approve one exact persisted action and wait for its resumed outcome."""
    decide_approval(execution_id, interruption_id, "approve")


@approval_app.command("reject")
def reject(execution_id: str, interruption_id: str) -> None:
    """Reject one exact persisted action and wait for the Agent to continue."""
    decide_approval(execution_id, interruption_id, "reject")


def decide_approval(execution_id: str, interruption_id: str, decision: Literal["approve", "reject"]) -> None:
    """Submit a decision through the shared service without inventing a Tool result."""
    service = create_application()

    async def execute() -> None:
        try:
            await service.start()
            await service.decide(execution_id, interruption_id, decision)
            record = await service.wait(execution_id)
            if record is None:
                raise LookupError(f"Execution disappeared after its approval decision: {execution_id}")
            typer.echo(record.model_dump_json(indent=2))
        finally:
            await service.shutdown()

    try:
        asyncio.run(execute())
    except SystemFault as error:
        if error.code != "weave.workspace_in_use":
            raise
        typer.echo(error.message, err=True)
        raise typer.Exit(code=1) from error
