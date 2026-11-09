import fcntl
import hashlib
import json
import os
import re
import shutil
import sqlite3
import zipfile
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Literal

from pleiad_weave.infrastructure.storage.product import JSON_OBJECT, ProductStore, fingerprint, stable_id
from pleiad_weave.infrastructure.storage.product_details import (
    GraphDefinition,
    GraphRunDetail,
    NodeAttempt,
    TextPage,
    ToolResultDetail,
    text_page,
)
from pleiad_weave.infrastructure.storage.product_models import (
    TERMINAL_STATUSES,
    Artifact,
    Attachment,
    Attention,
    CleanupRecord,
    CleanupScope,
    DeletionPreview,
    Execution,
    InboxItem,
    InputDisposition,
    ProductCapacityError,
    ProductConflict,
    Report,
    Space,
)

EMPTY_CLEANUP_SCOPE = CleanupScope()
REQUIRED_CLEANUP_STAGES = frozenset({"sessions_planned", "sessions", "facts", "work", "details", "content"})
TEXT_PREVIEW_MIMES = frozenset({"text/plain", "text/markdown", "text/csv", "application/json"})

SUPPORTED_MIMES = frozenset(
    {
        "application/pdf",
        "text/plain",
        "text/markdown",
        "text/csv",
        "application/json",
        "image/png",
        "image/jpeg",
        "image/webp",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    }
)


class ContentCorruptError(ValueError):
    """Report unavailable or modified content without fabricating an empty payload."""


class ProductContent:
    """Own validated files and generation-fenced cleanup beside product metadata.

    Args:
        store: The product transaction owner used for every reference decision.
    """

    def __init__(self, store: ProductStore) -> None:
        self.store = store

    def validate_name(self, name: str) -> None:
        """Reject path components in an untrusted display filename."""
        if not name or name in {".", ".."} or Path(name).name != name or "\\" in name or "\x00" in name:
            raise ValueError("A filename must be a single nonempty path component")

    def validate_upload(self, payload: bytes, mime: str) -> None:
        """Validate supported uploaded bytes before returning a ready reference."""
        if not payload or len(payload) > 25 * 1024 * 1024:
            raise ProductCapacityError("An attachment must contain 1 byte to 25 MiB")
        if mime not in SUPPORTED_MIMES:
            raise ValueError("Unsupported attachment MIME type")
        if mime == "application/pdf":
            valid = payload.startswith(b"%PDF-")
        elif mime == "image/png":
            valid = payload.startswith(b"\x89PNG\r\n\x1a\n")
        elif mime == "image/jpeg":
            valid = payload.startswith(b"\xff\xd8\xff")
        elif mime == "image/webp":
            valid = payload.startswith(b"RIFF") and payload[8:12] == b"WEBP"
        elif mime.startswith("application/vnd.openxmlformats"):
            try:
                with zipfile.ZipFile(BytesIO(payload)) as archive:
                    names = set(archive.namelist())
                    expected = "word/document.xml" if mime.endswith("document") else "xl/workbook.xml"
                    valid = "[Content_Types].xml" in names and expected in names
            except zipfile.BadZipFile as error:
                raise ValueError("The attachment is not a valid Office document") from error
        else:
            try:
                text = payload.decode("utf-8")
                if mime == "application/json":
                    json.loads(text)
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise ValueError("The attachment is not valid UTF-8 text for its MIME type") from error
            valid = True
        if not valid:
            raise ValueError("The attachment bytes do not match the declared MIME type")

    def guard_digest(self, connection: sqlite3.Connection, digest: str) -> None:
        """Prevent content installation into a generation currently being reclaimed."""
        for value in self.store.records(connection, "artifacts", Artifact):
            if value.digest == digest and value.deletion_state == "deleting":
                raise ProductConflict("The same content is being reclaimed by an active deletion")

    def install_bytes(self, payload: bytes) -> tuple[str, str]:
        """Atomically install validated bytes before committing any product reference."""
        digest = hashlib.sha256(payload).hexdigest()
        relative_path = f"{digest[:2]}/{digest}"
        path = self.store.content_root / relative_path
        path.parent.mkdir(exist_ok=True)
        if not path.resolve().is_relative_to(self.store.content_root):
            raise ContentCorruptError("Content installation escaped its owned root")
        with self.store.transaction(write=True) as connection:
            self.guard_digest(connection, digest)
            with NamedTemporaryFile(dir=path.parent, prefix="incoming-", delete=False) as stream:
                temporary = Path(stream.name)
                try:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                    os.replace(temporary, path)
                finally:
                    temporary.unlink(missing_ok=True)
        return digest, relative_path

    def upload_attachment(self, *, client_attachment_id: str, name: str, mime: str, payload: bytes) -> Attachment:
        """Accept a fully validated upload with stable full-payload idempotency."""
        self.validate_name(name)
        self.validate_upload(payload, mime)
        digest = hashlib.sha256(payload).hexdigest()
        request_digest = fingerprint(
            {"client_attachment_id": client_attachment_id, "name": name, "mime": mime, "digest": digest}
        )
        key = f"attachment/upload/{client_attachment_id}"
        with self.store.transaction() as connection:
            old = self.store.receipt(connection, key, request_digest, Attachment)
            if old is not None:
                return old
        digest, relative_path = self.install_bytes(payload)
        with self.store.transaction(write=True) as connection:
            old = self.store.receipt(connection, key, request_digest, Attachment)
            if old is not None:
                return old
            self.guard_digest(connection, digest)
            value = Attachment(
                attachment_id=stable_id(key),
                client_attachment_id=client_attachment_id,
                name=name,
                mime=mime,
                size=len(payload),
                digest=digest,
                relative_path=relative_path,
                expires_at=datetime.now(UTC) + timedelta(hours=24),
            )
            self.store.save(
                connection, "attachments", value.attachment_id, value, client_id=client_attachment_id, digest=digest
            )
            self.store.save_receipt(connection, key, request_digest, value)
            self.store.emit(connection, value.attachment_id, "attachment.ready", value.model_dump(mode="json"))
            return value

    def install_artifact(
        self,
        execution_id: str,
        *,
        artifact_id: str,
        name: str,
        mime: str,
        payload: bytes,
        graph_run_id: str | None = None,
        node_attempt_id: str | None = None,
    ) -> Artifact:
        """Install owned output bytes and commit their exact source metadata."""
        self.validate_name(name)
        if len(mime) > 255 or re.fullmatch(r"[A-Za-z0-9!#$%&'*+.^_`|~-]+/[A-Za-z0-9!#$%&'*+.^_`|~-]+", mime) is None:
            raise ValueError(
                "An artifact MIME type must be a valid media type without parameters or control characters"
            )
        digest = hashlib.sha256(payload).hexdigest()
        request_digest = fingerprint(
            {
                "execution_id": execution_id,
                "artifact_id": artifact_id,
                "name": name,
                "mime": mime,
                "digest": digest,
                "graph_run_id": graph_run_id,
                "node_attempt_id": node_attempt_id,
            }
        )
        key = f"artifact/install/{artifact_id}"
        with self.store.transaction() as connection:
            old = self.store.receipt(connection, key, request_digest, Artifact)
            if old is not None:
                return old
            self.store.writable_execution(connection, execution_id)
        digest, relative_path = self.install_bytes(payload)
        with self.store.transaction(write=True) as connection:
            old = self.store.receipt(connection, key, request_digest, Artifact)
            if old is not None:
                return old
            self.store.writable_execution(connection, execution_id)
            self.guard_digest(connection, digest)
            value = Artifact(
                artifact_id=artifact_id,
                created_at=datetime.now(UTC),
                name=name,
                mime=mime,
                size=len(payload),
                digest=digest,
                relative_path=relative_path,
                owner_execution_id=execution_id,
                graph_run_id=graph_run_id,
                node_attempt_id=node_attempt_id,
                previewable=mime in TEXT_PREVIEW_MIMES or mime in {"image/png", "image/jpeg", "image/webp"},
            )
            self.store.save(connection, "artifacts", artifact_id, value, owner_id=execution_id, digest=digest)
            self.store.save_receipt(connection, key, request_digest, value)
            self.store.emit(connection, artifact_id, "artifact.created", value.model_dump(mode="json"))
            return value

    def checked_bytes(self, relative_path: str, digest: str, size: int) -> bytes:
        """Read only contained content whose complete length and digest still match."""
        path = (self.store.content_root / relative_path).resolve()
        if not path.is_relative_to(self.store.content_root):
            raise ContentCorruptError("Content reference escapes the owned content root")
        try:
            payload = path.read_bytes()
        except OSError as error:
            raise ContentCorruptError("Referenced content is unavailable") from error
        if len(payload) != size or hashlib.sha256(payload).hexdigest() != digest:
            raise ContentCorruptError("Referenced content failed its integrity check")
        return payload

    def read_attachment(self, attachment_id: str) -> bytes:
        """Read a ready or formally referenced attachment after checking its lifecycle."""
        value = self.store.get_attachment(attachment_id)
        if value is None:
            raise LookupError("Attachment not found")
        if value.status == "deleted" or (value.expires_at is not None and value.expires_at <= datetime.now(UTC)):
            raise ProductConflict("The attachment is deleted or expired")
        return self.checked_bytes(value.relative_path, value.digest, value.size)

    def read_artifact(self, artifact_id: str) -> bytes:
        """Verify an owned result and explicitly mark missing or changed content corrupt."""
        value = self.store.get_artifact(artifact_id)
        if value is None:
            raise LookupError("Artifact not found")
        if value.deletion_state == "corrupt":
            raise ContentCorruptError("Referenced content is marked corrupt")
        if value.deletion_state != "active":
            raise ProductConflict("The artifact is not available")
        try:
            return self.checked_bytes(value.relative_path, value.digest, value.size)
        except ContentCorruptError:
            with self.store.transaction(write=True) as connection:
                latest = self.store.require(connection, "artifacts", artifact_id, Artifact)
                if latest.deletion_state == "active":
                    corrupt = latest.model_copy(update={"deletion_state": "corrupt"})
                    self.store.save(
                        connection,
                        "artifacts",
                        artifact_id,
                        corrupt,
                        owner_id=latest.owner_execution_id,
                        digest=latest.digest,
                    )
                    self.store.emit(connection, artifact_id, "artifact.corrupt", corrupt.model_dump(mode="json"))
            raise

    def content_text_page(
        self, kind: Literal["attachment", "artifact"], identity: str, *, offset: int = 0, max_bytes: int = 8192
    ) -> TextPage:
        """Read a bounded text preview after validating original content and lifecycle.

        Args:
            kind: Product-owned content collection.
            identity: Stable attachment or artifact identity.
            offset: UTF-8 byte offset into the original content.
            max_bytes: Maximum response bytes, bounded to 32 KiB.

        Returns:
            A text page carrying its original identity and product watermark.

        Raises:
            LookupError: The original product record is missing.
            ValueError: The content is not supported UTF-8 text or the page is invalid.
            ContentCorruptError: Original bytes fail integrity validation.
            ProductConflict: Content lifecycle prevents reading.
        """
        with self.store.transaction() as connection:
            value = (
                self.store.require(connection, "attachments", identity, Attachment)
                if kind == "attachment"
                else self.store.require(connection, "artifacts", identity, Artifact)
            )
            read_sequence = self.store.sequence(connection)
        if value.mime not in TEXT_PREVIEW_MIMES:
            raise ValueError("This content supports no text preview")
        payload = self.read_attachment(identity) if kind == "attachment" else self.read_artifact(identity)
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ValueError("Text preview requires valid UTF-8 content") from error
        return text_page(
            identity, text, offset=offset, max_bytes=max_bytes, read_sequence=read_sequence, read_scope="product"
        )

    def delete_attachment(self, attachment_id: str, *, request_id: str) -> Attachment:
        """Revoke unreferenced staging atomically against formal message admission."""
        request_digest = fingerprint({"attachment_id": attachment_id})
        key = f"attachment/delete/{request_id}"
        with self.store.transaction(write=True) as connection:
            old = self.store.receipt(connection, key, request_digest, Attachment)
            if old is not None:
                value = old
            else:
                current = self.store.require(connection, "attachments", attachment_id, Attachment)
                if current.status == "referenced":
                    raise ProductConflict("A formally referenced attachment cannot be revoked")
                value = current.model_copy(update={"status": "deleted", "expires_at": None})
                self.store.save(
                    connection,
                    "attachments",
                    attachment_id,
                    value,
                    client_id=current.client_attachment_id,
                    digest=current.digest,
                )
                self.store.emit(connection, attachment_id, "attachment.deleted", value.model_dump(mode="json"))
                self.store.save_receipt(connection, key, request_digest, value)
        self.remove_unreferenced_digest(value.digest, value.relative_path)
        return value

    def expire_attachments(self, *, now: datetime | None = None) -> tuple[str, ...]:
        """Reclaim expired staging only if a formal reference did not win admission."""
        instant = datetime.now(UTC) if now is None else now
        expired: list[Attachment] = []
        with self.store.transaction(write=True) as connection:
            for value in self.store.records(connection, "attachments", Attachment):
                if value.status == "ready" and value.expires_at is not None and value.expires_at <= instant:
                    removed = value.model_copy(update={"status": "deleted", "expires_at": None})
                    self.store.save(
                        connection,
                        "attachments",
                        value.attachment_id,
                        removed,
                        client_id=value.client_attachment_id,
                        digest=value.digest,
                    )
                    expired.append(value)
        for value in expired:
            self.remove_unreferenced_digest(value.digest, value.relative_path)
        return tuple(value.attachment_id for value in expired)

    def remove_unreferenced_digest(self, digest: str, relative_path: str) -> None:
        """Remove bytes only while the product transaction proves no remaining owner."""
        with self.store.transaction(write=True) as connection:
            if any(
                value.digest == digest and value.status != "deleted"
                for value in self.store.records(connection, "attachments", Attachment)
            ):
                return
            if any(
                value.digest == digest and value.deletion_state != "deleted"
                for value in self.store.records(connection, "artifacts", Artifact)
            ):
                return
            path = (self.store.content_root / relative_path).resolve()
            if not path.is_relative_to(self.store.content_root):
                raise ContentCorruptError("Content cleanup escaped the owned root")
            path.unlink(missing_ok=True)

    def preview_in_transaction(
        self,
        connection: sqlite3.Connection,
        space: Space,
        *,
        expires_at: datetime,
        scopes: CleanupScope = EMPTY_CLEANUP_SCOPE,
    ) -> DeletionPreview:
        """Compute exact ownership impact and every blocker at one product revision."""
        executions = tuple(
            value
            for value in self.store.records(connection, "executions", Execution)
            if value.space_id == space.space_id
        )
        identities = {value.execution_id for value in executions}
        artifacts = tuple(
            value
            for value in self.store.records(connection, "artifacts", Artifact)
            if value.owner_execution_id in identities and value.deletion_state != "deleted"
        )
        shared: list[str] = []
        owned: list[str] = []
        for artifact in artifacts:
            references = connection.execute(
                "SELECT owner_kind,owner_id FROM object_references WHERE target_kind='artifact' AND target_id=?",
                (artifact.artifact_id,),
            ).fetchall()
            if any(row["owner_kind"] != "execution" or row["owner_id"] not in identities for row in references):
                shared.append(artifact.artifact_id)
            else:
                owned.append(artifact.artifact_id)
        blockers = list(scopes.blockers)
        if any(value.status not in TERMINAL_STATUSES for value in executions):
            blockers.append("Active or unsettled Executions still belong to this Space")
        if any(value.unknown_effects for value in executions):
            blockers.append("External action results remain unknown")
        if any(
            value.target_execution_id in identities and value.state == "owned"
            for value in self.store.records(connection, "input_dispositions", InputDisposition)
        ):
            blockers.append("Accepted inputs still have an owning Worker")
        if any(
            value.execution_id in identities and value.status == "pending"
            for value in self.store.records(connection, "attentions", Attention)
        ):
            blockers.append("Pending human decisions still belong to this Space")
        reports = {
            value.report_id
            for value in self.store.records(connection, "reports", Report)
            if value.execution_id in identities and value.kind != "progress" and not value.rejection_reasons
        }
        if any(
            value.causation_id in reports and value.status != "handled"
            for value in self.store.records(connection, "inbox_items", InboxItem)
        ):
            blockers.append("An important report has not been formally delivered")
        if any(
            value.parent_execution_id in identities
            and value.execution_id not in identities
            and value.status not in TERMINAL_STATUSES
            for value in self.store.records(connection, "executions", Execution)
        ):
            blockers.append("Another Space has an active dependent child Execution")
        node_ids = {
            row["owner_id"]
            for row in connection.execute(
                "SELECT owner_id,target_id FROM object_references "
                "WHERE owner_kind='graph_node' AND target_kind='execution'"
            )
            if row["target_id"] in identities
        }
        references = [
            dict(row)
            for row in connection.execute(
                "SELECT owner_kind,owner_id,target_kind,target_id FROM object_references "
                "ORDER BY owner_kind,owner_id,target_kind,target_id"
            )
            if row["target_id"] in {space.space_id, *identities, *node_ids, *(value.artifact_id for value in artifacts)}
        ]
        reference_digest = fingerprint({"references": references})
        values = JSON_OBJECT.validate_python(
            {
                "space_id": space.space_id,
                "space_revision": space.revision,
                "execution_ids": sorted(identities),
                "artifact_ids": sorted(owned),
                "shared_artifact_ids": sorted(shared),
                "blockers": blockers,
                "expires_at": expires_at.isoformat(),
                "reference_digest": reference_digest,
                "scopes": scopes.model_dump(mode="json"),
            }
        )
        return DeletionPreview(
            space_id=space.space_id,
            space_revision=space.revision,
            preview_token=fingerprint(values),
            expires_at=expires_at,
            execution_ids=tuple(sorted(identities)),
            artifact_ids=tuple(sorted(owned)),
            shared_artifact_ids=tuple(sorted(shared)),
            blockers=tuple(blockers),
            reference_digest=reference_digest,
            scopes=scopes,
        )

    def deletion_preview(self, space_id: str, *, scopes: CleanupScope = EMPTY_CLEANUP_SCOPE) -> DeletionPreview:
        """Issue a five-minute exact-impact preview without accepting cleanup."""
        with self.store.transaction(write=True) as connection:
            space = self.store.require(connection, "spaces", space_id, Space)
            if space.deleted or space.deletion_pending:
                raise ProductConflict("The Space is already deleted or being reclaimed")
            value = self.preview_in_transaction(
                connection, space, expires_at=datetime.now(UTC) + timedelta(minutes=5), scopes=scopes
            )
            self.store.save(connection, "deletion_previews", value.preview_token, value)
            return value

    def accept_deletion(
        self,
        space_id: str,
        *,
        request_id: str,
        expected_revision: int,
        preview_token: str,
        scopes: CleanupScope = EMPTY_CLEANUP_SCOPE,
    ) -> CleanupRecord:
        """Fence every planned object and refuse new references before touching bytes."""
        request_digest = fingerprint(
            {"space_id": space_id, "expected_revision": expected_revision, "preview_token": preview_token}
        )
        key = f"space/delete/{request_id}"
        with self.store.transaction(write=True) as connection:
            old = self.store.receipt(connection, key, request_digest, CleanupRecord)
            if old is not None:
                return self.store.require(connection, "cleanup_records", request_id, CleanupRecord)
            space = self.store.require(connection, "spaces", space_id, Space)
            self.store.check_revision(space.revision, expected_revision)
            if space.deleted or space.deletion_pending:
                raise ProductConflict("The Space is already being deleted")
            preview = self.store.require(connection, "deletion_previews", preview_token, DeletionPreview)
            if preview.space_id != space_id or preview.expires_at <= datetime.now(UTC):
                raise ProductConflict("The deletion preview is expired or belongs to another Space")
            current = self.preview_in_transaction(connection, space, expires_at=preview.expires_at, scopes=scopes)
            if current != preview or current.blockers:
                raise ProductConflict(
                    "The deletion impact changed or still has blockers", current_revision=space.revision
                )
            generation = stable_id(f"cleanup/{request_id}")
            cleanup = CleanupRecord(request_id=request_id, space_id=space_id, generation=generation, plan=current)
            self.store.save(
                connection, "cleanup_records", request_id, cleanup, target_id=space_id, generation=generation
            )
            self.store.save_space(
                connection,
                space.model_copy(
                    update={"deletion_pending": True, "cleanup_generation": generation, "revision": space.revision + 1}
                ),
            )
            for artifact_id in current.artifact_ids:
                artifact = self.store.require(connection, "artifacts", artifact_id, Artifact)
                value = artifact.model_copy(update={"deletion_state": "deleting", "cleanup_generation": generation})
                self.store.save(
                    connection, "artifacts", artifact_id, value, owner_id=value.owner_execution_id, digest=value.digest
                )
            self.store.emit(
                connection, space_id, "space.deletion_pending", {"request_id": request_id, "generation": generation}
            )
            self.store.save_receipt(connection, key, request_digest, cleanup)
            return cleanup

    def task_file_scope(self, workspace: Path, space_id: str) -> tuple[dict[str, str | int], ...]:
        """Read task-file identities for an exact preview without following external links."""
        root = workspace / "work"
        source = root / space_id
        if root.is_symlink() or source.is_symlink() or source.resolve().parent != root.resolve():
            raise ContentCorruptError("Task cleanup refuses an unsafe ownership root")
        if not source.exists():
            return ()
        if not source.is_dir():
            raise ContentCorruptError("Task cleanup requires a controlled directory root")

        def reject_scan(error: OSError) -> None:
            """Reject an incomplete preview instead of understating its deletion impact."""
            raise error

        files: list[dict[str, str | int]] = []
        for directory, names, filenames in os.walk(source, followlinks=False, onerror=reject_scan):
            names.sort()
            links = [name for name in names if (Path(directory) / name).is_symlink()]
            for name in sorted((*filenames, *links)):
                path = Path(directory) / name
                stat = path.lstat()
                files.append(
                    {
                        "path": path.relative_to(source).as_posix(),
                        "size": stat.st_size,
                        "mtime_ns": stat.st_mtime_ns,
                        "inode": stat.st_ino,
                        "mode": stat.st_mode,
                    }
                )
        return tuple(files)

    def cleanup_record(self, request_id: str, *, expected_generation: str) -> CleanupRecord:
        """Validate the accepted cleanup owner before accessing another owned resource."""
        with self.store.transaction() as connection:
            cleanup = self.store.require(connection, "cleanup_records", request_id, CleanupRecord)
            space = self.store.require(connection, "spaces", cleanup.space_id, Space)
            if cleanup.generation != expected_generation or space.cleanup_generation != expected_generation:
                raise ProductConflict("The cleanup generation is obsolete")
            if cleanup.state != "completed" and (not space.deletion_pending or space.deleted):
                raise ProductConflict("The Space no longer belongs to this active cleanup")
            return cleanup

    def save_cleanup_sessions(
        self, request_id: str, *, expected_generation: str, session_ids: tuple[str, ...]
    ) -> CleanupRecord:
        """Persist the full recursive SDK cleanup plan before deleting its first Session."""
        self.cleanup_record(request_id, expected_generation=expected_generation)
        with self.store.transaction(write=True) as connection:
            cleanup = self.store.require(connection, "cleanup_records", request_id, CleanupRecord)
            if cleanup.generation != expected_generation:
                raise ProductConflict("The cleanup generation is obsolete")
            if "sessions_planned" in cleanup.completed_stages:
                if cleanup.session_ids != session_ids:
                    raise ProductConflict("The accepted Session cleanup plan cannot change")
                return cleanup
            value = cleanup.model_copy(
                update={"session_ids": session_ids, "completed_stages": (*cleanup.completed_stages, "sessions_planned")}
            )
            self.store.save(
                connection, "cleanup_records", request_id, value, target_id=value.space_id, generation=value.generation
            )
            return value

    def complete_cleanup_stage(
        self,
        request_id: str,
        *,
        expected_generation: str,
        stage: Literal["sessions", "facts", "work", "details", "content"],
    ) -> CleanupRecord:
        """Record a completed reclaim step without losing a previously committed plan."""
        self.cleanup_record(request_id, expected_generation=expected_generation)
        with self.store.transaction(write=True) as connection:
            cleanup = self.store.require(connection, "cleanup_records", request_id, CleanupRecord)
            if cleanup.generation != expected_generation:
                raise ProductConflict("The cleanup generation is obsolete")
            if stage in cleanup.completed_stages:
                return cleanup
            value = cleanup.model_copy(
                update={"completed_stages": (*cleanup.completed_stages, stage), "state": "pending", "last_error": None}
            )
            self.store.save(
                connection, "cleanup_records", request_id, value, target_id=value.space_id, generation=value.generation
            )
            return value

    def fail_cleanup(self, request_id: str, *, expected_generation: str, error_name: str) -> CleanupRecord:
        """Persist a visible retryable failure while retaining deletion ownership."""
        with self.store.transaction(write=True) as connection:
            cleanup = self.store.require(connection, "cleanup_records", request_id, CleanupRecord)
            if cleanup.generation != expected_generation:
                raise ProductConflict("The cleanup generation is obsolete")
            value = cleanup.model_copy(update={"state": "failed", "last_error": error_name})
            self.store.save(
                connection, "cleanup_records", request_id, value, target_id=value.space_id, generation=value.generation
            )
            return value

    def reclaim_work_directory(self, request_id: str, *, expected_generation: str, workspace: Path) -> None:
        """Move only this Space's controlled task directory into its owned recycle generation."""
        cleanup = self.cleanup_record(request_id, expected_generation=expected_generation)
        if "work" in cleanup.completed_stages:
            return
        root = workspace / "work"
        source = root / cleanup.space_id
        if root.is_symlink() or source.is_symlink():
            raise ContentCorruptError("Task cleanup refuses symbolic-link ownership roots")
        if source.resolve().parent != root.resolve():
            raise ContentCorruptError("Task cleanup escaped its owned root")
        recycle = self.store.root / "recycle" / cleanup.generation / "work"
        recycle.mkdir(parents=True, exist_ok=True)
        destination = recycle / cleanup.space_id
        with self.store.transaction() as connection:
            latest = self.store.require(connection, "cleanup_records", request_id, CleanupRecord)
            if latest.generation != expected_generation:
                raise ProductConflict("The cleanup generation is obsolete")
            if source.exists():
                if destination.exists():
                    raise ContentCorruptError("The controlled task directory was recreated during cleanup")
                os.replace(source, destination)
            if destination.exists():
                shutil.rmtree(destination)
        self.complete_cleanup_stage(request_id, expected_generation=expected_generation, stage="work")

    def reclaim_execution_details(self, request_id: str, *, expected_generation: str) -> None:
        """Delete private execution bodies while preserving message and source tombstones."""
        cleanup = self.cleanup_record(request_id, expected_generation=expected_generation)
        if "details" in cleanup.completed_stages:
            return
        owners = set(cleanup.plan.execution_ids)
        with self.store.transaction(write=True) as connection:
            latest = self.store.require(connection, "cleanup_records", request_id, CleanupRecord)
            if latest.generation != expected_generation:
                raise ProductConflict("The cleanup generation is obsolete")
            for record in self.store.records(connection, "tool_results", ToolResultDetail):
                if record.execution_id in owners:
                    connection.execute("DELETE FROM tool_results WHERE id=?", (record.tool_result_id,))
            for attempt in self.store.records(connection, "node_attempts", NodeAttempt):
                if attempt.execution_id in owners:
                    connection.execute("DELETE FROM node_attempts WHERE id=?", (attempt.node_attempt_id,))
            for run in self.store.records(connection, "graph_runs", GraphRunDetail):
                if run.execution_id in owners:
                    connection.execute("DELETE FROM graph_runs WHERE id=?", (run.graph_run_id,))
            for definition in self.store.records(connection, "graph_definitions", GraphDefinition):
                if definition.execution_id in owners:
                    identity = f"{definition.execution_id}/{definition.graph.graph_id}/{definition.graph.revision}"
                    connection.execute("DELETE FROM graph_definitions WHERE id=?", (identity,))
            for execution_id in owners:
                tombstone = json.dumps(
                    {"execution_id": execution_id, "space_id": cleanup.space_id, "deleted": True},
                    ensure_ascii=False,
                )
                connection.execute(
                    "UPDATE product_events SET data=? WHERE subject=? AND (type LIKE 'graph.%' OR type LIKE 'node.%')",
                    (tombstone, execution_id),
                )
        self.complete_cleanup_stage(request_id, expected_generation=expected_generation, stage="details")

    def finish_deletion(self, request_id: str, *, expected_generation: str) -> CleanupRecord:
        """Publish the tombstone only after every controlled reclaim stage is committed."""
        cleanup = self.cleanup_record(request_id, expected_generation=expected_generation)
        with self.store.transaction(write=True) as connection:
            latest = self.store.require(connection, "cleanup_records", request_id, CleanupRecord)
            space = self.store.require(connection, "spaces", cleanup.space_id, Space)
            if latest.generation != expected_generation or space.cleanup_generation != expected_generation:
                raise ProductConflict("The Space cleanup generation changed")
            if latest.state == "completed":
                return latest
            if not set(latest.completed_stages) >= REQUIRED_CLEANUP_STAGES:
                raise ProductConflict("Space deletion requires every owned cleanup stage to settle")
            for attention in self.store.records(connection, "attentions", Attention):
                if attention.execution_id in cleanup.plan.execution_ids and attention.status == "pending":
                    self.store.save_attention(
                        connection,
                        attention.model_copy(update={"status": "withdrawn", "revision": attention.revision + 1}),
                    )
            value = latest.model_copy(update={"state": "completed", "last_error": None})
            self.store.save(
                connection,
                "cleanup_records",
                request_id,
                value,
                target_id=space.space_id,
                generation=expected_generation,
            )
            self.store.save_space(
                connection,
                space.model_copy(update={"deleted": True, "deletion_pending": False, "revision": space.revision + 1}),
            )
            self.store.emit(
                connection,
                space.space_id,
                "space.deleted",
                {"space_id": space.space_id, "generation": expected_generation},
            )
        return value

    def reclaim_content(self, request_id: str, *, expected_generation: str) -> CleanupRecord:
        """Serialize one cleanup owner across its file and database transactions."""
        locks = self.store.root / "cleanup-locks"
        locks.mkdir(exist_ok=True)
        with (locks / stable_id(request_id)).open("a+b") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            return self.reclaim_owned_content(request_id, expected_generation=expected_generation)

    def reclaim_owned_content(self, request_id: str, *, expected_generation: str) -> CleanupRecord:
        """Reclaim controlled content for one accepted cleanup generation."""
        with self.store.transaction() as connection:
            cleanup = self.store.require(connection, "cleanup_records", request_id, CleanupRecord)
            if cleanup.generation != expected_generation:
                raise ProductConflict("The cleanup generation is obsolete")
            if cleanup.state == "completed":
                return cleanup
        recycle = self.store.root / "recycle" / cleanup.generation
        recycle.mkdir(parents=True, exist_ok=True)
        try:
            for artifact_id in cleanup.plan.artifact_ids:
                with self.store.transaction(write=True) as connection:
                    latest = self.store.require(connection, "cleanup_records", request_id, CleanupRecord)
                    artifact = self.store.require(connection, "artifacts", artifact_id, Artifact)
                    if latest.generation != expected_generation or artifact.cleanup_generation != expected_generation:
                        raise ProductConflict("Cleanup no longer owns the exact content generation")
                    if artifact.deletion_state == "deleted":
                        continue
                    if artifact.deletion_state != "deleting":
                        raise ProductConflict("The artifact is no longer marked for this cleanup")
                    shared_digest = any(
                        value.artifact_id != artifact_id
                        and value.digest == artifact.digest
                        and value.deletion_state == "active"
                        for value in self.store.records(connection, "artifacts", Artifact)
                    ) or any(
                        value.digest == artifact.digest and value.status != "deleted"
                        for value in self.store.records(connection, "attachments", Attachment)
                    )
                    source = (self.store.content_root / artifact.relative_path).resolve()
                    if not source.is_relative_to(self.store.content_root):
                        raise ContentCorruptError("Cleanup content escaped its owned root")
                    destination = recycle / artifact.digest
                    if not shared_digest and source.exists():
                        os.replace(source, destination)
                    destination.unlink(missing_ok=True)
                    self.store.save(
                        connection,
                        "artifacts",
                        artifact_id,
                        artifact.model_copy(update={"deletion_state": "deleted"}),
                        owner_id=artifact.owner_execution_id,
                        digest=artifact.digest,
                    )
            shutil.rmtree(recycle)
            return self.complete_cleanup_stage(request_id, expected_generation=expected_generation, stage="content")
        except (OSError, ContentCorruptError) as error:
            with self.store.transaction(write=True) as connection:
                latest = self.store.require(connection, "cleanup_records", request_id, CleanupRecord)
                failed = latest.model_copy(update={"state": "failed", "last_error": type(error).__name__})
                self.store.save(
                    connection,
                    "cleanup_records",
                    request_id,
                    failed,
                    target_id=latest.space_id,
                    generation=latest.generation,
                )
            raise
