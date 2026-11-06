import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path

from pleiad.contracts import ContractModel
from pleiad.errors.base import SystemFault
from pydantic import AwareDatetime, Field, ValidationError, field_validator


class SavedGraphReference(ContractModel):
    """Select one immutable Graph revision for reuse in future conversations."""

    graph_id: str = Field(min_length=1)
    revision: int = Field(ge=1)


class SpaceDefinition(ContractModel):
    """Validate a reusable purpose and preferences before a space is created."""

    name: str = Field(min_length=1, max_length=120)
    instructions: str = Field(default="", max_length=20_000)
    preferred_tool_codes: list[str] = Field(default_factory=list, max_length=200)
    preferred_skill_codes: list[str] = Field(default_factory=list, max_length=200)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        """Reject a blank display name and remove surrounding whitespace."""
        value = value.strip()
        if not value:
            raise ValueError("Space name must not be blank")
        return value

    @field_validator("preferred_tool_codes", "preferred_skill_codes")
    @classmethod
    def validate_codes(cls, value: list[str]) -> list[str]:
        """Keep explicit preferences unique and within SDK identity limits."""
        if any(not code.strip() or len(code) > 256 or code != code.strip() for code in value):
            raise ValueError("Capability codes must be non-blank identities of at most 256 characters")
        if len(value) != len(set(value)):
            raise ValueError("Capability preferences must not contain duplicate codes")
        return value


class SpaceConfiguration(SpaceDefinition):
    """Extend reusable settings with a Graph saved from an existing task."""

    saved_graph: SavedGraphReference | None = None


class SpaceRecord(SpaceConfiguration):
    """Persist one reusable space independently of conversation memory."""

    space_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    revision: int = Field(default=1, ge=1)
    created_at: AwareDatetime
    updated_at: AwareDatetime


class ConversationRecord(ContractModel):
    """Associate one task's SDK conversation with its reusable product space."""

    conversation_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    space_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    title: str = Field(min_length=1, max_length=160)
    created_at: AwareDatetime
    updated_at: AwareDatetime


class WorkspaceConflictError(ValueError):
    """Reject a workspace mutation that conflicts with its current state."""


class WorkspaceRepository:
    """Store space and conversation metadata with atomic SQLite transactions."""

    def __init__(self, root: Path) -> None:
        """Select storage without doing synchronous IO at property access."""
        self.root = root
        self.path = root / "workspaces.sqlite3"

    @contextmanager
    def transaction(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        """Open a short transaction and preserve storage failure diagnostics."""
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(self.path, timeout=5.0)) as connection:
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA foreign_keys = ON")
                connection.execute("PRAGMA busy_timeout = 5000")
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS spaces (
                        space_id TEXT PRIMARY KEY,
                        record_json TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS conversations (
                        conversation_id TEXT PRIMARY KEY,
                        space_id TEXT NOT NULL REFERENCES spaces(space_id),
                        record_json TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS conversations_by_space ON conversations(space_id);
                    CREATE TABLE IF NOT EXISTS deleted_conversations (
                        conversation_id TEXT PRIMARY KEY
                    );
                    """
                )
                if write:
                    connection.execute("BEGIN IMMEDIATE")
                yield connection
                if write:
                    connection.commit()
        except (OSError, sqlite3.Error, ValidationError) as error:
            raise SystemFault(
                "weave.system_storage", str({"path": str(self.path), "exception": type(error).__name__})
            ) from error

    def list_spaces(self) -> tuple[SpaceRecord, ...]:
        """Return spaces in stable creation order."""
        with self.transaction() as connection:
            rows = connection.execute("SELECT record_json FROM spaces ORDER BY rowid").fetchall()
            return tuple(SpaceRecord.model_validate_json(row["record_json"]) for row in rows)

    def get_space(self, space_id: str) -> SpaceRecord | None:
        """Read one space using an opaque SQL identity rather than a file path."""
        with self.transaction() as connection:
            row = connection.execute("SELECT record_json FROM spaces WHERE space_id = ?", (space_id,)).fetchone()
            return None if row is None else SpaceRecord.model_validate_json(row["record_json"])

    def create_space(self, record: SpaceRecord) -> SpaceRecord:
        """Persist a newly generated space without replacing an existing record."""
        with self.transaction(write=True) as connection:
            connection.execute(
                "INSERT INTO spaces (space_id, record_json) VALUES (?, ?)",
                (record.space_id, record.model_dump_json()),
            )
        return record

    def update_space(self, space_id: str, configuration: SpaceConfiguration, expected_revision: int) -> SpaceRecord:
        """Apply a complete space configuration only when its revision matches.

        Args:
            space_id: Existing product space identity.
            configuration: Validated new rules and capability preferences.
            expected_revision: Revision observed by the caller.

        Returns:
            The committed next revision.

        Raises:
            LookupError: If the space does not exist.
            WorkspaceConflictError: If another update already changed the space.
        """
        with self.transaction(write=True) as connection:
            row = connection.execute("SELECT record_json FROM spaces WHERE space_id = ?", (space_id,)).fetchone()
            if row is None:
                raise LookupError("Space not found")
            current = SpaceRecord.model_validate_json(row["record_json"])
            if expected_revision != current.revision:
                raise WorkspaceConflictError(
                    f"Space revision changed: expected {expected_revision}, current {current.revision}"
                )
            updated = SpaceRecord(
                **configuration.model_dump(),
                space_id=space_id,
                revision=current.revision + 1,
                created_at=current.created_at,
                updated_at=datetime.now(UTC),
            )
            connection.execute(
                "UPDATE spaces SET record_json = ? WHERE space_id = ?", (updated.model_dump_json(), space_id)
            )
        return updated

    def create_conversation(self, record: ConversationRecord) -> ConversationRecord:
        """Create an independent task conversation inside an existing space."""
        with self.transaction(write=True) as connection:
            if connection.execute("SELECT 1 FROM spaces WHERE space_id = ?", (record.space_id,)).fetchone() is None:
                raise LookupError("Space not found")
            connection.execute(
                "INSERT INTO conversations (conversation_id, space_id, record_json) VALUES (?, ?, ?)",
                (record.conversation_id, record.space_id, record.model_dump_json()),
            )
        return record

    def get_conversation(self, conversation_id: str) -> ConversationRecord | None:
        """Read conversation metadata without changing its activity timestamp."""
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT record_json FROM conversations WHERE conversation_id = ?", (conversation_id,)
            ).fetchone()
            return None if row is None else ConversationRecord.model_validate_json(row["record_json"])

    def list_conversations(self, space_id: str) -> tuple[ConversationRecord, ...]:
        """List task conversations without mixing in another space's history."""
        with self.transaction() as connection:
            if connection.execute("SELECT 1 FROM spaces WHERE space_id = ?", (space_id,)).fetchone() is None:
                raise LookupError("Space not found")
            rows = connection.execute(
                "SELECT record_json FROM conversations WHERE space_id = ? ORDER BY rowid", (space_id,)
            ).fetchall()
            records = (ConversationRecord.model_validate_json(row["record_json"]) for row in rows)
            return tuple(sorted(records, key=lambda record: record.updated_at, reverse=True))

    def execution_space(self, conversation_id: str) -> SpaceRecord | None:
        """Snapshot the current rules and touch only the submitted conversation.

        Unknown conversation identities remain valid for the existing CLI and TUI.
        """
        with self.transaction(write=True) as connection:
            if (
                connection.execute(
                    "SELECT 1 FROM deleted_conversations WHERE conversation_id = ?", (conversation_id,)
                ).fetchone()
                is not None
            ):
                raise LookupError("Conversation was deleted")
            row = connection.execute(
                "SELECT spaces.record_json AS space_json, conversations.record_json AS conversation_json "
                "FROM conversations JOIN spaces USING(space_id) WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
            if row is None:
                return None
            space = SpaceRecord.model_validate_json(row["space_json"])
            conversation = ConversationRecord.model_validate_json(row["conversation_json"])
            updated = conversation.model_copy(update={"updated_at": datetime.now(UTC)})
            connection.execute(
                "UPDATE conversations SET record_json = ? WHERE conversation_id = ?",
                (updated.model_dump_json(), conversation_id),
            )
            return space

    def delete(self, conversation_ids: frozenset[str], *, space_id: str | None = None) -> None:
        """Remove workspace metadata and prevent deleted task identities being reused.

        Args:
            conversation_ids: Conversations whose owned runtime data has been removed.
            space_id: Optional containing space to remove in the same transaction.
        """
        with self.transaction(write=True) as connection:
            for conversation_id in conversation_ids:
                connection.execute(
                    "INSERT OR IGNORE INTO deleted_conversations (conversation_id) VALUES (?)", (conversation_id,)
                )
                connection.execute("DELETE FROM conversations WHERE conversation_id = ?", (conversation_id,))
            if space_id is not None:
                connection.execute("DELETE FROM spaces WHERE space_id = ?", (space_id,))
