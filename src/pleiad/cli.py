"""Typer CLI entrypoint for the Application Service."""

import asyncio
import json

import typer
import uvicorn

from pleiad.application.models import ExecuteRequest
from pleiad.bootstrap import create_application
from pleiad.config import Settings
from pleiad.entrypoints.api import create_app
from pleiad.tui import run_tui

app = typer.Typer(add_completion=False, help="Pleiad dynamic Agent runtime")


@app.command()
def run(
    objective: str = typer.Argument(..., help="要完成的目标"),
    input_json: str = typer.Option("{}", "--input", help="JSON 格式的业务输入"),
) -> None:
    """Submit one objective to PleiadAgent."""
    service = create_application()
    request = ExecuteRequest(objective=objective, input=json.loads(input_json))
    record = asyncio.run(service.execute(request))
    typer.echo(record.model_dump_json(indent=2))


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
    run_tui(create_application(Settings(log_console=False)))
