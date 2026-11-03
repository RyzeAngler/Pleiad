import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal

import typer

from pleiad_weave.infrastructure.config import MemorySettings
from pleiad_weave.infrastructure.storage.memory import MemoryConflictError, MemoryFact, MemoryStore

memory_app = typer.Typer(help="Manage this workspace's locally owned memory facts.")


@dataclass(frozen=True, slots=True)
class MemoryCommand:
    """Bind local management commands to one host-configured memory scope."""

    store: MemoryStore
    scope: Literal["conversation", "user"]
    conversation_id: str | None


@memory_app.callback()
def configure_memory(
    context: typer.Context,
    workspace: Annotated[
        Path | None, typer.Option("--workspace", help="Host workspace; defaults to deployment settings.")
    ] = None,
    scope: Annotated[Literal["conversation", "user"], typer.Option("--scope")] = "conversation",
    conversation_id: Annotated[str | None, typer.Option("--conversation-id")] = None,
) -> None:
    """Select a local deployment scope without creating model or Sandbox clients."""
    try:
        settings = MemorySettings() if workspace is None else MemorySettings(workspace=workspace)
        store = MemoryStore(
            settings.runtime_root / "memory",
            user_id=settings.memory_user_id,
            cross_conversation=settings.memory_cross_conversation,
        )
        store.management_scope_id(scope, conversation_id)
    except (PermissionError, ValueError) as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(code=2) from error
    context.obj = MemoryCommand(store, scope, conversation_id)


def command_binding(context: typer.Context) -> MemoryCommand:
    """Require the deployment binding installed by the memory command group."""
    selected: object = context.obj
    if not isinstance(selected, MemoryCommand):
        raise RuntimeError("Memory command scope is not configured")
    return selected


def emit_memory(operation: Callable[[], tuple[MemoryFact, ...]]) -> None:
    """Print current facts or a clear management conflict as local JSON output."""
    try:
        facts = operation()
    except MemoryConflictError as error:
        typer.echo(f"{error.code}: {error.message}", err=True)
        raise typer.Exit(code=2) from error
    except (LookupError, PermissionError, ValueError) as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(code=2) from error
    typer.echo(json.dumps([fact.model_dump(mode="json") for fact in facts], ensure_ascii=False, indent=2))


@memory_app.command("list")
def list_memory(context: typer.Context, include_inactive: Annotated[bool, typer.Option("--all")] = False) -> None:
    """List current facts, optionally including deactivated facts."""
    binding = command_binding(context)
    emit_memory(
        lambda: binding.store.list_facts(
            binding.scope, conversation_id=binding.conversation_id, include_inactive=include_inactive
        )
    )


@memory_app.command("show")
def show_memory(
    context: typer.Context,
    memory_id: str,
    history: Annotated[bool, typer.Option("--history")] = False,
) -> None:
    """Show the latest fact or every revision with its original provenance."""
    binding = command_binding(context)

    def select() -> tuple[MemoryFact, ...]:
        records = binding.store.fact_history(binding.scope, memory_id, conversation_id=binding.conversation_id)
        if not records:
            raise LookupError(f"Memory fact not found: {memory_id}")
        return records if history else (records[-1],)

    emit_memory(select)


@memory_app.command("update")
def update_memory(
    context: typer.Context,
    memory_id: str,
    content: Annotated[str, typer.Option("--content")],
    expected_revision: Annotated[int, typer.Option("--expected-revision", min=1)],
) -> None:
    """Append a local edit only if the displayed revision is still current."""
    binding = command_binding(context)
    emit_memory(
        lambda: (
            binding.store.update_fact(
                binding.scope,
                memory_id,
                content,
                expected_revision=expected_revision,
                conversation_id=binding.conversation_id,
            ),
        )
    )


@memory_app.command("deactivate")
def deactivate_memory(
    context: typer.Context,
    memory_id: str,
    expected_revision: Annotated[int, typer.Option("--expected-revision", min=1)],
) -> None:
    """Keep fact history while removing its next revision from automatic recall."""
    binding = command_binding(context)
    emit_memory(
        lambda: (
            binding.store.deactivate_fact(
                binding.scope,
                memory_id,
                expected_revision=expected_revision,
                conversation_id=binding.conversation_id,
            ),
        )
    )
