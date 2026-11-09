import asyncio
import hashlib
import os
import sqlite3
import stat
from pathlib import Path
from typing import Literal

from pleiad.contracts import ContractModel, JsonObject, JsonValue
from pleiad.tools.base import FunctionTool
from pleiad.tools.builtins.file_support import (
    FileToolBinding,
    normalize_relative_path,
    read_scoped_bytes,
    scoped_parent,
    write_scoped_bytes,
)
from pleiad.tools.contracts import ToolContext, ToolDescriptor
from pleiad.tools.errors import ToolFileConflictError
from pydantic import Field

from pleiad_weave.infrastructure.storage.product import ProductStore, stable_id
from pleiad_weave.infrastructure.storage.product_content import ProductContent
from pleiad_weave.infrastructure.storage.product_models import (
    Artifact,
    Attachment,
    Execution,
    InboxItem,
    Message,
    Report,
)


class ContentArguments(ContractModel):
    """Select an authorized source or a bounded task-local output."""

    action: Literal["list", "materialize", "publish"] = "list"
    kind: Literal["attachment", "artifact"] = "attachment"
    id: str | None = None
    path: str | None = None
    name: str | None = None
    mime: str = "text/plain"


class ExecutionContent:
    """Bridge explicitly referenced product bytes into one isolated Worker scratch root."""

    def __init__(self, execution_id: str, store: ProductStore, content: ProductContent, scratch: Path) -> None:
        """Bind the immutable product owner and task-local file root."""
        self.execution_id = execution_id
        self.store = store
        self.content = content
        self.scratch = scratch

    def source_messages(self, connection: sqlite3.Connection) -> tuple[Message, ...]:
        """Read accepted source messages and their explicitly quoted main-chat facts."""
        items = self.store.records(connection, "inbox_items", InboxItem)
        identities = {
            value.causation_id
            for value in items
            if value.target_kind == "execution" and value.target_id == self.execution_id
        }
        messages = tuple(
            Message.model_validate_json(row["payload"])
            for row in connection.execute("SELECT payload FROM chat_messages ORDER BY sequence")
        )
        quoted = {value.reply_to_message_id for value in messages if value.message_id in identities}
        return tuple(value for value in messages if value.message_id in identities or value.message_id in quoted)

    def sources(self) -> tuple[Attachment | Artifact, ...]:
        """Read only own outputs, child results, and references carried by assigned messages."""
        with self.store.transaction() as connection:
            self.store.writable_execution(connection, self.execution_id)
            messages = self.source_messages(connection)
            attachments = {identity for message in messages for identity in message.attachment_ids}
            artifacts = {
                reference.id
                for message in messages
                for reference in message.context_refs
                if reference.kind == "artifact"
            }
            for message in messages:
                artifacts.update(
                    row["target_id"]
                    for row in connection.execute(
                        "SELECT target_id FROM object_references "
                        "WHERE owner_kind='message' AND owner_id=? AND target_kind='artifact'",
                        (message.message_id,),
                    )
                )
                for report_id in message.report_ids:
                    report = self.store.require(connection, "reports", report_id, Report)
                    artifacts.update(report.artifact_ids)
            owners = {self.execution_id} | {
                value.execution_id
                for value in self.store.records(connection, "executions", Execution)
                if value.parent_execution_id == self.execution_id
            }
            return (
                *(
                    self.store.require(connection, "attachments", identity, Attachment)
                    for identity in sorted(attachments)
                ),
                *(
                    value
                    for value in self.store.records(connection, "artifacts", Artifact)
                    if value.artifact_id in artifacts or value.owner_execution_id in owners
                ),
            )

    def materialize(self, kind: str, identity: str) -> JsonObject:
        """Copy a validated source using pinned no-follow SDK file operations."""
        selected = next(
            (
                value
                for value in self.sources()
                if (isinstance(value, Attachment) and kind == "attachment" and value.attachment_id == identity)
                or (isinstance(value, Artifact) and kind == "artifact" and value.artifact_id == identity)
            ),
            None,
        )
        if selected is None:
            raise PermissionError("The content is outside this Execution's accepted references")
        payload = (
            self.content.read_attachment(identity) if kind == "attachment" else self.content.read_artifact(identity)
        )
        relative = normalize_relative_path(f"sources/{kind}/{identity}/{selected.name}", "scratch")
        try:
            write_scoped_bytes(self.scratch, relative, payload, expected_sha256=None, max_bytes=25 * 1024 * 1024)
        except ToolFileConflictError:
            existing = read_scoped_bytes(
                self.scratch, scope="scratch", relative_path=relative, max_bytes=25 * 1024 * 1024
            )
            if existing != payload:
                raise
        return {"kind": kind, "id": identity, "scope": "scratch", "path": relative, "size": len(payload)}

    def publish(self, run_id: str, call_id: str, arguments: ContentArguments) -> JsonObject:
        """Register task-local bytes under one actual SDK invocation and call.

        Args:
            run_id: Original SDK invocation owning this publication.
            call_id: Tool-call identity within that invocation.
            arguments: Authorized local path and public artifact metadata.

        Returns:
            Public artifact metadata for the immutable installed content.
        """
        if arguments.path is None or arguments.name is None:
            raise ValueError("Publishing requires a scratch path and artifact name")
        relative = normalize_relative_path(arguments.path, "scratch")
        payload = read_scoped_bytes(self.scratch, scope="scratch", relative_path=relative, max_bytes=25 * 1024 * 1024)
        value = self.content.install_artifact(
            self.execution_id,
            artifact_id=stable_id(f"artifact/{self.execution_id}/{run_id}/{call_id}"),
            name=arguments.name,
            mime=arguments.mime,
            payload=payload,
        )
        return value.model_dump(mode="json", exclude={"relative_path"})

    def tool(self) -> FunctionTool:
        """Expose source discovery, isolated materialization, and owned artifact publication."""

        async def execute(context: ToolContext, arguments: JsonObject) -> JsonObject:
            selected = ContentArguments.model_validate(arguments)
            if selected.action == "list":
                values = await asyncio.to_thread(self.sources)
                return {"sources": [value.model_dump(mode="json", exclude={"relative_path"}) for value in values]}
            if selected.action == "materialize":
                if selected.id is None:
                    raise ValueError("Materializing requires its exact source identity")
                return await asyncio.to_thread(self.materialize, selected.kind, selected.id)
            if context.run_id is None:
                raise ValueError("Artifact publication requires its owning SDK Run")
            return await asyncio.to_thread(self.publish, context.run_id, context.call_id, selected)

        return FunctionTool(
            ToolDescriptor(
                name="execution_content",
                code="weave.execution_content",
                description="List sources, materialize accepted content into scratch, or publish owned artifacts.",
            ),
            execute,
            ContentArguments,
        )


class ResourceArguments(ContractModel):
    """Inspect or copy an explicitly granted workspace resource into task files."""

    action: Literal["list", "materialize"] = "list"
    path: str = ""
    cursor: int = Field(default=0, ge=0)
    limit: int = Field(default=50, ge=1, le=100)


class WorkspaceResource:
    """Expose shared host data read-only while excluding product state and other tasks."""

    def __init__(self, workspace: Path, work_root: Path) -> None:
        """Bind the resource only after its Space explicitly selects the host identity."""
        self.files = FileToolBinding(workspace, work_root, excluded_workspace_paths=("runtime-v2", "work"))

    def read(self, arguments: ResourceArguments) -> JsonObject:
        """Use the SDK's pinned-directory operations for authorized shared reads."""
        relative = normalize_relative_path(arguments.path, "resource") if arguments.path else ""
        if relative:
            self.files.validate_workspace_path(relative)
        if arguments.action == "list":
            with scoped_parent(self.files.workspace, f"{relative}/.listing" if relative else ".listing") as parent:
                names = sorted(os.listdir(parent[0]))
                visible = [
                    name
                    for name in names
                    if (f"{relative}/{name}" if relative else name).split("/")[0]
                    not in self.files.excluded_workspace_paths
                ]
                selected = visible[arguments.cursor : arguments.cursor + arguments.limit]
                entries: list[JsonValue] = []
                for name in selected:
                    metadata = os.stat(name, dir_fd=parent[0], follow_symlinks=False)
                    entry_kind = "unavailable"
                    if stat.S_ISDIR(metadata.st_mode):
                        entry_kind = "directory"
                    elif stat.S_ISREG(metadata.st_mode):
                        entry_kind = "file"
                    entries.append(
                        {
                            "name": name,
                            "path": f"{relative}/{name}" if relative else name,
                            "kind": entry_kind,
                            "size": metadata.st_size,
                        }
                    )
            return {
                "resource_id": "workspace",
                "items": entries,
                "total": len(visible),
                "next_cursor": arguments.cursor + arguments.limit
                if arguments.cursor + arguments.limit < len(visible)
                else None,
            }
        if not relative:
            raise ValueError("Materializing a resource requires a relative file path")
        payload = read_scoped_bytes(
            self.files.workspace, scope="resource", relative_path=relative, max_bytes=25 * 1024 * 1024
        )
        digest = hashlib.sha256(payload).hexdigest()
        target = f"resources/workspace/{digest}/{relative}"
        try:
            write_scoped_bytes(self.files.scratch, target, payload, expected_sha256=None, max_bytes=25 * 1024 * 1024)
        except ToolFileConflictError:
            current = read_scoped_bytes(
                self.files.scratch, scope="scratch", relative_path=target, max_bytes=25 * 1024 * 1024
            )
            if current != payload:
                raise
        return {
            "resource_id": "workspace",
            "source_path": relative,
            "scope": "scratch",
            "path": target,
            "sha256": digest,
        }

    def tool(self) -> FunctionTool:
        """Expose a bounded read-only host resource through the authorized Worker catalog."""

        async def execute(context: ToolContext, arguments: JsonObject) -> JsonObject:
            return await asyncio.to_thread(self.read, ResourceArguments.model_validate(arguments))

        return FunctionTool(
            ToolDescriptor(
                name="workspace_resource",
                code="weave.workspace_resource",
                description=(
                    "List explicitly granted shared workspace data or materialize a read-only source into scratch."
                ),
            ),
            execute,
            ResourceArguments,
            required_permissions=frozenset({"file.read"}),
        )
